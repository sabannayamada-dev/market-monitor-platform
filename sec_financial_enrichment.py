from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


APP_VERSION = "0.2.2"
TICKER_ALIASES = ["銘柄", "ticker", "symbol", "stock_code", "入力値"]
SIGNAL_DATE_ALIASES = ["底打ち候補日", "底検知日", "候補日", "signal_date", "bottom_date"]
ALLOWED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    kind: str
    concepts: tuple[str, ...]
    preferred_units: tuple[str, ...] = ("USD",)


METRICS = (
    MetricSpec("revenue", "売上高", "duration", (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet", "SalesRevenueGoodsNet", "Revenue",
        "RevenueFromContractsWithCustomers", "RevenuesNetOfInterestExpense",
    )),
    MetricSpec("gross_profit", "粗利益", "duration", ("GrossProfit",)),
    MetricSpec("cost_of_revenue", "売上原価", "duration", (
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
        "CostOfSales", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    )),
    MetricSpec("operating_income", "営業利益", "duration", (
        "OperatingIncomeLoss", "ProfitLossFromOperatingActivities",
    )),
    MetricSpec("net_income", "純利益", "duration", (
        "NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic",
    )),
    MetricSpec("assets", "総資産", "instant", ("Assets",)),
    MetricSpec("assets_current", "流動資産", "instant", ("AssetsCurrent", "CurrentAssets")),
    MetricSpec("liabilities", "負債", "instant", ("Liabilities",)),
    MetricSpec("liabilities_current", "流動負債", "instant", ("LiabilitiesCurrent", "CurrentLiabilities")),
    MetricSpec("equity", "純資産", "instant", (
        "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapital", "Equity",
    )),
    MetricSpec("cash", "現金等", "instant", (
        "CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks", "CashAndCashEquivalents",
    )),
    MetricSpec("operating_cf", "営業CF", "duration", (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities",
    )),
    MetricSpec("capex", "設備投資額", "duration", (
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForAdditionsToPropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipment",
    )),
    MetricSpec("rd", "研究開発費", "duration", (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
    )),
    MetricSpec("inventory", "棚卸資産", "instant", (
        "InventoryNet", "Inventories", "InventoryGross",
        "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
        "InventoryFinishedGoodsNetOfAllowancesCustomerAdvancesAndProgressBillings",
        "InventoryFinishedGoodsNetOfReserves", "InventoryWorkInProcessNetOfReserves",
    )),
    MetricSpec("receivables", "売掛債権", "instant", (
        "AccountsReceivableNetCurrent", "AccountsNotesAndLoansReceivableNetCurrent",
        "TradeAndOtherCurrentReceivables", "CurrentTradeReceivables",
    )),
    MetricSpec("long_term_debt", "長期有利子負債", "instant", (
        "LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "NoncurrentBorrowings", "LongtermBorrowings",
    )),
    MetricSpec("interest_expense", "支払利息", "duration", (
        "InterestExpenseNonOperating", "InterestAndDebtExpense", "InterestExpense",
    )),
    MetricSpec("shares", "発行済株式数", "instant", (
        "EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding",
        "CommonStockSharesIssued",
    ), ("shares",)),
    MetricSpec("eps", "希薄化後EPS", "duration", (
        "EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
    ), ("USD/shares",)),
)


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"CSVを読み込めません: {path} ({last_error})")


def normalize_header(value: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).lower()


def find_column(columns: list[str], aliases: list[str]) -> str:
    normalized = {normalize_header(column): column for column in columns}
    for alias in aliases:
        if normalize_header(alias) in normalized:
            return normalized[normalize_header(alias)]
    for column in columns:
        key = normalize_header(column)
        if any(normalize_header(alias) in key for alias in aliases):
            return column
    return ""


