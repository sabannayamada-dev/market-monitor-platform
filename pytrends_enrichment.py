from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from app_meta import APP_BUILD_DATE, APP_NAME, APP_VERSION


COMPANY_NAME_ALIASES = ["company_name", "企業名", "会社名", "名称", "filer_name", "提出者名"]
SECURITY_CODE_ALIASES = ["sec_code", "security_code", "証券コード", "証券コード協議会コード", "銘柄", "ticker", "symbol"]
OUTPUT_PREFIX = "GoogleTrends_"


@dataclass
class TrendResult:
    query: str
    status: str
    error: str = ""
    points: int = 0
    latest_date: str = ""
    latest_value: float | None = None
    recent_4_period_avg: float | None = None
    previous_4_period_avg: float | None = None
    recent_change_pct: float | None = None
    recent_12_period_avg: float | None = None
    max_value: float | None = None
    peak_date: str = ""
    latest_vs_peak_ratio: float | None = None
    recent_slope: float | None = None
    recent_zscore: float | None = None
    zero_ratio: float | None = None
    is_partial_latest: bool = False
    cache_used: bool = False


@dataclass
class CompanyAlias:
    cache_key: str
    common_name: str
    confidence: float = 0.0
    status: str = "ok"
    error: str = ""
    cache_used: bool = False


def detect_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            pd.read_csv(path, encoding=encoding, nrows=2, dtype=str)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def read_csv_flexible(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding=detect_encoding(path), dtype=str, keep_default_na=False, low_memory=False)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_column(columns: list[str], aliases: list[str]) -> str | None:
    exact = {str(column).strip().lower(): str(column) for column in columns}
    for alias in aliases:
        found = exact.get(alias.strip().lower())
        if found:
            return found
    for column in columns:
        normalized = str(column).strip().lower()
        if any(alias.strip().lower() in normalized for alias in aliases):
            return str(column)
    return None


def normalize_company_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    for token in ("株式会社", "有限会社", "合同会社", "（株）", "(株)", "㈱"):
        text = text.replace(token, "")
    return text.strip(" 　・-/／")


def normalize_security_code(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return ""
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"\.0$", "", text)
    if text.endswith(".T"):
        text = text[:-2]
    # EDINET-style five-digit codes commonly append zero to the exchange code.
    if re.fullmatch(r"\d{5}", text) and text.endswith("0"):
        text = text[:-1]
    return text


def build_query(company_name: object, suffix: str, security_code: object = None) -> str:
    code = normalize_security_code(security_code)
    company = normalize_company_name(company_name)
    suffix = unicodedata.normalize("NFKC", suffix or "").strip()
    search_key = code or company
    if not search_key:
        return ""
    return f"{search_key} {suffix}".strip()


def company_alias_key(company_name: object, security_code: object = None) -> str:
    code = normalize_security_code(security_code)
    if code:
        return f"security:{code}"
    company = normalize_company_name(company_name)
    return f"company:{company.casefold()}" if company else ""


def clean_common_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?:の)?株価$", "", text, flags=re.IGNORECASE).strip()
    if not 1 < len(text) <= 80 or "\n" in text or "\r" in text:
        return ""
    return text


class CompanyAliasCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS company_alias_cache (
                cache_key TEXT PRIMARY KEY,
                security_code TEXT NOT NULL,
                source_company_name TEXT NOT NULL,
                common_name TEXT NOT NULL,
                confidence REAL NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def get(self, cache_key: str) -> CompanyAlias | None:
        row = self.connection.execute(
            "SELECT common_name, confidence, status, error FROM company_alias_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row or str(row[2]) != "ok" or not clean_common_name(row[0]):
            return None
        return CompanyAlias(
            cache_key=cache_key,
            common_name=clean_common_name(row[0]),
            confidence=float(row[1] or 0.0),
            status="ok",
            error="",
            cache_used=True,
        )

    def put(
        self,
        result: CompanyAlias,
        security_code: str,
        source_company_name: str,
        model: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO company_alias_cache(
                cache_key, security_code, source_company_name, common_name,
                confidence, model, status, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                security_code=excluded.security_code,
                source_company_name=excluded.source_company_name,
                common_name=excluded.common_name,
                confidence=excluded.confidence,
                model=excluded.model,
                status=excluded.status,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                result.cache_key,
                security_code,
                source_company_name,
                result.common_name,
                float(result.confidence),
                model,
                result.status,
                result.error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class OpenAICompanyAliasResolver:
    def __init__(self, api_key: str, model: str, timeout: int = 45) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "gpt-5-nano-2025-08-07"
        self.timeout = timeout

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if exc.code == 401:
            return RuntimeError("OpenAI APIキーが無効です")
        if exc.code == 429:
            return RuntimeError(f"OpenAI APIの利用上限またはレート制限です: {body[:500]}")
        return RuntimeError(f"OpenAI API HTTP {exc.code}: {body[:800]}")

    def resolve(self, cache_key: str, security_code: str, company_name: str) -> CompanyAlias:
        prompt = (
            "次の上場銘柄について、日本国内でGoogle検索するときに一般的に使われる、"
            "企業を一意に識別しやすい短い呼称を1つ決めてください。"
            "日本企業は日本語の一般名称、海外企業は日本で一般的なブランド名またはティッカーを優先します。"
            "『株価』という語は含めず、曖昧すぎる普通名詞は避けてください。\n"
            f"証券コード: {security_code or '不明'}\n元データの企業名: {company_name or '不明'}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "あなたは日本の上場企業名と検索呼称の名寄せ担当です。推測を広げず簡潔に回答してください。",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "company_common_name",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "common_name": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["common_name", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "reasoning_effort": "minimal",
            "max_completion_tokens": 512,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        output = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not output:
            raise RuntimeError("OpenAIから企業呼称を取得できませんでした")
        parsed = json.loads(output)
        common_name = clean_common_name(parsed.get("common_name"))
        if not common_name:
            raise RuntimeError("OpenAIが返した企業呼称が空または不正です")
        return CompanyAlias(
            cache_key=cache_key,
            common_name=common_name,
            confidence=float(parsed.get("confidence", 0.0) or 0.0),
        )


def timeframe_from_months(months: int, now: datetime | None = None) -> str:
    now = now or datetime.now()
    start = (now - timedelta(days=max(months, 1) * 31)).date().isoformat()
    return f"{start} {now.date().isoformat()}"


class TrendCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trend_cache (
                cache_key TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                geo TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def key(query: str, timeframe: str, geo: str) -> str:
        raw = json.dumps([query, timeframe, geo], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, query: str, cache_scope: str, geo: str, max_age_days: int) -> pd.DataFrame | None:
        row = self.connection.execute(
            "SELECT fetched_at, payload_json FROM trend_cache WHERE cache_key = ?",
            (self.key(query, cache_scope, geo),),
        ).fetchone()
        if not row:
            return None
        fetched_at = datetime.fromisoformat(str(row[0]))
        if datetime.now(timezone.utc) - fetched_at > timedelta(days=max(max_age_days, 0)):
            return None
        records = json.loads(str(row[1]))
        frame = pd.DataFrame(records)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        return frame

    def put(self, query: str, cache_scope: str, geo: str, frame: pd.DataFrame) -> None:
        serializable = frame.copy()
        serializable["date"] = pd.to_datetime(serializable["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        payload = serializable.where(pd.notna(serializable), None).to_dict(orient="records")
        self.connection.execute(
            """
            INSERT INTO trend_cache(cache_key, query, timeframe, geo, fetched_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                payload_json=excluded.payload_json
            """,
            (
                self.key(query, cache_scope, geo),
                query,
                cache_scope,
                geo,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def _finite(value: float | int | np.floating[Any] | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def summarize_series(query: str, frame: pd.DataFrame, cache_used: bool = False) -> TrendResult:
    if frame.empty or "value" not in frame.columns:
        return TrendResult(query=query, status="no_data", error="Google Trendsの時系列が空です", cache_used=cache_used)
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["value"] = pd.to_numeric(work["value"], errors="coerce")
    work = work.dropna(subset=["date", "value"]).sort_values("date")
    if work.empty:
        return TrendResult(query=query, status="no_data", error="有効な時系列値がありません", cache_used=cache_used)
    is_partial = bool(work.iloc[-1].get("isPartial", False))
    complete = work.iloc[:-1] if is_partial and len(work) > 1 else work
    values = complete["value"].astype(float)
    if values.empty:
        return TrendResult(query=query, status="no_data", error="確定済み時系列値がありません", cache_used=cache_used)
    recent4 = values.tail(4)
    previous4 = values.iloc[max(0, len(values) - 8) : max(0, len(values) - 4)]
    recent12 = values.tail(12)
    latest = float(values.iloc[-1])
    recent_avg = float(recent4.mean())
    previous_avg = float(previous4.mean()) if not previous4.empty else float("nan")
    change_pct = ((recent_avg / previous_avg) - 1.0) * 100.0 if previous_avg > 0 else float("nan")
    max_value = float(values.max())
    peak_index = values.idxmax()
    x = np.arange(len(recent4), dtype=float)
    slope = float(np.polyfit(x, recent4.to_numpy(dtype=float), 1)[0]) if len(recent4) >= 2 else float("nan")
    baseline = values.iloc[:-1].tail(52)
    baseline_std = float(baseline.std(ddof=0)) if len(baseline) >= 2 else float("nan")
    zscore = (latest - float(baseline.mean())) / baseline_std if baseline_std > 0 else float("nan")
    return TrendResult(
        query=query,
        status="ok",
        points=int(len(values)),
        latest_date=complete.iloc[-1]["date"].date().isoformat(),
        latest_value=_finite(latest),
        recent_4_period_avg=_finite(recent_avg),
        previous_4_period_avg=_finite(previous_avg),
        recent_change_pct=_finite(change_pct),
        recent_12_period_avg=_finite(float(recent12.mean())),
        max_value=_finite(max_value),
        peak_date=complete.loc[peak_index, "date"].date().isoformat(),
        latest_vs_peak_ratio=_finite(latest / max_value if max_value > 0 else float("nan")),
        recent_slope=_finite(slope),
        recent_zscore=_finite(zscore),
        zero_ratio=_finite(float((values == 0).mean())),
        is_partial_latest=is_partial,
        cache_used=cache_used,
    )


def fetch_trend_frame(client: Any, query: str, timeframe: str, geo: str) -> pd.DataFrame:
    client.build_payload([query], cat=0, timeframe=timeframe, geo=geo, gprop="")
    raw = client.interest_over_time()
    if raw.empty or query not in raw.columns:
        return pd.DataFrame(columns=["date", "value", "isPartial"])
    result = raw.reset_index().rename(columns={query: "value"})
    if "date" not in result.columns:
        result = result.rename(columns={result.columns[0]: "date"})
    if "isPartial" not in result.columns:
        result["isPartial"] = False
    return result[["date", "value", "isPartial"]]


def result_columns() -> dict[str, str]:
    return {
        "query": f"{OUTPUT_PREFIX}検索語",
        "status": f"{OUTPUT_PREFIX}取得状態",
        "error": f"{OUTPUT_PREFIX}失敗理由",
        "points": f"{OUTPUT_PREFIX}時系列点数",
        "latest_date": f"{OUTPUT_PREFIX}最新確定日",
        "latest_value": f"{OUTPUT_PREFIX}最新指数_企業内相対0_100",
        "recent_4_period_avg": f"{OUTPUT_PREFIX}直近4期間平均_企業内相対0_100",
        "previous_4_period_avg": f"{OUTPUT_PREFIX}前4期間平均_企業内相対0_100",
        "recent_change_pct": f"{OUTPUT_PREFIX}直近4期間前期比_pct",
        "recent_12_period_avg": f"{OUTPUT_PREFIX}直近12期間平均_企業内相対0_100",
        "max_value": f"{OUTPUT_PREFIX}期間内最大指数_企業内相対0_100",
        "peak_date": f"{OUTPUT_PREFIX}検索ピーク日",
        "latest_vs_peak_ratio": f"{OUTPUT_PREFIX}最新対ピーク比率",
        "recent_slope": f"{OUTPUT_PREFIX}直近4期間傾き",
        "recent_zscore": f"{OUTPUT_PREFIX}最新異常度Z",
        "zero_ratio": f"{OUTPUT_PREFIX}ゼロ比率",
        "is_partial_latest": f"{OUTPUT_PREFIX}最新期間暫定フラグ",
        "cache_used": f"{OUTPUT_PREFIX}キャッシュ利用",
    }


def enrich(
    company_file: Path,
    output_dir: Path,
    months: int,
    geo: str,
    suffix: str,
    max_companies: int,
    delay_seconds: float,
    cache_days: int,
    retries: int,
    openai_api_key: str = "",
    alias_model: str = "gpt-5-nano-2025-08-07",
    alias_retries: int = 2,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    try:
        from pytrends.request import TrendReq
    except ImportError as exc:
        raise RuntimeError("pytrendsが未導入です。pip install -r requirements.txt を実行してください") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    companies = read_csv_flexible(company_file)
    company_column = find_column(list(companies.columns), COMPANY_NAME_ALIASES)
    if not company_column:
        raise ValueError("企業名列が見つかりません。company_name / 企業名 / 会社名のいずれかが必要です")
    security_column = find_column(list(companies.columns), SECURITY_CODE_ALIASES)
    existing = [column for column in companies.columns if str(column).startswith(OUTPUT_PREFIX)]
    if existing:
        companies = companies.drop(columns=existing)

    row_alias_keys: list[str] = []
    row_company_names: list[str] = []
    row_security_codes: list[str] = []
    company_records: dict[str, tuple[str, str]] = {}
    for _, row in companies.iterrows():
        company_value = str(row.get(company_column, "") or "").strip()
        security_value = row.get(security_column, "") if security_column else ""
        normalized_code = normalize_security_code(security_value)
        alias_key = company_alias_key(company_value, security_value)
        row_alias_keys.append(alias_key)
        row_company_names.append(company_value)
        row_security_codes.append(normalized_code)
        if alias_key and alias_key not in company_records:
            company_records[alias_key] = (normalized_code, company_value)

    target_keys = list(company_records)
    if max_companies > 0:
        target_keys = target_keys[:max_companies]
    target_key_set = set(target_keys)
    alias_cache_path = output_dir / "company_alias_cache.sqlite3"
    alias_cache = CompanyAliasCache(alias_cache_path)
    alias_resolver = OpenAICompanyAliasResolver(openai_api_key, alias_model)
    aliases: dict[str, CompanyAlias] = {}
    alias_api_count = 0
    alias_cache_count = 0
    alias_error_count = 0
    try:
        for number, alias_key in enumerate(target_keys, start=1):
            security_code, company_name = company_records[alias_key]
            cached_alias = alias_cache.get(alias_key)
            if cached_alias is not None:
                aliases[alias_key] = cached_alias
                alias_cache_count += 1
                progress(
                    f"GPT企業呼称: {number}/{len(target_keys)}件 キャッシュ利用 "
                    f"{security_code or company_name} -> {cached_alias.common_name}"
                )
                continue
            if not openai_api_key.strip():
                raise RuntimeError(
                    "未登録企業の一般呼称を初回取得するためOpenAI APIキーが必要です。"
                    "特許モニターで暗号化保存するか、OPENAI_API_KEYを設定してください"
                )
            progress(f"GPT企業呼称取得中: {number}/{len(target_keys)}件 {security_code or company_name}")
            last_error = ""
            resolved: CompanyAlias | None = None
            for attempt in range(max(alias_retries, 0) + 1):
                try:
                    alias_api_count += 1
                    resolved = alias_resolver.resolve(alias_key, security_code, company_name)
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < alias_retries:
                        wait = min(60.0, 3.0 * (2**attempt))
                        progress(f"GPT企業呼称再試行: {wait:.1f}秒 / 理由={last_error}")
                        time.sleep(wait)
            if resolved is None:
                alias_error_count += 1
                resolved = CompanyAlias(
                    cache_key=alias_key,
                    common_name="",
                    status="error",
                    error=last_error or "企業呼称を取得できませんでした",
                )
            else:
                alias_cache.put(resolved, security_code, company_name, alias_model)
            aliases[alias_key] = resolved
    finally:
        alias_cache.close()

    alias_name_counts: dict[str, int] = {}
    for result in aliases.values():
        if result.status == "ok":
            normalized = result.common_name.casefold()
            alias_name_counts[normalized] = alias_name_counts.get(normalized, 0) + 1
    key_to_query: dict[str, str] = {}
    for alias_key, result in aliases.items():
        if result.status != "ok":
            continue
        security_code, _ = company_records[alias_key]
        search_name = result.common_name
        if alias_name_counts.get(search_name.casefold(), 0) > 1 and security_code:
            search_name = f"{search_name} {security_code}"
        key_to_query[alias_key] = f"{search_name} {suffix}".strip()

    query_values: list[str] = []
    query_modes: list[str] = []
    query_sources: list[str] = []
    alias_statuses: list[str] = []
    alias_cache_flags: list[bool] = []
    alias_confidences: list[float | None] = []
    for alias_key, company_name, security_code in zip(
        row_alias_keys, row_company_names, row_security_codes, strict=True
    ):
        alias = aliases.get(alias_key)
        if alias_key in target_key_set and alias and alias.status == "ok":
            query_values.append(key_to_query[alias_key])
            query_modes.append("gpt_common_name")
            query_sources.append(alias.common_name)
            alias_statuses.append("cached" if alias.cache_used else "api_resolved")
            alias_cache_flags.append(alias.cache_used)
            alias_confidences.append(alias.confidence)
        elif alias_key in target_key_set and alias:
            query_values.append("")
            query_modes.append("gpt_alias_error")
            query_sources.append("")
            alias_statuses.append("error")
            alias_cache_flags.append(False)
            alias_confidences.append(None)
        else:
            query_values.append(build_query(company_name, suffix, security_code))
            query_modes.append("not_processed" if alias_key else "missing")
            query_sources.append(security_code or normalize_company_name(company_name))
            alias_statuses.append("not_processed" if alias_key else "missing")
            alias_cache_flags.append(False)
            alias_confidences.append(None)
    queries = pd.Series(query_values, index=companies.index, dtype="object")
    unique_queries = list(dict.fromkeys(key_to_query[key] for key in target_keys if key in key_to_query))
    timeframe = timeframe_from_months(months)
    cache_scope = f"months={months}"
    cache = TrendCache(output_dir / "pytrends_cache.sqlite3")
    client = TrendReq(hl="ja-JP", tz=-540, timeout=(10, 30), retries=0, backoff_factor=0)
    results: dict[str, TrendResult] = {}
    time_series: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    try:
        for number, query in enumerate(unique_queries, start=1):
            progress(f"Google Trends取得中: {number}/{len(unique_queries)}件 検索語={query}")
            cached = cache.get(query, cache_scope, geo, cache_days)
            frame: pd.DataFrame | None = cached
            cache_used = cached is not None
            error = ""
            if frame is None:
                for attempt in range(max(retries, 0) + 1):
                    try:
                        frame = fetch_trend_frame(client, query, timeframe, geo)
                        cache.put(query, cache_scope, geo, frame)
                        break
                    except Exception as exc:  # pytrends exposes several requests exceptions.
                        error = f"{type(exc).__name__}: {exc}"
                        if attempt < retries:
                            wait = 60.0 * (attempt + 1) if "429" in error else max(delay_seconds, 5.0) * (2 ** attempt)
                            progress(f"Google Trends再試行待ち: {wait:.1f}秒 / 理由={error}")
                            time.sleep(wait)
                if frame is None:
                    result = TrendResult(query=query, status="error", error=error)
                    results[query] = result
                    errors.append({"query": query, "error": error})
                    continue
            result = summarize_series(query, frame, cache_used=cache_used)
            results[query] = result
            if result.status != "ok":
                errors.append({"query": query, "error": result.error})
            if not frame.empty:
                series = frame.copy()
                series.insert(0, "検索語", query)
                time_series.append(series)
            if not cache_used and delay_seconds > 0:
                time.sleep(delay_seconds + random.uniform(0.0, delay_seconds * 0.5))
    finally:
        cache.close()

    output_rows = []
    for query, alias_key in zip(queries, row_alias_keys, strict=True):
        alias = aliases.get(alias_key)
        if not alias_key:
            output_rows.append(asdict(TrendResult(query="", status="missing_company_name", error="企業キーが空です")))
        elif alias_key not in target_key_set:
            output_rows.append(asdict(TrendResult(query=query, status="not_processed", error="最大取得件数の対象外です")))
        elif alias is None or alias.status != "ok":
            error = alias.error if alias else "GPT企業呼称を取得できませんでした"
            output_rows.append(asdict(TrendResult(query="", status="alias_error", error=error)))
        elif query not in results:
            output_rows.append(asdict(TrendResult(query=query, status="error", error="検索結果がありません")))
        else:
            output_rows.append(asdict(results[query]))
    result_frame = pd.DataFrame(output_rows).rename(columns=result_columns())
    query_metadata = pd.DataFrame(
        {
            f"{OUTPUT_PREFIX}検索方式": query_modes,
            f"{OUTPUT_PREFIX}検索元値": query_sources,
            f"{OUTPUT_PREFIX}GPT呼称状態": alias_statuses,
            f"{OUTPUT_PREFIX}GPT呼称キャッシュ利用": alias_cache_flags,
            f"{OUTPUT_PREFIX}GPT呼称信頼度": alias_confidences,
        }
    )
    enriched = pd.concat(
        [companies.reset_index(drop=True), query_metadata.reset_index(drop=True), result_frame.reset_index(drop=True)],
        axis=1,
    )
    enriched_path = output_dir / "companies_with_google_trends.csv"
    series_path = output_dir / "google_trends_time_series.csv"
    errors_path = output_dir / "google_trends_errors.csv"
    aliases_path = output_dir / "company_search_aliases.csv"
    report_path = output_dir / "pytrends_enrichment_report.json"
    enriched.to_csv(enriched_path, index=False, encoding="utf-8-sig")
    alias_rows = []
    for alias_key in target_keys:
        security_code, company_name = company_records[alias_key]
        alias = aliases.get(alias_key)
        alias_rows.append(
            {
                "企業キー": alias_key,
                "証券コード": security_code,
                "元企業名": company_name,
                "Google検索用呼称": alias.common_name if alias else "",
                "呼称信頼度": alias.confidence if alias else None,
                "取得方法": "保存済み" if alias and alias.cache_used else "GPT API",
                "状態": alias.status if alias else "missing",
                "エラー": alias.error if alias else "呼称結果がありません",
            }
        )
    pd.DataFrame(alias_rows).to_csv(aliases_path, index=False, encoding="utf-8-sig")
    if time_series:
        pd.concat(time_series, ignore_index=True).to_csv(series_path, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame(columns=["検索語", "date", "value", "isPartial"]).to_csv(series_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(errors, columns=["query", "error"]).to_csv(errors_path, index=False, encoding="utf-8-sig")

    ok_count = sum(result.status == "ok" for result in results.values())
    no_data_count = sum(result.status == "no_data" for result in results.values())
    error_count = sum(result.status == "error" for result in results.values())
    cache_count = sum(result.cache_used for result in results.values())
    report = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "company_file": str(company_file.resolve()),
        "company_file_sha256": file_sha256(company_file),
        "company_file_size": int(company_file.stat().st_size),
        "company_file_modified_at": datetime.fromtimestamp(company_file.stat().st_mtime).isoformat(timespec="seconds"),
        "company_input_count": int(len(companies)),
        "company_name_column": company_column,
        "security_code_column": security_column or "",
        "company_preview": companies[company_column].head(5).astype(str).tolist(),
        "query_strategy": "gpt_common_name_with_persistent_alias_cache",
        "alias_model": alias_model,
        "alias_target_count": len(target_keys),
        "alias_api_count": alias_api_count,
        "alias_cache_used_count": alias_cache_count,
        "alias_error_count": alias_error_count,
        "query_suffix": suffix,
        "geo": geo,
        "timeframe": timeframe,
        "requested_months": months,
        "unique_query_count": len(unique_queries),
        "success_count": ok_count,
        "no_data_count": no_data_count,
        "error_count": error_count,
        "cache_used_count": cache_count,
        "success_rate": ok_count / len(unique_queries) if unique_queries else 0.0,
        "warning": "Google Trends指数は検索語ごとに0〜100へ正規化された相対値です。企業間の絶対検索量比較には使えません。pytrendsは非公式クライアントであり、Google側の変更やレート制限で失敗する場合があります。",
        "outputs": {
            "companies_with_google_trends_csv": str(enriched_path.resolve()),
            "google_trends_time_series_csv": str(series_path.resolve()),
            "google_trends_errors_csv": str(errors_path.resolve()),
            "company_search_aliases_csv": str(aliases_path.resolve()),
            "cache_db": str((output_dir / "pytrends_cache.sqlite3").resolve()),
            "company_alias_cache_db": str(alias_cache_path.resolve()),
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(
        f"Google Trends結合完了: 対象={len(unique_queries)} 成功={ok_count} "
        f"データなし={no_data_count} エラー={error_count} キャッシュ={cache_count}"
    )
    print(json.dumps(report, ensure_ascii=False))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="企業CSVへGoogle Trendsの時系列要約を結合します")
    parser.add_argument("--company-file", required=True)
    parser.add_argument("--output-dir", default="outputs/pytrends_enrichment")
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--geo", default="JP")
    parser.add_argument("--query-suffix", default="株価")
    parser.add_argument("--max-companies", type=int, default=100, help="0なら全件。pytrendsでは少数での検証を推奨")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--cache-days", type=int, default=7)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--alias-model", default="gpt-5-nano-2025-08-07")
    parser.add_argument("--alias-retries", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # A failed rerun must not leave the previous success report looking current.
    for stale_name in ("pytrends_enrichment_report.json", "pytrends_failure.json"):
        (output_dir / stale_name).unlink(missing_ok=True)
    try:
        enrich(
            company_file=Path(args.company_file),
            output_dir=Path(args.output_dir),
            months=max(args.months, 1),
            geo=str(args.geo).strip().upper() or "JP",
            suffix=str(args.query_suffix),
            max_companies=max(args.max_companies, 0),
            delay_seconds=max(args.delay_seconds, 0.0),
            cache_days=max(args.cache_days, 0),
            retries=max(args.retries, 0),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            alias_model=str(args.alias_model).strip() or "gpt-5-nano-2025-08-07",
            alias_retries=max(args.alias_retries, 0),
        )
        return 0
    except Exception as exc:
        failure_path = output_dir / "pytrends_failure.json"
        failure = {
            "app_version": APP_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "python_executable": sys.executable,
            "company_file": str(args.company_file),
        }
        failure_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        print(f"failure_report: {failure_path.resolve()}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
