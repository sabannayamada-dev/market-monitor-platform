from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app_meta import APP_BUILD_DATE, APP_VERSION
from company_scoring import (
    RunContext,
    ensure_dirs,
    fetch_edinet_document_list,
    fetch_edinet_xbrl,
    normalize_text,
    parse_xbrl_to_edinet_record,
    record_event,
    setup_logging,
)


STOCK_CODE_ALIASES = ["銘柄", "入力値", "証券コード", "stock_code", "ticker", "code"]
BOTTOM_DATE_ALIASES = ["底打ち候補日", "底検知日", "候補日", "bottom_date", "signal_date"]

OUTPUT_COLUMNS = {
    "average_annual_salary": "底検知時_平均年間給与",
    "average_age": "底検知時_平均年齢",
    "average_length_of_service": "底検知時_平均勤続年数",
    "number_of_employees": "底検知時_従業員数",
    "net_sales": "底検知時_売上高",
    "operating_income": "底検知時_営業利益",
    "ordinary_income": "底検知時_経常利益",
    "net_income": "底検知時_当期利益",
    "total_assets": "底検知時_総資産",
    "net_assets": "底検知時_純資産",
    "liabilities": "底検知時_負債",
    "equity_ratio": "底検知時_自己資本比率",
    "roe": "底検知時_ROE",
    "operating_cash_flow": "底検知時_営業CF",
    "investing_cash_flow": "底検知時_投資CF",
    "financing_cash_flow": "底検知時_財務CF",
    "cash_and_equivalents": "底検知時_現金等",
    "rd_expenses": "底検知時_研究開発費",
    "capital_expenditures": "底検知時_設備投資額",
    "accounting_standard": "底検知時_会計基準",
    "property_info": "底検知時_設備状況テキスト",
}


class ApiUnavailableError(RuntimeError):
    pass


def make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_csv_flexible(path: Path) -> pd.DataFrame:
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"CSVを読み込めません: {path} ({last_error})")


def find_column(columns: list[str], aliases: list[str]) -> str:
    normalized = {normalize_text(column).lower(): column for column in columns}
    for alias in aliases:
        key = normalize_text(alias).lower()
        if key in normalized:
            return normalized[key]
    for column in columns:
        lower = normalize_text(column).lower()
        if any(normalize_text(alias).lower() in lower for alias in aliases):
            return column
    return ""