def normalize_ticker(value: object) -> str:
    text = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", text):
        return ""
    if text.endswith(".T") or text.endswith("-USD") or text.startswith("^"):
        return ""
    return text.replace(".", "-")


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    # SEC JSONの日付はほぼYYYY-MM-DD。pandasを通すと大量行で桁違いに遅い。
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        parsed = pd.to_datetime(text, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()


def json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, request_interval: float = 0.15) -> None:
        if "@" not in user_agent or len(user_agent.strip()) < 6:
            raise ValueError("SEC User-Agentには連絡可能なメールアドレスを含めてください。例: Taro user@example.com")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_interval = max(float(request_interval), 0.11)
        self.headers = {
            "User-Agent": user_agent.strip(),
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.network_count = 0
        self.cache_count = 0

    def _wait(self) -> None:
        # 複数ワーカーでもリクエスト開始間隔をSECの許容範囲内に保つ。
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.request_interval:
                time.sleep(self.request_interval - elapsed)
            self._last_request_at = time.monotonic()

    def get_json(self, url: str, cache_path: Path, refresh: bool = False) -> dict[str, Any]:
        if cache_path.exists() and not refresh:
            try:
                with self._count_lock:
                    self.cache_count += 1
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                self._wait()
                request = urllib.request.Request(url, headers=self.headers)
                response = urllib.request.urlopen(request, timeout=60)
                with self._count_lock:
                    self.network_count += 1
                status = int(getattr(response, "status", 200))
                if status == 200:
                    body = response.read()
                    content_encoding = str(response.headers.get("Content-Encoding", "")).lower()
                    if content_encoding == "gzip":
                        body = gzip.decompress(body)
                    elif content_encoding == "deflate":
                        body = zlib.decompress(body)
                    payload = json.loads(body.decode("utf-8"))
                    json_dump(cache_path, payload)
                    return payload
                if status in {403, 429, 500, 502, 503, 504}:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise RuntimeError(f"SEC API HTTP {status}: {url}")
            except urllib.error.HTTPError as exc:
                with self._count_lock:
                    self.network_count += 1
                last_error = exc
                if exc.code in {403, 429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(1.5 * (2**attempt))
                    continue
                break
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1.5 * (2**attempt))
        raise RuntimeError(f"SEC API取得失敗: {url} ({last_error})")

    def ticker_map(self, refresh: bool = False) -> dict[str, dict[str, str]]:
        payload = self.get_json(
            "https://www.sec.gov/files/company_tickers.json",
            self.cache_dir / "company_tickers.json",
            refresh=refresh,
        )
        result: dict[str, dict[str, str]] = {}
        for item in payload.values():
            ticker = normalize_ticker(item.get("ticker", ""))
            if ticker:
                result[ticker] = {
                    "cik": str(item.get("cik_str", "")).zfill(10),
                    "title": str(item.get("title", "")),
                }
        return result

    def company_facts(self, cik: str, refresh: bool = False) -> dict[str, Any]:
        return self.get_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            self.cache_dir / "companyfacts" / f"CIK{cik}.json",
            refresh=refresh,
        )

    def company_submissions(self, cik: str, refresh: bool = False) -> dict[str, Any]:
        return self.get_json(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            self.cache_dir / "submissions" / f"CIK{cik}.json",
            refresh=refresh,
        )


def _valid_fact_rows(companyfacts: dict[str, Any], concept: str, signal_date: date) -> list[dict[str, Any]]:
    # 同じ企業・conceptは3時点×複数抽出で繰り返し参照されるため、
    # JSON行の整形と日付変換を一度だけ行う。
    index = companyfacts.setdefault("_codex_concept_row_index", {})
    if concept not in index:
        prepared: list[dict[str, Any]] = []
        for taxonomy in ("us-gaap", "ifrs-full", "dei", "srt"):
            fact = companyfacts.get("facts", {}).get(taxonomy, {}).get(concept)
            if not fact:
                continue
            for unit, rows in fact.get("units", {}).items():
                for raw in rows:
                    filed = parse_date(raw.get("filed"))
                    period_end = parse_date(raw.get("end"))
                    form = str(raw.get("form", ""))
                    if not filed or not period_end or form not in ALLOWED_FORMS:
                        continue
                    try:
                        value = float(raw.get("val"))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(value):
                        continue
                    row = dict(raw)
                    period_start = parse_date(raw.get("start"))
                    duration_days = (
                        (period_end - period_start).days + 1
                        if period_start and period_end >= period_start else None
                    )
                    row.update({
                        "value": value, "unit": unit, "taxonomy": taxonomy,
                        "concept": concept, "_filed_date": filed,
                        "_period_end_date": period_end,
                        "_duration_days_cached": duration_days,
                    })
                    prepared.append(row)
        index[concept] = prepared
    return [
        row for row in index[concept]
        if row["_filed_date"] <= signal_date and row["_period_end_date"] <= signal_date
    ]


def _duration_days(row: dict[str, Any]) -> int | None:
    if "_duration_days_cached" in row:
        return row["_duration_days_cached"]
    start, end = parse_date(row.get("start")), parse_date(row.get("end"))
    return (end - start).days + 1 if start and end and end >= start else None


def select_fact(companyfacts: dict[str, Any], spec: MetricSpec, signal_date: date, annual_only: bool = False) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for concept_rank, concept in enumerate(spec.concepts):
        for row in _valid_fact_rows(companyfacts, concept, signal_date):
            if annual_only and row.get("form") not in ANNUAL_FORMS:
                continue
            if spec.kind == "duration" and not row.get("start"):
                continue
            row["concept_rank"] = concept_rank
            candidates.append(row)
    if not candidates:
        return None

    def score(row: dict[str, Any]) -> tuple[Any, ...]:
        unit = str(row.get("unit", ""))
        unit_rank = spec.preferred_units.index(unit) if unit in spec.preferred_units else len(spec.preferred_units)
        filed = row.get("_filed_date") or date.min
        end = row.get("_period_end_date") or date.min
        days = _duration_days(row) or 0
        if spec.kind == "duration":
            expected = 365 if row.get("form") in ANNUAL_FORMS else 91
            duration_fit = -abs(days - expected)
        else:
            duration_fit = 0
        # Latest available period first; taxonomy concept priority resolves same-period duplicates.
        return (end, filed, -unit_rank, duration_fit, -int(row.get("concept_rank", 999)))

    selected = max(candidates, key=score)
    selected["duration_days"] = _duration_days(selected)
    return selected


def _fact_value(fact: dict[str, Any] | None) -> float | None:
    return None if not fact else fact.get("value")


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        a, b = float(numerator), float(denominator)
        return a / b if math.isfinite(a) and math.isfinite(b) and b != 0 else None
    except (TypeError, ValueError):
        return None