def parse_date(value: object) -> date | None:
    text = normalize_text(value)
    if not text or text in {"-", "未検出", "nan", "NaT"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def normalize_stock_code(value: object) -> tuple[str, str]:
    text = normalize_text(value).upper().strip()
    match = re.fullmatch(r"(\d{4})(?:\.T)?", text)
    if not match:
        return "", ""
    code4 = match.group(1)
    return code4, f"{code4}0"


def normalize_edinet_sec_code(value: object) -> str:
    digits = re.sub(r"\D", "", normalize_text(value))
    if len(digits) >= 5:
        return digits[:5]
    if len(digits) == 4:
        return f"{digits}0"
    return ""


def safe_to_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def date_range_desc(end_date: date, days: int) -> list[str]:
    return [(end_date - timedelta(days=offset)).isoformat() for offset in range(max(days, 1))]


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "計算中"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"約{hours}時間{minutes}分"
    if minutes:
        return f"約{minutes}分{sec}秒"
    return f"約{sec}秒"


def estimate_eta(started_at: float, completed_units: int, total_units: int) -> str:
    if completed_units <= 0 or total_units <= 0:
        return "計算中"
    elapsed = time.perf_counter() - started_at
    remaining_units = max(total_units - completed_units, 0)
    if remaining_units <= 0:
        return "まもなく完了"
    return format_duration(elapsed / completed_units * remaining_units)


def summarize_bottom_years(frame: pd.DataFrame, bottom_date_column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in frame[bottom_date_column].tolist():
        bottom = parse_date(value)
        if not bottom:
            counts["底検知日なし"] = counts.get("底検知日なし", 0) + 1
            continue
        key = str(bottom.year)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


class EdinetDateDocumentCache:
    def __init__(self, cache_dir: Path, ctx: RunContext, sleep_seconds: float) -> None:
        self.cache_dir = cache_dir
        self.ctx = ctx
        self.sleep_seconds = sleep_seconds
        self.memory: dict[str, pd.DataFrame] = {}
        self.network_fetch_count = 0
        self.cache_hit_count = 0
        self.consecutive_api_failure_count = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, date_text: str) -> pd.DataFrame:
        if date_text in self.memory:
            self.cache_hit_count += 1
            return self.memory[date_text]
        cache_path = self.cache_dir / f"documents_{date_text}.csv"
        if cache_path.exists():
            try:
                frame = pd.read_csv(cache_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
            except Exception:
                frame = pd.DataFrame()
            self.memory[date_text] = frame
            self.cache_hit_count += 1
            return frame

        error_count_before = len(self.ctx.errors)
        frame = fetch_edinet_document_list([date_text], self.ctx)
        new_errors = self.ctx.errors[error_count_before:]
        api_failed = any(error.get("event_type") in {"api_request_exception", "api_request_failed"} for error in new_errors)
        if api_failed:
            self.consecutive_api_failure_count += 1
        else:
            self.consecutive_api_failure_count = 0
        if self.consecutive_api_failure_count >= 3:
            raise ApiUnavailableError("EDINET APIへの通信失敗が連続しています。APIキー、ネットワーク、EDINET側の状態を確認してください。")
        if frame.empty:
            frame = pd.DataFrame(columns=["document_id", "sec_code", "filer_name", "report_submit_date", "submit_datetime"])
        if not api_failed:
            frame.to_csv(cache_path, index=False, encoding="utf-8-sig")
        self.memory[date_text] = frame
        self.network_fetch_count += 1
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return frame


def select_document_for_bottom_date(
    sec_code5: str,
    bottom_date: date,
    lookback_days: int,
    cache: EdinetDateDocumentCache,
    progress_label: str = "",
) -> dict[str, Any] | None:
    for day_index, date_text in enumerate(date_range_desc(bottom_date, lookback_days), start=1):
        if day_index == 1 or day_index % 30 == 0:
            label = f" {progress_label}" if progress_label else ""
            print(f"底検知EDINET日付探索中:{label} {day_index}/{lookback_days}日目 日付={date_text}", flush=True)
        documents = cache.get(date_text)
        if documents.empty or "sec_code" not in documents.columns:
            continue
        sec_codes = documents["sec_code"].map(normalize_edinet_sec_code)
        matches = documents[sec_codes == sec_code5].copy()
        if matches.empty:
            continue
        if "submit_datetime" in matches.columns:
            matches["_submit_sort"] = pd.to_datetime(matches["submit_datetime"], errors="coerce")
            matches = matches.sort_values("_submit_sort", ascending=False)
        elif "report_submit_date" in matches.columns:
            matches["_submit_sort"] = pd.to_datetime(matches["report_submit_date"], errors="coerce")
            matches = matches.sort_values("_submit_sort", ascending=False)
        return dict(matches.iloc[0].drop(labels=[label for label in ["_submit_sort"] if label in matches.columns]))
    return None


def build_required_date_set(
    frame: pd.DataFrame,
    stock_column: str,
    bottom_date_column: str,
    lookback_days: int,
) -> set[str]:
    required_dates: set[str] = set()
    for _, source_row in frame.iterrows():
        bottom = parse_date(source_row.get(bottom_date_column, ""))
        _, sec_code5 = normalize_stock_code(source_row.get(stock_column, ""))
        if not bottom or not sec_code5:
            continue
        for date_text in date_range_desc(bottom, lookback_days):
            required_dates.add(date_text)
    return required_dates


def build_document_index_for_rows(
    frame: pd.DataFrame,
    stock_column: str,
    bottom_date_column: str,
    lookback_days: int,
    cache: EdinetDateDocumentCache,
    total_row_units: int = 0,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    required_dates = build_required_date_set(frame, stock_column, bottom_date_column, lookback_days)
    date_list = sorted(required_dates, reverse=True)
    total_dates = len(date_list)
    total_units = max(total_dates + total_row_units, 1)
    started_at = time.perf_counter()
    print(f"底検知EDINET一括取得準備: 対象日付={total_dates}日 対象行={total_row_units}件", flush=True)

    index: dict[str, list[dict[str, Any]]] = {}
    fetched_documents = 0
    for date_index, date_text in enumerate(date_list, start=1):
        documents = cache.get(date_text)
        if not documents.empty:
            fetched_documents += len(documents)
            for _, doc_row in documents.iterrows():
                sec_code = normalize_edinet_sec_code(doc_row.get("sec_code"))
                if not sec_code:
                    continue
                index.setdefault(sec_code, []).append(dict(doc_row))
        if date_index == 1 or date_index % 10 == 0 or date_index == total_dates:
            overall_percent = date_index / total_units * 100
            eta = estimate_eta(started_at, date_index, total_units)
            print(
                f"底検知EDINET一括取得中: {date_index}/{total_dates}日目 日付={date_text} "
                f"取得済み書類={fetched_documents}件 対象証券コード={len(index)}件 "
                f"全体進捗={overall_percent:.1f}% 推定残り={eta}",
                flush=True,
            )

    for docs in index.values():
        docs.sort(
            key=lambda item: normalize_text(item.get("submit_datetime")) or normalize_text(item.get("report_submit_date")),
            reverse=True,
        )
    return index, {"required_date_count": total_dates, "indexed_document_count": fetched_documents, "indexed_sec_code_count": len(index)}


def select_document_from_index(
    document_index: dict[str, list[dict[str, Any]]],
    sec_code5: str,
    bottom_date: date,
    lookback_days: int,
) -> dict[str, Any] | None:
    lower_bound = bottom_date - timedelta(days=max(lookback_days - 1, 0))
    for document in document_index.get(sec_code5, []):
        submitted = parse_date(document.get("submit_datetime") or document.get("report_submit_date"))
        if not submitted:
            continue
        if lower_bound <= submitted <= bottom_date:
            return document
    return None


def print_financial_progress(
    row_index: int,
    total: int,
    counts: dict[str, int],
    started_at: float,
    completed_date_units: int,
) -> None:
    total_units = max(completed_date_units + total, 1)
    completed_units = min(completed_date_units + row_index, total_units)
    overall_percent = completed_units / total_units * 100
    eta = estimate_eta(started_at, completed_units, total_units)
    print(
        f"底検知EDINET財務取得状況: {row_index}/{total}件処理 "
        f"書類発見={counts.get('document_found', 0)}件 "
        f"必要データ入手={counts.get('xbrl_parsed', 0)}件 "
        f"未発見={counts.get('document_not_found', 0)}件 "
        f"エラー={counts.get('errors', 0)}件 "
        f"全体進捗={overall_percent:.1f}% 推定残り={eta}",
        flush=True,
    )


def append_blank_edinet_columns(row: dict[str, Any]) -> None:
    base_columns = [
        "底検知時_EDINET状態",
        "底検知時_底検知日",
        "底検知時_探索日数",
        "底検知時_EDINET提出日",
        "底検知時_EDINET対象期間終了日",
        "底検知時_EDINET書類ID",
        "底検知時_EDINET提出者名",
        "底検知時_EDINET証券コード",
        "底検知時_EDINET書類名",
        "底検知時_XBRL解析状態",
        "底検知時_XBRL取得項目",
        "底検知時_XBRL不足項目",
        "底検知時_メモ",
    ]
    for column in base_columns:
        row.setdefault(column, "")
    for column in OUTPUT_COLUMNS.values():
        row.setdefault(column, "")


def enrich_stock_bottom_rows(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    stock_path = Path(args.stock_file)
    output_dir = Path(args.output_dir)
    run_id = make_run_id()
    ctx = RunContext(run_id, output_dir)
    setup_logging(ctx)

    df = read_csv_flexible(stock_path)
    stock_column = find_column(list(df.columns), STOCK_CODE_ALIASES)
    bottom_date_column = find_column(list(df.columns), BOTTOM_DATE_ALIASES)
    if not stock_column:
        raise ValueError("銘柄列を見つけられません。'銘柄' または '入力値' 列が必要です。")
    if not bottom_date_column:
        raise ValueError("底検知日列を見つけられません。'底打ち候補日' 列が必要です。")

    if args.max_rows and args.max_rows > 0:
        work_df = df.head(args.max_rows).copy()
    else:
        work_df = df.copy()

    bottom_year_counts = summarize_bottom_years(work_df, bottom_date_column)
    bottom_year_summary = ", ".join(f"{year}年={count}件" if year != "底検知日なし" else f"{year}={count}件" for year, count in bottom_year_counts.items())
    print(f"底検知EDINET年別サマリー: {bottom_year_summary}", flush=True)

    cache = EdinetDateDocumentCache(output_dir / "edinet_document_cache", ctx, args.sleep_seconds)
    xbrl_cache_dir = output_dir / "edinet_xbrl_cache"
    xbrl_cache_dir.mkdir(parents=True, exist_ok=True)

    enriched_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "input_rows": len(work_df),
        "bottom_date_missing": 0,
        "stock_code_missing": 0,
        "document_not_found": 0,
        "document_found": 0,
        "xbrl_parsed": 0,
        "xbrl_not_parsed": 0,
        "errors": 0,
    }
    document_cache: dict[str, dict[str, Any]] = {}
    batch_api_error = ""
    document_index: dict[str, list[dict[str, Any]]] = {}
    batch_index_stats = {"required_date_count": 0, "indexed_document_count": 0, "indexed_sec_code_count": 0}
    progress_started_at = time.perf_counter()
    try:
        document_index, batch_index_stats = build_document_index_for_rows(
            work_df,
            stock_column,
            bottom_date_column,
            args.lookback_days,
            cache,
            len(work_df),
        )
    except ApiUnavailableError as exc:
        batch_api_error = str(exc)
        record_event(ctx, "ERROR", "stock_bottom_edinet_api_unavailable", "stock_bottom", batch_api_error)

    total = len(work_df)
    completed_date_units = int(batch_index_stats.get("required_date_count", 0) or 0)
    for row_index, (_, source_row) in enumerate(work_df.iterrows(), start=1):
        output_row = dict(source_row)
        append_blank_edinet_columns(output_row)
        bottom = parse_date(source_row.get(bottom_date_column, ""))
        code4, sec_code5 = normalize_stock_code(source_row.get(stock_column, ""))
        output_row["底検知時_底検知日"] = bottom.isoformat() if bottom else ""
        output_row["底検知時_探索日数"] = args.lookback_days

        if row_index == 1 or row_index % 10 == 0 or row_index == total:
            print(f"底検知EDINET結合中: {row_index}/{total}件目 銘柄={code4 or '-'} 底検知日={bottom or '-'}", flush=True)

        if not bottom:
            counts["bottom_date_missing"] += 1
            output_row["底検知時_EDINET状態"] = "bottom_date_missing"
            output_row["底検知時_メモ"] = "底打ち候補日が空、または日付として読めません。"
            enriched_rows.append(output_row)
            print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
            continue
        if not sec_code5:
            counts["stock_code_missing"] += 1
            output_row["底検知時_EDINET状態"] = "stock_code_missing"
            output_row["底検知時_メモ"] = "銘柄が4桁または4桁.T形式ではないため、EDINET対象外としてスキップしました。"
            enriched_rows.append(output_row)
            print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
            continue
        if batch_api_error:
            counts["errors"] += 1
            output_row["底検知時_EDINET状態"] = "api_unavailable"
            output_row["底検知時_メモ"] = batch_api_error
            enriched_rows.append(output_row)
            print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
            continue

        try:
            doc = select_document_from_index(document_index, sec_code5, bottom, args.lookback_days)
            if not doc:
                counts["document_not_found"] += 1
                output_row["底検知時_EDINET状態"] = "document_not_found"
                output_row["底検知時_メモ"] = f"底検知日以前{args.lookback_days}日以内に有価証券報告書を見つけられません。"
                enriched_rows.append(output_row)
                print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
                continue

            counts["document_found"] += 1
            document_id = normalize_text(doc.get("document_id"))
            corporate_number = normalize_text(doc.get("corporate_number"))
            output_row["底検知時_EDINET状態"] = "document_found"
            output_row["底検知時_EDINET提出日"] = normalize_text(doc.get("report_submit_date"))
            output_row["底検知時_EDINET対象期間終了日"] = normalize_text(doc.get("fiscal_year_end") or doc.get("period_end"))
            output_row["底検知時_EDINET書類ID"] = document_id
            output_row["底検知時_EDINET提出者名"] = normalize_text(doc.get("filer_name"))
            output_row["底検知時_EDINET証券コード"] = normalize_edinet_sec_code(doc.get("sec_code"))
            output_row["底検知時_EDINET書類名"] = normalize_text(doc.get("doc_description"))

            if document_id in document_cache:
                values = document_cache[document_id]
            else:
                xbrl_cache_path = xbrl_cache_dir / f"{document_id}.zip"
                if xbrl_cache_path.exists() and xbrl_cache_path.stat().st_size > 0:
                    xbrl_bytes = xbrl_cache_path.read_bytes()
                else:
                    xbrl_bytes = fetch_edinet_xbrl(document_id, ctx)
                    if xbrl_bytes:
                        xbrl_cache_path.write_bytes(xbrl_bytes)
                    if args.sleep_seconds > 0:
                        time.sleep(args.sleep_seconds)
                values = parse_xbrl_to_edinet_record(xbrl_bytes, corporate_number, ctx)
                document_cache[document_id] = values

            if not values:
                counts["xbrl_not_parsed"] += 1
                output_row["底検知時_EDINET状態"] = "xbrl_not_parsed"
                output_row["底検知時_XBRL解析状態"] = "not_parsed"
                output_row["底検知時_メモ"] = "書類は見つかりましたが、XBRLを取得または解析できませんでした。"
                enriched_rows.append(output_row)
                print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
                continue

            counts["xbrl_parsed"] += 1
            output_row["底検知時_EDINET状態"] = "xbrl_parsed"
            output_row["底検知時_XBRL解析状態"] = "parsed"
            output_row["底検知時_XBRL取得項目"] = normalize_text(values.get("_xbrl_extracted_fields"))
            output_row["底検知時_XBRL不足項目"] = normalize_text(values.get("_xbrl_missing_fields"))
            for source_key, output_key in OUTPUT_COLUMNS.items():
                output_row[output_key] = values.get(source_key, "")
            enriched_rows.append(output_row)
            print_financial_progress(row_index, total, counts, progress_started_at, completed_date_units)
        except ApiUnavailableError as exc:
            counts["errors"] += 1
            output_row["底検知時_EDINET状態"] = "api_unavailable"
            output_row["底検知時_メモ"] = str(exc)
            enriched_rows.append(output_row)
            for _, remaining_row in work_df.iloc[row_index:].iterrows():
                skipped_row = dict(remaining_row)
                append_blank_edinet_columns(skipped_row)
                skipped_bottom = parse_date(remaining_row.get(bottom_date_column, ""))
                skipped_row["底検知時_底検知日"] = skipped_bottom.isoformat() if skipped_bottom else ""
                skipped_row["底検知時_探索日数"] = args.lookback_days
                skipped_row["底検知時_EDINET状態"] = "api_unavailable"
                skipped_row["底検知時_メモ"] = str(exc)
                enriched_rows.append(skipped_row)
            counts["errors"] += max(total - row_index, 0)
            record_event(ctx, "ERROR", "stock_bottom_edinet_api_unavailable", "stock_bottom", str(exc))
            break
        except Exception as exc:
            counts["errors"] += 1
            output_row["底検知時_EDINET状態"] = "error"
            output_row["底検知時_メモ"] = f"{type(exc).__name__}: {exc}"
            record_event(ctx, "ERROR", "stock_bottom_enrichment_row_failed", "stock_bottom", str(exc), stock_code=code4)
            enriched_rows.append(output_row)

    result = pd.DataFrame(enriched_rows)
    report = {
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "run_id": run_id,
        "stock_file": str(stock_path),
        "stock_column": stock_column,
        "bottom_date_column": bottom_date_column,
        "lookback_days": args.lookback_days,
        "bottom_year_counts": bottom_year_counts,
        "counts": counts,
        "edinet_document_list_network_fetch_count": cache.network_fetch_count,
        "edinet_document_list_cache_hit_count": cache.cache_hit_count,
        "unique_xbrl_document_count": len(document_cache),
        "batch_required_date_count": batch_index_stats.get("required_date_count", 0),
        "batch_indexed_document_count": batch_index_stats.get("indexed_document_count", 0),
        "batch_indexed_sec_code_count": batch_index_stats.get("indexed_sec_code_count", 0),
        "batch_api_error": batch_api_error,
    }
    return result, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="株の底検知CSVに、底検知日時点で利用可能だったEDINET財務データを追加します。")
    parser.add_argument("--stock-file", required=True, help="底検知アプリの出力CSV")
    parser.add_argument("--output-dir", default="outputs/stock_bottom_financial_enrichment", help="出力フォルダ")
    parser.add_argument("--lookback-days", type=int, default=460, help="底検知日から何日前まで有価証券報告書を探すか")
    parser.add_argument("--max-rows", type=int, default=0, help="テスト用の最大処理行数。0なら全件")
    parser.add_argument("--sleep-seconds", type=float, default=0.15, help="EDINET APIへの連続アクセス間隔")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result, report = enrich_stock_bottom_rows(args)
    except Exception as exc:
        error_report = {
            "app_version": APP_VERSION,
            "app_build_date": APP_BUILD_DATE,
            "error": f"{type(exc).__name__}: {exc}",
        }
        (output_dir / "stock_bottom_financial_enrichment_report.json").write_text(
            json.dumps(error_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"底検知EDINET結合に失敗しました: {exc}", flush=True)
        return 1

    output_csv = output_dir / "stock_bottom_with_edinet_financials.csv"
    result.to_csv(output_csv, index=False, encoding="utf-8-sig")
    report["outputs"] = {"stock_bottom_with_edinet_financials_csv": str(output_csv)}
    report_path = output_dir / "stock_bottom_financial_enrichment_report.json"
    report_path.write_text(
        json.dumps({key: safe_to_json(value) for key, value in report.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("底検知EDINET結合が完了しました。", flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