def extract_point_in_time_financials(companyfacts: dict[str, Any], signal_date: date) -> dict[str, Any]:
    output: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    annual: dict[str, Any] = {}
    currencies: list[str] = []
    for spec in METRICS:
        latest_fact = select_fact(companyfacts, spec, signal_date, annual_only=False)
        annual_fact = select_fact(companyfacts, spec, signal_date, annual_only=True)
        latest[spec.key] = _fact_value(latest_fact)
        annual[spec.key] = _fact_value(annual_fact)
        output[f"底検知時_SEC_{spec.label}"] = latest[spec.key]
        output[f"底検知時_SEC_{spec.label}_直近年次"] = annual[spec.key]
        if latest_fact:
            currencies.append(str(latest_fact.get("unit", "")))
            audit[spec.label] = {
                "value": latest_fact.get("value"), "unit": latest_fact.get("unit"),
                "taxonomy": latest_fact.get("taxonomy"), "tag": latest_fact.get("concept"),
                "form": latest_fact.get("form"), "filed": latest_fact.get("filed"),
                "period_start": latest_fact.get("start"), "period_end": latest_fact.get("end"),
                "duration_days": latest_fact.get("duration_days"), "accession": latest_fact.get("accn"),
            }

    def derive_missing(values: dict[str, Any], suffix: str) -> None:
        revenue, cost = values.get("revenue"), values.get("cost_of_revenue")
        if values.get("gross_profit") is None and revenue is not None and cost is not None:
            gross_profit = float(revenue) - abs(float(cost))
            ratio = _safe_ratio(gross_profit, revenue)
            if ratio is not None and -2.0 <= ratio <= 2.0:
                values["gross_profit"] = gross_profit
                output[f"底検知時_SEC_粗利益{suffix}"] = gross_profit
                audit[f"粗利益{suffix}_算出"] = {"method": "売上高-abs(売上原価)"}
        assets, equity = values.get("assets"), values.get("equity")
        if values.get("liabilities") is None and assets is not None and equity is not None:
            liabilities = float(assets) - float(equity)
            if math.isfinite(liabilities) and liabilities >= 0:
                values["liabilities"] = liabilities
                output[f"底検知時_SEC_負債{suffix}"] = liabilities
                audit[f"負債{suffix}_算出"] = {"method": "総資産-純資産"}

    derive_missing(latest, "")
    derive_missing(annual, "_直近年次")
    output["底検知時_SEC_営業利益率"] = _safe_ratio(latest.get("operating_income"), latest.get("revenue"))
    output["底検知時_SEC_純利益率"] = _safe_ratio(latest.get("net_income"), latest.get("revenue"))
    output["底検知時_SEC_流動比率"] = _safe_ratio(latest.get("assets_current"), latest.get("liabilities_current"))
    output["底検知時_SEC_負債総資産比率"] = _safe_ratio(latest.get("liabilities"), latest.get("assets"))
    output["底検知時_SEC_現金総資産比率"] = _safe_ratio(latest.get("cash"), latest.get("assets"))
    output["底検知時_SEC_研究開発費売上比率"] = _safe_ratio(latest.get("rd"), latest.get("revenue"))
    output["底検知時_SEC_FCF"] = (
        latest["operating_cf"] - latest["capex"]
        if latest.get("operating_cf") is not None and latest.get("capex") is not None else None
    )
    monetary_units = [unit for unit in currencies if unit not in {"shares", "pure", "USD/shares"}]
    output["底検知時_SEC_主通貨"] = max(set(monetary_units), key=monetary_units.count) if monetary_units else ""
    available = sum(value is not None for value in latest.values())
    output["底検知時_SEC_取得数"] = available
    output["底検知時_SEC_採用根拠JSON"] = json.dumps(audit, ensure_ascii=False, separators=(",", ":"))
    filed_dates = [parse_date(item.get("filed")) for item in audit.values()]
    filed_dates = [item for item in filed_dates if item]
    output["底検知時_SEC_最新採用提出日"] = max(filed_dates).isoformat() if filed_dates else ""
    return output


ProgressCallback = Callable[[dict[str, Any]], None]


def enrich_csv(
    input_path: Path,
    output_dir: Path,
    user_agent: str,
    max_rows: int | None = None,
    request_interval: float = 0.15,
    refresh_cache: bool = False,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    frame = read_csv_flexible(input_path)
    ticker_column = find_column(list(frame.columns), TICKER_ALIASES)
    date_column = find_column(list(frame.columns), SIGNAL_DATE_ALIASES)
    if not ticker_column or not date_column:
        raise ValueError(f"必要列がありません。銘柄列={ticker_column or '未検出'}、底検知日列={date_column or '未検出'}")
    if max_rows:
        frame = frame.head(max_rows).copy()
    else:
        frame = frame.copy()
    output_dir.mkdir(parents=True, exist_ok=True)
    client = SecClient(user_agent, output_dir / "cache", request_interval=request_interval)
    ticker_map = client.ticker_map(refresh=refresh_cache)

    prepared: list[tuple[int, str, date, str, str]] = []
    skipped_non_us = skipped_no_date = ticker_unmapped = 0
    for idx, row in frame.iterrows():
        ticker = normalize_ticker(row.get(ticker_column, ""))
        signal_date = parse_date(row.get(date_column, ""))
        if not ticker:
            skipped_non_us += 1
            continue
        if not signal_date:
            skipped_no_date += 1
            continue
        mapping = ticker_map.get(ticker)
        if not mapping:
            ticker_unmapped += 1
            continue
        prepared.append((idx, ticker, signal_date, mapping["cik"], mapping["title"]))

    total = len(prepared)
    results: dict[int, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    facts_cache: dict[str, dict[str, Any]] = {}
    success = no_facts = 0
    for position, (idx, ticker, signal_date, cik, sec_name) in enumerate(prepared, start=1):
        if stop_event and stop_event.is_set():
            break
        try:
            if cik not in facts_cache:
                facts_cache[cik] = client.company_facts(cik, refresh=refresh_cache)
            extracted = extract_point_in_time_financials(facts_cache[cik], signal_date)
            extracted.update({
                "底検知時_SEC状態": "取得成功" if extracted["底検知時_SEC_取得数"] else "対象時点以前の財務なし",
                "底検知時_SEC_CIK": cik,
                "底検知時_SEC企業名": sec_name,
                "底検知時_SECティッカー": ticker,
                "底検知時_SEC基準日": signal_date.isoformat(),
                "底検知時_SECアプリver": APP_VERSION,
            })
            results[idx] = extracted
            if extracted["底検知時_SEC_取得数"]:
                success += 1
            else:
                no_facts += 1
        except Exception as exc:
            errors.append({"row": int(idx) + 2, "ticker": ticker, "cik": cik, "error": str(exc)})
            results[idx] = {
                "底検知時_SEC状態": "エラー", "底検知時_SEC_CIK": cik,
                "底検知時_SEC企業名": sec_name, "底検知時_SECティッカー": ticker,
                "底検知時_SEC基準日": signal_date.isoformat(), "底検知時_SECエラー": str(exc),
                "底検知時_SECアプリver": APP_VERSION,
            }
        elapsed = time.perf_counter() - started
        if progress:
            progress({
                "phase": "collect", "current": position, "total": total, "ticker": ticker,
                "success": success, "no_facts": no_facts, "errors": len(errors),
                "percent": position / total * 100 if total else 100,
                "eta_seconds": elapsed / position * (total - position) if position else None,
            })

    result_frame = frame.copy()
    result_columns: list[str] = []
    for item in results.values():
        for column in item:
            if column not in result_columns:
                result_columns.append(column)
    for column in result_columns:
        result_frame[column] = pd.Series([None] * len(result_frame), index=result_frame.index, dtype=object)
    for idx, item in results.items():
        for column, value in item.items():
            result_frame.at[idx, column] = "" if value is None else value

    output_csv = output_dir / "stock_bottom_with_sec_financials.csv"
    result_frame.to_csv(output_csv, index=False, encoding="utf-8-sig")
    errors_path = output_dir / "sec_financial_enrichment_errors.json"
    json_dump(errors_path, errors)
    report = {
        "app_version": APP_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_csv": str(input_path), "output_csv": str(output_csv),
        "input_rows": len(frame), "eligible_rows": total, "processed_rows": len(results),
        "success_rows": success, "no_facts_rows": no_facts, "error_rows": len(errors),
        "skipped_non_us_rows": skipped_non_us, "skipped_no_signal_date_rows": skipped_no_date,
        "ticker_unmapped_rows": ticker_unmapped, "unique_ciks": len(facts_cache),
        "network_requests": client.network_count, "cache_hits": client.cache_count,
        "stopped": bool(stop_event and stop_event.is_set()),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "errors_json": str(errors_path),
    }
    report_path = output_dir / "sec_financial_enrichment_report.json"
    json_dump(report_path, report)
    if progress:
        progress({"phase": "done", **report})
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="底検知CSVにSEC時点財務を結合します。")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/sec_financial_enrichment"))
    parser.add_argument("--user-agent", required=True, help="SEC向け識別子。メールアドレスを含めてください。")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--request-interval", type=float, default=0.15)
    parser.add_argument("--refresh-cache", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    def print_progress(info: dict[str, Any]) -> None:
        if info.get("phase") == "collect":
            print(
                "SEC財務取得状況: "
                f"{info['current']}/{info['total']}件 ({info['percent']:.1f}%) "
                f"銘柄={info['ticker']} 成功={info['success']} 財務なし={info['no_facts']} エラー={info['errors']}",
                flush=True,
            )
        elif info.get("phase") == "done":
            print(json.dumps(info, ensure_ascii=False), flush=True)

    report = enrich_csv(
        args.input, args.output_dir, args.user_agent, args.max_rows,
        args.request_interval, args.refresh_cache, print_progress,
    )
    return 0 if report["error_rows"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
