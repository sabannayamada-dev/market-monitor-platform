from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
import threading
import time
from urllib.parse import quote
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from sec_financial_enrichment import (
    ALLOWED_FORMS,
    ANNUAL_FORMS,
    APP_VERSION as SEC_APP_VERSION,
    METRICS,
    SecClient,
    _duration_days,
    _valid_fact_rows,
    extract_point_in_time_financials,
    parse_date,
    select_fact,
)


APP_VERSION = "0.3.8"
ALGORITHM_VERSION_EXPECTED = "5.3-long-general-history"
DEFAULT_LEGACY_APP = Path(r"C:\Users\saban\Documents\Codex\2026-06-23\python-csv\app.py")
DEFAULT_OUTPUT_DIR = Path("outputs/us_stock_research")
SUPPORTED_EXCHANGES = {"Nasdaq", "NYSE", "NYSE American", "Cboe BZX"}
CHANGE_METRIC_KEYS = {
    "revenue", "gross_profit", "cost_of_revenue", "operating_income", "net_income", "assets",
    "liabilities", "equity", "cash", "operating_cf", "capex", "rd",
    "inventory", "receivables", "long_term_debt", "shares", "eps",
}


ProgressCallback = Callable[[dict[str, Any]], None]


def emit(progress: ProgressCallback | None, **payload: Any) -> None:
    if progress:
        progress(payload)


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def atomic_csv_write(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def resilient_csv_write(frame: pd.DataFrame, path: Path) -> Path:
    """Write atomically, falling back to a timestamped file if Excel locks the target."""
    try:
        atomic_csv_write(frame, path)
        return path
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{timestamp}{path.suffix}")
        atomic_csv_write(frame, fallback)
        return fallback


def load_yfinance() -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "yfinanceがありません。Anaconda Promptで "
            "pip install -r requirements_us_stock_research.txt を実行してください。"
        ) from exc
    return yf


def load_legacy_algorithm(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"従来の底検知app.pyが見つかりません: {path}")
    spec = importlib.util.spec_from_file_location("legacy_bottom_algorithm", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"底検知アルゴリズムを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = ["PricePoint", "analyze_bottoming", "ALGORITHM_VERSION"]
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"従来app.pyに必要な機能がありません: {', '.join(missing)}")
    return module


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS prices (
                ticker TEXT NOT NULL,
                price_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL NOT NULL,
                volume REAL,
                PRIMARY KEY (ticker, price_date)
            );
            CREATE INDEX IF NOT EXISTS idx_prices_ticker_date
            ON prices(ticker, price_date);
            CREATE TABLE IF NOT EXISTS symbols (
                ticker TEXT PRIMARY KEY,
                company_name TEXT,
                exchange TEXT,
                cik TEXT,
                median_dollar_volume REAL,
                last_price REAL,
                price_start TEXT,
                price_end TEXT,
                updated_at TEXT
            );
            """
        )


def save_prices(db_path: Path, ticker: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    rows = []
    for timestamp, row in frame.iterrows():
        close = pd.to_numeric(pd.Series([row.get("Close")]), errors="coerce").iloc[0]
        if pd.isna(close) or float(close) <= 0:
            continue
        rows.append((
            ticker, pd.Timestamp(timestamp).date().isoformat(),
            _float_or_none(row.get("Open")), _float_or_none(row.get("High")),
            _float_or_none(row.get("Low")), float(close), _float_or_none(row.get("Volume")),
        ))
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO prices(ticker,price_date,open,high,low,close,volume)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(ticker,price_date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume
            """,
            rows,
        )


def load_prices(db_path: Path, ticker: str, start_date: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as connection:
        frame = pd.read_sql_query(
            """SELECT price_date,open,high,low,close,volume FROM prices
               WHERE ticker=? AND price_date>=? ORDER BY price_date""",
            connection,
            params=(ticker, start_date),
        )
    if frame.empty:
        return frame
    frame["price_date"] = pd.to_datetime(frame["price_date"])
    frame = frame.set_index("price_date")
    frame.columns = ["Open", "High", "Low", "Close", "Volume"]
    return frame


def cached_price_range(db_path: Path, ticker: str) -> tuple[str, str, int]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT MIN(price_date),MAX(price_date),COUNT(*) FROM prices WHERE ticker=?",
            (ticker,),
        ).fetchone()
    return (str(row[0] or ""), str(row[1] or ""), int(row[2] or 0))


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_downloaded_frame(data: pd.DataFrame, ticker: str, ticker_count: int) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(map(str, data.columns.get_level_values(0)))
        level1 = set(map(str, data.columns.get_level_values(1)))
        if ticker in level0:
            frame = data[ticker].copy()
        elif ticker in level1:
            frame = data.xs(ticker, axis=1, level=1).copy()
        else:
            return pd.DataFrame()
    elif ticker_count == 1:
        frame = data.copy()
    else:
        # yfinanceの版によっては複数銘柄でも列名を平坦化して返す。
        selected: dict[str, pd.Series] = {}
        for column in data.columns:
            text = str(column)
            upper = text.upper().replace(" ", "_")
            for field in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"):
                if upper in {f"{ticker}_{field}", f"{field}_{ticker}"}:
                    selected[field.title()] = data[column]
        frame = pd.DataFrame(selected, index=data.index)
        if frame.empty:
            return pd.DataFrame()
    rename = {str(column).title(): column for column in frame.columns}
    required = {}
    for name in ("Open", "High", "Low", "Close", "Volume"):
        original = rename.get(name)
        if original is not None:
            required[original] = name
    frame = frame.rename(columns=required)
    if "Close" not in frame.columns:
        return pd.DataFrame()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in frame.columns:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[["Open", "High", "Low", "Close", "Volume"]]
    frame = frame[frame["Close"].notna() & frame["Close"].gt(0)]
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def download_batch(yf: Any, tickers: list[str], **kwargs: Any) -> dict[str, pd.DataFrame]:
    if not tickers:
        return {}
    common = dict(
        tickers=tickers, group_by="ticker", auto_adjust=False,
        actions=False, threads=True, progress=False, **kwargs,
    )
    repair_available = importlib.util.find_spec("scipy") is not None
    try:
        data = yf.download(repair=repair_available, timeout=30, **common)
    except TypeError:
        # 古いyfinanceではrepair/timeout引数が未対応。
        data = yf.download(**common)
    return {ticker: normalize_downloaded_frame(data, ticker, len(tickers)) for ticker in tickers}


def download_yahoo_chart(ticker: str, **kwargs: Any) -> pd.DataFrame:
    """yfinanceの版差・解析失敗時にYahoo Chart APIを直接読む。"""
    import requests

    interval = str(kwargs.get("interval", "1d"))
    params: dict[str, Any] = {"interval": interval, "events": "div,splits", "includeAdjustedClose": "true"}
    if kwargs.get("start"):
        params["period1"] = int(pd.Timestamp(kwargs["start"], tz="UTC").timestamp())
        end = kwargs.get("end") or (date.today() + pd.Timedelta(days=1)).isoformat()
        params["period2"] = int(pd.Timestamp(end, tz="UTC").timestamp())
    else:
        params["range"] = str(kwargs.get("period", "3mo"))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='-')}"
    response = requests.get(
        url, params=params, timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; USStockResearch/0.1)"},
    )
    response.raise_for_status()
    payload = response.json().get("chart", {})
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    results = payload.get("result") or []
    if not results:
        return pd.DataFrame()
    result = results[0]
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not quotes:
        return pd.DataFrame()
    count = len(timestamps)
    frame = pd.DataFrame(
        {
            "Open": quotes.get("open") or [None] * count,
            "High": quotes.get("high") or [None] * count,
            "Low": quotes.get("low") or [None] * count,
            "Close": quotes.get("close") or [None] * count,
            "Volume": quotes.get("volume") or [None] * count,
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_localize(None),
    )
    return normalize_downloaded_frame(frame, ticker, 1)


def download_with_fallback(yf: Any, tickers: list[str], **kwargs: Any) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """一括取得を優先し、失敗・空データだけ個別取得で救済する。"""
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    batch_error = ""
    try:
        frames = download_batch(yf, tickers, **kwargs)
    except Exception as exc:
        batch_error = str(exc)
        frames = {}
    missing = [ticker for ticker in tickers if frames.get(ticker, pd.DataFrame()).empty]
    for ticker in missing:
        try:
            single = download_batch(yf, [ticker], **kwargs).get(ticker, pd.DataFrame())
            if single.empty:
                # download()の応答形式差でも空ならTicker.historyを最後の手段にする。
                history_kwargs = dict(kwargs)
                history_kwargs.pop("interval", None)
                interval = kwargs.get("interval", "1d")
                try:
                    single = yf.Ticker(ticker).history(
                        auto_adjust=False,
                        repair=importlib.util.find_spec("scipy") is not None,
                        interval=interval,
                        **history_kwargs,
                    )
                except TypeError:
                    single = yf.Ticker(ticker).history(
                        auto_adjust=False, interval=interval, **history_kwargs
                    )
                single = normalize_downloaded_frame(single, ticker, 1)
            if single.empty:
                single = download_yahoo_chart(ticker, **kwargs)
            if single.empty:
                errors[ticker] = batch_error or "yfinance_empty"
            else:
                frames[ticker] = single
        except Exception as exc:
            individual_error = str(exc)
            try:
                direct = download_yahoo_chart(ticker, **kwargs)
                if not direct.empty:
                    frames[ticker] = direct
                    continue
                direct_error = "empty"
            except Exception as direct_exc:
                direct_error = str(direct_exc)
            errors[ticker] = (
                f"batch={batch_error or 'empty'}; individual={individual_error}; "
                f"chart={direct_error}"
            )
    return frames, errors


def fetch_universe(client: SecClient, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    payload = client.get_json(
        "https://www.sec.gov/files/company_tickers_exchange.json",
        cache_dir / "company_tickers_exchange.json",
        refresh=refresh,
    )
    fields = payload.get("fields", [])
    data = payload.get("data", [])
    frame = pd.DataFrame(data, columns=fields)
    aliases = {str(column).lower(): column for column in frame.columns}
    ticker_col = aliases.get("ticker")
    exchange_col = aliases.get("exchange")
    cik_col = aliases.get("cik")
    name_col = aliases.get("name")
    if not all((ticker_col, exchange_col, cik_col, name_col)):
        raise RuntimeError(f"SEC企業一覧の形式を認識できません: {list(frame.columns)}")
    frame = frame.rename(columns={ticker_col: "ticker", exchange_col: "exchange", cik_col: "cik", name_col: "company_name"})
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.replace(".", "-", regex=False)
    frame["exchange"] = frame["exchange"].astype(str)
    frame["cik"] = frame["cik"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(10)
    frame = frame[frame["exchange"].isin(SUPPORTED_EXCHANGES)]
    frame = frame[frame["ticker"].str.fullmatch(r"[A-Z][A-Z0-9\-]{0,9}", na=False)]
    frame = frame[~frame["ticker"].str.contains(r"-(?:W|U|R)$", regex=True)]
    fund_pattern = r"\bETF\b|EXCHANGE.TRADED|ISHARES|SPDR|PROSHARES|DIREXION|WISDOMTREE|TEUCRIUM|UNITED STATES .* FUND|ABRDN PHYSICAL"
    frame = frame[~frame["company_name"].astype(str).str.upper().str.contains(fund_pattern, regex=True, na=False)]
    # SEC配列の順序を維持する。現在の公開ファイルは主要企業から並ぶため、
    # 後段の流動性スクリーニングと組み合わせて再現可能な母集団にする。
    return frame.drop_duplicates("ticker").reset_index(drop=True)


def liquidity_screen(
    yf: Any,
    universe: pd.DataFrame,
    target_count: int,
    batch_size: int,
    minimum_price: float,
    minimum_dollar_volume: float,
    progress: ProgressCallback | None,
    stop_event: threading.Event,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidate_count = min(len(universe), max(target_count * 2, target_count + 500))
    candidates = universe.head(candidate_count).copy()
    metrics: dict[str, tuple[float, float, int]] = {}
    failures: dict[str, str] = {}
    total_batches = math.ceil(len(candidates) / batch_size)
    for batch_no, start in enumerate(range(0, len(candidates), batch_size), start=1):
        if stop_event.is_set():
            break
        tickers = candidates["ticker"].iloc[start:start + batch_size].tolist()
        downloaded, batch_failures = download_with_fallback(yf, tickers, period="3mo", interval="1d")
        failures.update(batch_failures)
        for ticker, frame in downloaded.items():
            if frame.empty:
                continue
            recent = frame.tail(60)
            last_price = float(recent["Close"].iloc[-1])
            dollar_volume = (recent["Close"] * recent["Volume"]).replace([np.inf, -np.inf], np.nan).median()
            metrics[ticker] = (last_price, float(dollar_volume) if pd.notna(dollar_volume) else 0.0, len(frame))
        emit(
            progress, phase="liquidity", batch=batch_no, total_batches=total_batches,
            checked=min(start + batch_size, len(candidates)), candidates=len(candidates),
            price_available=len(metrics), failures=len(failures),
            failure_sample=(next(iter(failures.items())) if failures else None),
        )
        if batch_no == 1 and not any(not frame.empty for frame in downloaded.values()):
            for ticker in candidates["ticker"].iloc[start + batch_size:]:
                failures[str(ticker)] = "先頭バッチが全滅したため未試行"
            break
        # The batched request itself provides pacing. Keep only a short gap to
        # avoid adding minutes of unconditional idle time on large universes.
        time.sleep(0.1)
    candidates["last_price"] = candidates["ticker"].map(lambda t: metrics.get(t, (np.nan, 0.0, 0))[0])
    candidates["median_dollar_volume"] = candidates["ticker"].map(lambda t: metrics.get(t, (np.nan, 0.0, 0))[1])
    candidates["recent_days"] = candidates["ticker"].map(lambda t: metrics.get(t, (np.nan, 0.0, 0))[2])
    candidates["取得状態"] = candidates["ticker"].map(
        lambda ticker: "取得成功" if ticker in metrics else "取得失敗"
    )
    candidates["取得失敗理由"] = candidates["ticker"].map(lambda ticker: failures.get(ticker, ""))
    candidates["除外理由"] = ""
    candidates.loc[candidates["last_price"].lt(minimum_price), "除外理由"] = "最低株価未満"
    candidates.loc[candidates["median_dollar_volume"].lt(minimum_dollar_volume), "除外理由"] = "最低売買代金未満"
    candidates.loc[candidates["recent_days"].lt(30), "除外理由"] = "最近の日足不足"
    candidates.loc[candidates["取得状態"].eq("取得失敗"), "除外理由"] = "価格取得失敗"
    selected = candidates[
        candidates["last_price"].ge(minimum_price)
        & candidates["median_dollar_volume"].ge(minimum_dollar_volume)
        & candidates["recent_days"].ge(30)
    ].sort_values("median_dollar_volume", ascending=False).head(target_count)
    return selected.reset_index(drop=True), candidates


def collect_long_prices(
    yf: Any,
    selected: pd.DataFrame,
    db_path: Path,
    price_start: str,
    batch_size: int,
    progress: ProgressCallback | None,
    stop_event: threading.Event,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    today = date.today().isoformat()
    missing: list[str] = []
    for ticker in selected["ticker"]:
        first, last, count = cached_price_range(db_path, ticker)
        if not first or first > price_start or not last or last < (date.today().replace(day=1).isoformat()) or count < 400:
            missing.append(ticker)
    total_batches = math.ceil(len(missing) / batch_size) if missing else 0
    for batch_no, start in enumerate(range(0, len(missing), batch_size), start=1):
        if stop_event.is_set():
            break
        tickers = missing[start:start + batch_size]
        downloaded, batch_failures = download_with_fallback(
            yf, tickers, start=price_start, end=today, interval="1d"
        )
        errors.update(batch_failures)
        for ticker in tickers:
            frame = downloaded.get(ticker, pd.DataFrame())
            if frame.empty:
                errors[ticker] = "price_empty"
                continue
            save_prices(db_path, ticker, frame)
        emit(progress, phase="prices", batch=batch_no, total_batches=total_batches, completed=min(start + batch_size, len(missing)), total=len(missing), cached=len(selected) - len(missing))
        time.sleep(0.1)
    return errors


def to_price_points(frame: pd.DataFrame, algorithm: Any) -> list[Any]:
    points = []
    # Avoid constructing one pandas Series for every trading day.
    for timestamp, _open, high, low, close_value, volume in frame.itertuples(index=True, name=None):
        close = _float_or_none(close_value)
        if close is None or close <= 0:
            continue
        ts = pd.Timestamp(timestamp)
        points.append(algorithm.PricePoint(
            timestamp=int(ts.timestamp()), date_text=ts.date().isoformat(), close=close,
            high=_float_or_none(high), low=_float_or_none(low),
            volume=int(volume) if pd.notna(volume) else None,
        ))
    return points


def fixed_horizon_metrics(frame: pd.DataFrame, signal_date: str, signal_price: float) -> dict[str, Any]:
    if frame.empty or not signal_date or signal_price <= 0:
        return {}
    index_dates = pd.DatetimeIndex(frame.index)
    position = int(index_dates.searchsorted(pd.Timestamp(signal_date), side="left"))
    output: dict[str, Any] = {}
    for label, trading_days in (("1年", 252), ("2年", 504), ("3年", 756)):
        end = position + trading_days
        if position >= len(frame) or end >= len(frame):
            output[f"{label}後リターン"] = None
            output[f"{label}内最大上昇率"] = None
            output[f"{label}内最大下落率"] = None
            continue
        future = frame.iloc[position + 1:end + 1]
        output[f"{label}後リターン"] = (float(frame["Close"].iloc[end]) / signal_price - 1) * 100
        output[f"{label}内最大上昇率"] = max(0.0, (float(future["High"].max()) / signal_price - 1) * 100)
        output[f"{label}内最大下落率"] = min(0.0, (float(future["Low"].min()) / signal_price - 1) * 100)
    return output


def find_crash_start_date(
    frame: pd.DataFrame,
    peak_date: str,
    bottom_date: str,
    peak_price: float,
    threshold: float = 0.10,
) -> str:
    """ピーク後、終値がピーク価格から初めて指定率下落した日を返す。"""
    if frame.empty or not peak_date or not bottom_date or peak_price <= 0:
        return ""
    window = frame.loc[
        (frame.index > pd.Timestamp(peak_date))
        & (frame.index <= pd.Timestamp(bottom_date))
    ]
    crossed = window[pd.to_numeric(window["Close"], errors="coerce").le(peak_price * (1 - threshold))]
    return pd.Timestamp(crossed.index[0]).date().isoformat() if not crossed.empty else ""


def detect_events(
    selected: pd.DataFrame,
    db_path: Path,
    algorithm: Any,
    price_start: str,
    signal_start: str,
    progress: ProgressCallback | None,
    stop_event: threading.Event,
    checkpoint_events_path: Path | None = None,
    checkpoint_state_path: Path | None = None,
    resume: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    processed_tickers: set[str] = set()
    algorithm_version = str(getattr(algorithm, "ALGORITHM_VERSION", ""))
    if resume and checkpoint_state_path and checkpoint_state_path.exists():
        try:
            state = json.loads(checkpoint_state_path.read_text(encoding="utf-8"))
            if state.get("algorithm_version") == algorithm_version:
                if checkpoint_events_path and checkpoint_events_path.exists() and checkpoint_events_path.stat().st_size > 5:
                    checkpoint_frame = pd.read_csv(checkpoint_events_path, encoding="utf-8-sig", low_memory=False)
                    if "暴落開始時価格" in checkpoint_frame.columns:
                        events = checkpoint_frame.to_dict("records")
                        processed_tickers = set(map(str, state.get("processed_tickers", [])))
                        errors = {str(key): str(value) for key, value in state.get("errors", {}).items()}
        except Exception:
            events, errors, processed_tickers = [], {}, set()

    def save_detection_checkpoint() -> None:
        if checkpoint_events_path:
            atomic_csv_write(pd.DataFrame(events), checkpoint_events_path)
        if checkpoint_state_path:
            atomic_json_write(checkpoint_state_path, {
                "app_version": APP_VERSION, "algorithm_version": algorithm_version,
                "processed_tickers": sorted(processed_tickers), "errors": errors,
                "event_count": len(events), "updated_at": datetime.now().isoformat(timespec="seconds"),
            })

    total = len(selected)
    for position, (_, row) in enumerate(selected.iterrows(), start=1):
        if stop_event.is_set():
            break
        ticker = str(row["ticker"])
        if ticker in processed_tickers:
            emit(progress, phase="detect", current=position, total=total, ticker=ticker, events=len(events), errors=len(errors), resumed=len(processed_tickers))
            continue
        try:
            frame = load_prices(db_path, ticker, price_start)
            points = to_price_points(frame, algorithm)
            analysis = algorithm.analyze_bottoming(points, "stock", "general")
            signals = {item.get("confirmationDate"): item for item in analysis.get("signals", [])}
            for evaluation in analysis.get("bottomEvaluations", []):
                event_date = str(evaluation.get("date") or "")
                if not event_date or event_date < signal_start:
                    continue
                signal = signals.get(event_date, {})
                algorithm_peak_date = str(signal.get("peakDate") or evaluation.get("previousPeakDate") or "")
                algorithm_peak_price = _float_or_none(signal.get("rollingPeak")) or _float_or_none(evaluation.get("previousPeakPrice")) or 0.0
                crash_start_date = find_crash_start_date(
                    frame, algorithm_peak_date, event_date, algorithm_peak_price
                )
                crash_start_price = None
                if crash_start_date:
                    crash_rows = frame.loc[frame.index == pd.Timestamp(crash_start_date), "Close"]
                    if not crash_rows.empty:
                        crash_start_price = _float_or_none(crash_rows.iloc[0])
                event = {
                    "銘柄": ticker, "企業名": row.get("company_name", ""), "取引所": row.get("exchange", ""),
                    "CIK": row.get("cik", ""), "底打ち候補日": event_date,
                    "底打ち候補日価格": evaluation.get("price"), "判定前最高値日": evaluation.get("previousPeakDate"),
                    "判定前最高値": evaluation.get("previousPeakPrice"), "高値からの下落率": evaluation.get("drawdownFromPreviousPeakPercent"),
                    "候補後最安値日": evaluation.get("minAfterDate"), "候補後最安値": evaluation.get("minAfterPrice"),
                    "候補後下落率": evaluation.get("drawdownAfterPercent"), "底位置指数": evaluation.get("bottomPositionRatio"),
                    "現在日": evaluation.get("currentDate"), "現在価格": evaluation.get("currentPrice"),
                    "現在まで保有リターン": evaluation.get("holdingReturnPercent"), "年利換算": evaluation.get("annualizedReturnPercent"),
                    "保有営業日": evaluation.get("tradingDaysHeld"), "保有暦日": evaluation.get("calendarDaysHeld"),
                    "成否": evaluation.get("verdict"), "トリガー日": signal.get("triggerDate"),
                    "トリガー種別": signal.get("triggerType"), "底検知時スコア": _series_score(analysis, event_date),
                    "アルゴリズム判定ピーク日": algorithm_peak_date,
                    "アルゴリズム判定ピーク価格": algorithm_peak_price,
                    "アルゴリズムピーク→底検知下落率": (
                        (float(evaluation.get("price")) / algorithm_peak_price - 1) * 100
                        if algorithm_peak_price > 0 and _float_or_none(evaluation.get("price")) is not None else None
                    ),
                    "暴落開始日": crash_start_date,
                    "暴落開始時価格": crash_start_price,
                    "暴落開始定義": "ピーク後に終値がピーク価格から初めて10%以上下落した日",
                    "底検知時60日レンジ": signal.get("range60"), "底検知時20日ボラ": signal.get("volatility20"),
                    "底検知時出来高倍率": signal.get("volumeRatio"), "底検知時日次騰落率": signal.get("dailyReturn"),
                    "ピーク経過営業日": signal.get("peakAgeTradingDays"),
                    "底検知アルゴリズムver": getattr(algorithm, "ALGORITHM_VERSION", ""),
                    **fixed_horizon_metrics(frame, event_date, float(evaluation.get("price") or 0)),
                }
                peak = _float_or_none(evaluation.get("previousPeakPrice"))
                low = _float_or_none(evaluation.get("minAfterPrice"))
                event["判定前最高値→候補後最安値下落率"] = ((low / peak - 1) * 100 if peak and low else None)
                events.append(event)
        except Exception as exc:
            errors[ticker] = str(exc)
        processed_tickers.add(ticker)
        if position % 10 == 0 or position == total:
            save_detection_checkpoint()
        emit(progress, phase="detect", current=position, total=total, ticker=ticker, events=len(events), errors=len(errors))
    save_detection_checkpoint()
    return events, errors


def _series_score(analysis: dict[str, Any], event_date: str) -> Any:
    for row in analysis.get("series", []):
        if row.get("date") == event_date:
            return row.get("score")
    return None


def _fact_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row.get("taxonomy", "")), str(row.get("concept", "")), str(row.get("unit", ""))


def _select_prior_annual(companyfacts: dict[str, Any], spec: Any, signal_date: date, current: dict[str, Any] | None) -> dict[str, Any] | None:
    if not current:
        return None
    current_end = parse_date(current.get("end"))
    if not current_end:
        return None
    candidates: list[dict[str, Any]] = []
    for concept in spec.concepts:
        for row in _valid_fact_rows(companyfacts, concept, signal_date):
            end = parse_date(row.get("end"))
            if row.get("form") not in ANNUAL_FORMS or not end or end >= current_end:
                continue
            gap = (current_end - end).days
            if 250 <= gap <= 500 and _fact_identity(row) == _fact_identity(current):
                candidates.append(row)
    return max(candidates, key=lambda row: (parse_date(row.get("end")) or date.min, parse_date(row.get("filed")) or date.min)) if candidates else None


def _select_latest_quarter(companyfacts: dict[str, Any], spec: Any, signal_date: date) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for concept in spec.concepts:
        for row in _valid_fact_rows(companyfacts, concept, signal_date):
            days = _duration_days(row)
            filed = parse_date(row.get("filed"))
            if row.get("form") in {"10-Q", "10-Q/A"} and days and 70 <= days <= 115 and filed and (signal_date - filed).days <= 220:
                candidates.append(row)
    return max(candidates, key=lambda row: (parse_date(row.get("end")) or date.min, parse_date(row.get("filed")) or date.min)) if candidates else None


def _select_prior_year_quarter(companyfacts: dict[str, Any], spec: Any, signal_date: date, current: dict[str, Any] | None) -> dict[str, Any] | None:
    if not current:
        return None
    current_end = parse_date(current.get("end")); current_days = _duration_days(current)
    if not current_end or not current_days:
        return None
    candidates: list[dict[str, Any]] = []
    for concept in spec.concepts:
        for row in _valid_fact_rows(companyfacts, concept, signal_date):
            end = parse_date(row.get("end")); days = _duration_days(row)
            if not end or not days or _fact_identity(row) != _fact_identity(current):
                continue
            gap = (current_end - end).days
            if 320 <= gap <= 410 and abs(days - current_days) <= 20:
                candidates.append(row)
    return max(candidates, key=lambda row: (parse_date(row.get("end")) or date.min, parse_date(row.get("filed")) or date.min)) if candidates else None


def _change_values(current: dict[str, Any] | None, prior: dict[str, Any] | None) -> tuple[Any, Any]:
    if not current or not prior:
        return None, None
    current_value, prior_value = _float_or_none(current.get("value")), _float_or_none(prior.get("value"))
    if current_value is None or prior_value is None:
        return None, None
    delta = current_value - prior_value
    scaled = delta / abs(prior_value) if prior_value != 0 else None
    return delta, scaled


def extract_financial_changes(companyfacts: dict[str, Any], signal_date: date) -> dict[str, Any]:
    output: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    comparable_count = 0
    for spec in METRICS:
        if spec.key not in CHANGE_METRIC_KEYS:
            continue
        annual = select_fact(companyfacts, spec, signal_date, annual_only=True)
        annual_filed = parse_date(annual.get("filed")) if annual else None
        if not annual_filed or (signal_date - annual_filed).days > 550:
            annual = None
        prior_annual = _select_prior_annual(companyfacts, spec, signal_date, annual)
        quarter = _select_latest_quarter(companyfacts, spec, signal_date) if spec.kind == "duration" else None
        prior_quarter = _select_prior_year_quarter(companyfacts, spec, signal_date, quarter)
        annual_delta, annual_rate = _change_values(annual, prior_annual)
        quarter_delta, quarter_rate = _change_values(quarter, prior_quarter)
        prefix = f"底検知時_SEC_{spec.label}"
        output[f"{prefix}_前年差"] = annual_delta
        output[f"{prefix}_前年差率"] = annual_rate
        output[f"{prefix}_前年同期差"] = quarter_delta
        output[f"{prefix}_前年同期比"] = quarter_rate
        comparable_count += int(annual_rate is not None) + int(quarter_rate is not None)
        if annual or quarter:
            audit[spec.label] = {"annual_current": annual, "annual_prior": prior_annual, "quarter_current": quarter, "quarter_prior": prior_quarter}
    output["底検知時_SEC_変化取得数"] = comparable_count
    output["底検知時_SEC_変化根拠JSON"] = json.dumps(audit, ensure_ascii=False, default=str, separators=(",", ":"))
    return output


def extract_named_financial_snapshot(
    companyfacts: dict[str, Any],
    as_of_date: date,
    stage_name: str,
) -> dict[str, Any]:
    raw = extract_point_in_time_financials(companyfacts, as_of_date)
    return rename_financial_snapshot(raw, stage_name)


def rename_financial_snapshot(raw: dict[str, Any], stage_name: str) -> dict[str, Any]:
    source_prefix = "底検知時_SEC_"
    target_prefix = f"{stage_name}_SEC_"
    return {
        (target_prefix + key[len(source_prefix):] if key.startswith(source_prefix) else key): value
        for key, value in raw.items()
    }


def _relative_change(new_value: Any, old_value: Any) -> float | None:
    new_number, old_number = _float_or_none(new_value), _float_or_none(old_value)
    if new_number is None or old_number is None or old_number == 0:
        return None
    return (new_number - old_number) / abs(old_number)


def _ratio_value(numerator: Any, denominator: Any) -> float | None:
    numerator_number = _float_or_none(numerator)
    denominator_number = _float_or_none(denominator)
    if numerator_number is None or denominator_number is None or denominator_number == 0:
        return None
    return numerator_number / denominator_number


DERIVED_FINANCIAL_LABELS = [
    "粗利益率", "営業利益率", "純利益率", "営業CFマージン", "FCFマージン",
    "設備投資売上比率", "研究開発費売上比率", "ROA", "ROE",
    "負債総資産比率", "純資産総資産比率", "現金総資産比率",
    "流動資産総資産比率", "流動負債総資産比率", "長期有利子負債総資産比率",
    "負債純資産倍率", "現金負債比率", "現金長期有利子負債比率",
    "流動比率", "当座比率概算", "運転資本", "運転資本総資産比率",
    "運転資本売上比率", "総資産回転率", "棚卸資産売上比率",
    "売掛債権売上比率", "営業CF純利益比率", "設備投資営業CF比率",
    "インタレストカバレッジ", "1株当たり純資産", "1株当たり現金",
    "1株当たり売上高", "1株当たり純利益", "FCF",
    "時価総額", "企業価値EV", "PER", "PBR", "PSR", "利益利回り",
    "EV売上高倍率", "EV営業利益倍率", "グレアム型理論株価",
    "実株価理論株価乖離率", "理論株価上昇余地率",
]


def add_derived_financial_features(row: dict[str, Any]) -> None:
    """各局面について規模に左右されにくい財務比率を追加する。"""
    for stage in ("ピーク時", "暴落開始時", "底検知時"):
        prefix = f"{stage}_SEC_"
        for annual_suffix, output_suffix in (("", ""), ("_直近年次", "_直近年次ベース")):
            def value(label: str) -> Any:
                return row.get(f"{prefix}{label}{annual_suffix}")

            revenue, gross_profit = value("売上高"), value("粗利益")
            operating_income, net_income = value("営業利益"), value("純利益")
            assets, current_assets = value("総資産"), value("流動資産")
            liabilities, current_liabilities = value("負債"), value("流動負債")
            equity, cash = value("純資産"), value("現金等")
            operating_cf, capex, rd = value("営業CF"), value("設備投資額"), value("研究開発費")
            inventory, receivables = value("棚卸資産"), value("売掛債権")
            long_debt, interest, shares = value("長期有利子負債"), value("支払利息"), value("発行済株式数")
            fcf = None if _float_or_none(operating_cf) is None or _float_or_none(capex) is None else float(operating_cf) - float(capex)
            working_capital = None if _float_or_none(current_assets) is None or _float_or_none(current_liabilities) is None else float(current_assets) - float(current_liabilities)
            quick_assets = None if _float_or_none(current_assets) is None or _float_or_none(inventory) is None else float(current_assets) - float(inventory)
            values = {
                "粗利益率": _ratio_value(gross_profit, revenue),
                "営業利益率": _ratio_value(operating_income, revenue),
                "純利益率": _ratio_value(net_income, revenue),
                "営業CFマージン": _ratio_value(operating_cf, revenue),
                "FCFマージン": _ratio_value(fcf, revenue),
                "設備投資売上比率": _ratio_value(capex, revenue),
                "研究開発費売上比率": _ratio_value(rd, revenue),
                "ROA": _ratio_value(net_income, assets), "ROE": _ratio_value(net_income, equity),
                "負債総資産比率": _ratio_value(liabilities, assets),
                "純資産総資産比率": _ratio_value(equity, assets),
                "現金総資産比率": _ratio_value(cash, assets),
                "流動資産総資産比率": _ratio_value(current_assets, assets),
                "流動負債総資産比率": _ratio_value(current_liabilities, assets),
                "長期有利子負債総資産比率": _ratio_value(long_debt, assets),
                "負債純資産倍率": _ratio_value(liabilities, equity),
                "現金負債比率": _ratio_value(cash, liabilities),
                "現金長期有利子負債比率": _ratio_value(cash, long_debt),
                "流動比率": _ratio_value(current_assets, current_liabilities),
                "当座比率概算": _ratio_value(quick_assets, current_liabilities),
                "運転資本": working_capital,
                "運転資本総資産比率": _ratio_value(working_capital, assets),
                "運転資本売上比率": _ratio_value(working_capital, revenue),
                "総資産回転率": _ratio_value(revenue, assets),
                "棚卸資産売上比率": _ratio_value(inventory, revenue),
                "売掛債権売上比率": _ratio_value(receivables, revenue),
                "営業CF純利益比率": _ratio_value(operating_cf, net_income),
                "設備投資営業CF比率": _ratio_value(capex, operating_cf),
                "インタレストカバレッジ": _ratio_value(operating_income, interest),
                "1株当たり純資産": _ratio_value(equity, shares),
                "1株当たり現金": _ratio_value(cash, shares),
                "1株当たり売上高": _ratio_value(revenue, shares),
                "1株当たり純利益": _ratio_value(net_income, shares),
                "FCF": fcf,
            }
            for label, calculated in values.items():
                row[f"{prefix}{label}{output_suffix}"] = calculated


def add_point_in_time_valuation_features(row: dict[str, Any]) -> None:
    """Add valuation features using only information available at each stage."""
    price_columns = {
        "ピーク時": "アルゴリズム判定ピーク価格",
        "暴落開始時": "暴落開始時価格",
        "底検知時": "底打ち候補日価格",
    }
    for stage, price_column in price_columns.items():
        prefix = f"{stage}_SEC_"
        price = _float_or_none(row.get(price_column))
        shares = _float_or_none(row.get(f"{prefix}発行済株式数"))
        if shares is None:
            shares = _float_or_none(row.get(f"{prefix}発行済株式数_直近年次"))
        revenue = _float_or_none(row.get(f"{prefix}売上高_直近年次"))
        operating_income = _float_or_none(row.get(f"{prefix}営業利益_直近年次"))
        net_income = _float_or_none(row.get(f"{prefix}純利益_直近年次"))
        equity = _float_or_none(row.get(f"{prefix}純資産"))
        cash = _float_or_none(row.get(f"{prefix}現金等"))
        debt = _float_or_none(row.get(f"{prefix}長期有利子負債"))
        reported_eps = _float_or_none(row.get(f"{prefix}希薄化後EPS_直近年次"))

        valid_price = price is not None and price > 0
        valid_shares = shares is not None and shares > 0
        market_cap = price * shares if valid_price and valid_shares else None
        enterprise_value = (
            market_cap + debt - cash
            if market_cap is not None and debt is not None and cash is not None else None
        )
        eps = reported_eps
        if eps is None and net_income is not None and valid_shares:
            eps = net_income / shares
        book_value_per_share = equity / shares if equity is not None and valid_shares else None
        theoretical_price = None
        if eps is not None and eps > 0 and book_value_per_share is not None and book_value_per_share > 0:
            theoretical_price = math.sqrt(22.5 * eps * book_value_per_share)

        values = {
            "時価総額": market_cap,
            "企業価値EV": enterprise_value,
            "PER": _ratio_value(market_cap, net_income) if net_income is not None and net_income > 0 else None,
            "PBR": _ratio_value(market_cap, equity) if equity is not None and equity > 0 else None,
            "PSR": _ratio_value(market_cap, revenue) if revenue is not None and revenue > 0 else None,
            "利益利回り": _ratio_value(net_income, market_cap),
            "EV売上高倍率": _ratio_value(enterprise_value, revenue) if revenue is not None and revenue > 0 else None,
            "EV営業利益倍率": (
                _ratio_value(enterprise_value, operating_income)
                if operating_income is not None and operating_income > 0 else None
            ),
            "グレアム型理論株価": theoretical_price,
            "実株価理論株価乖離率": _relative_change(price, theoretical_price),
            "理論株価上昇余地率": _relative_change(theoretical_price, price),
        }
        for label, calculated in values.items():
            row[f"{prefix}{label}"] = calculated


def add_stage_financial_changes(row: dict[str, Any]) -> None:
    labels = (
        [spec.label for spec in METRICS]
        + [f"{spec.label}_直近年次" for spec in METRICS]
        + DERIVED_FINANCIAL_LABELS
        + [f"{label}_直近年次ベース" for label in DERIVED_FINANCIAL_LABELS]
    )
    comparisons = (
        ("ピーク時", "暴落開始時", "ピーク→暴落開始"),
        ("暴落開始時", "底検知時", "暴落開始→底検知"),
        ("ピーク時", "底検知時", "ピーク→底検知"),
    )
    for label in labels:
        phase_changes: dict[str, float | None] = {}
        for old_stage, new_stage, comparison_label in comparisons:
            change_value = _relative_change(
                row.get(f"{new_stage}_SEC_{label}"),
                row.get(f"{old_stage}_SEC_{label}"),
            )
            row[f"局面変化_SEC_{label}_{comparison_label}変化率"] = change_value
            phase_changes[comparison_label] = change_value
        first_change = phase_changes.get("ピーク→暴落開始")
        second_change = phase_changes.get("暴落開始→底検知")
        row[f"局面変化_SEC_{label}_変化加速度"] = (
            second_change - first_change
            if first_change is not None and second_change is not None else None
        )
        stock_drawdown = abs((_float_or_none(row.get("アルゴリズムピーク→底検知下落率")) or 0.0) / 100)
        peak_to_bottom = phase_changes.get("ピーク→底検知")
        row[f"局面変化_SEC_{label}_株価下落対比"] = (
            peak_to_bottom / stock_drawdown
            if peak_to_bottom is not None and stock_drawdown > 0 else None
        )


def add_robust_financial_transforms(row: dict[str, Any]) -> None:
    """外れ値に強い変換と、黒字化・赤字化を分離するフラグを追加する。"""
    monetary_labels = [spec.label for spec in METRICS]
    for stage in ("ピーク時", "暴落開始時", "底検知時"):
        for label in monetary_labels:
            for suffix in ("", "_直近年次"):
                column = f"{stage}_SEC_{label}{suffix}"
                number = _float_or_none(row.get(column))
                row[f"{column}_符号付きlog1p"] = (
                    math.copysign(math.log1p(abs(number)), number) if number is not None else None
                )

    rate_suffixes = ("前年差率", "前年同期比", "変化率", "変化加速度", "株価下落対比")
    for column in list(row):
        if "_SEC_" not in column or not column.endswith(rate_suffixes):
            continue
        number = _float_or_none(row.get(column))
        row[f"{column}_クリップ±500%"] = (
            max(-5.0, min(5.0, number)) if number is not None else None
        )

    comparisons = (
        ("ピーク時", "暴落開始時", "ピーク→暴落開始"),
        ("暴落開始時", "底検知時", "暴落開始→底検知"),
        ("ピーク時", "底検知時", "ピーク→底検知"),
    )
    labels = monetary_labels + [f"{label}_直近年次" for label in monetary_labels]
    for label in labels:
        for old_stage, new_stage, comparison in comparisons:
            old_value = _float_or_none(row.get(f"{old_stage}_SEC_{label}"))
            new_value = _float_or_none(row.get(f"{new_stage}_SEC_{label}"))
            flag = None
            if old_value is not None and new_value is not None:
                flag = 1 if old_value < 0 <= new_value else -1 if old_value >= 0 > new_value else 0
            row[f"局面変化_SEC_{label}_{comparison}符号反転"] = flag


def sic_major_group(value: Any) -> str:
    try:
        sic = int(str(value or "").strip())
    except ValueError:
        return "不明"
    if 100 <= sic <= 999:
        return "農林水産"
    if 1000 <= sic <= 1499:
        return "鉱業"
    if 1500 <= sic <= 1799:
        return "建設"
    if 2000 <= sic <= 3999:
        return "製造"
    if 4000 <= sic <= 4999:
        return "運輸・通信・公益"
    if 5000 <= sic <= 5199:
        return "卸売"
    if 5200 <= sic <= 5999:
        return "小売"
    if 6000 <= sic <= 6799:
        return "金融・保険・不動産"
    if 7000 <= sic <= 8999:
        return "サービス"
    if 9000 <= sic <= 9999:
        return "公共"
    return "その他"


def enrich_events_with_sec(
    events: list[dict[str, Any]],
    client: SecClient,
    progress: ProgressCallback | None,
    stop_event: threading.Event,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> list[dict[str, Any]]:
    facts_cache: dict[str, dict[str, Any]] = {}
    submissions_cache: dict[str, dict[str, Any]] = {}
    facts_errors: dict[str, str] = {}
    snapshot_cache: dict[tuple[str, date], dict[str, Any]] = {}
    change_cache: dict[tuple[str, date], dict[str, Any]] = {}
    completed_rows: dict[tuple[str, str], dict[str, Any]] = {}
    if resume and checkpoint_path and checkpoint_path.exists():
        try:
            checkpoint = pd.read_csv(checkpoint_path, encoding="utf-8-sig", low_memory=False)
            if "米国研究アプリver" in checkpoint.columns:
                checkpoint = checkpoint[checkpoint["米国研究アプリver"].astype(str).eq(APP_VERSION)]
            for record in checkpoint.to_dict("records"):
                key = (str(record.get("銘柄", "")), str(record.get("底打ち候補日", "")))
                if all(key):
                    completed_rows[key] = record
        except Exception:
            completed_rows = {}
    output: list[dict[str, Any]] = []
    total = len(events)
    pending_events = [
        event for event in events
        if (str(event.get("銘柄", "")), str(event.get("底打ち候補日", ""))) not in completed_rows
    ]
    unique_ciks = sorted({str(event.get("CIK", "")).zfill(10) for event in pending_events if event.get("CIK")})

    def fetch_facts(cik: str) -> tuple[str, dict[str, Any] | None, dict[str, Any], str]:
        try:
            facts = client.company_facts(cik)
            try:
                submissions = client.company_submissions(cik)
            except Exception:
                submissions = {}
            return cik, facts, submissions, ""
        except Exception as exc:
            return cik, None, {}, str(exc)

    # 通信待ちを重ね合わせる。開始間隔はSecClient側で約6.7req/sに制限する。
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_facts, cik) for cik in unique_ciks]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            cik, facts, submissions, error = future.result()
            if facts is not None:
                facts_cache[cik] = facts
                submissions_cache[cik] = submissions
            else:
                facts_errors[cik] = error
            emit(
                progress, phase="sec_fetch", current=completed, total=len(unique_ciks),
                success=len(facts_cache), errors=len(facts_errors),
            )
            if stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break

    for position, event in enumerate(events, start=1):
        if stop_event.is_set():
            break
        event_key = (str(event.get("銘柄", "")), str(event.get("底打ち候補日", "")))
        if event_key in completed_rows:
            output.append(completed_rows[event_key])
            emit(progress, phase="sec", current=position, total=total, ticker=event.get("銘柄", ""), success=sum(bool(item.get("底検知時_SEC_取得数", 0)) for item in output), errors=sum(item.get("底検知時_SEC状態") == "エラー" for item in output), resumed=len(output))
            continue
        row = dict(event)
        cik = str(event.get("CIK", "")).zfill(10)
        signal_date = parse_date(event.get("底打ち候補日"))
        peak_date = parse_date(event.get("アルゴリズム判定ピーク日"))
        crash_start_date = parse_date(event.get("暴落開始日"))
        try:
            if cik in facts_errors:
                raise RuntimeError(facts_errors[cik])
            if cik not in facts_cache:
                facts_cache[cik] = client.company_facts(cik)
            facts = facts_cache[cik]
            submissions = submissions_cache.get(cik, {})
            sic = str(submissions.get("sic", "") or "")
            row["SEC_SIC"] = sic
            row["SEC_SIC説明"] = str(submissions.get("sicDescription", "") or "")
            row["SEC_SIC大分類"] = sic_major_group(sic)

            def snapshot(as_of: date, stage_name: str) -> dict[str, Any]:
                cache_key = (cik, as_of)
                if cache_key not in snapshot_cache:
                    snapshot_cache[cache_key] = extract_point_in_time_financials(facts, as_of)
                return rename_financial_snapshot(snapshot_cache[cache_key], stage_name)

            if peak_date:
                row.update(snapshot(peak_date, "ピーク時"))
                row["ピーク時_SEC状態"] = "取得成功" if row.get("ピーク時_SEC_取得数", 0) else "対象時点以前の財務なし"
            if crash_start_date:
                row.update(snapshot(crash_start_date, "暴落開始時"))
                row["暴落開始時_SEC状態"] = "取得成功" if row.get("暴落開始時_SEC_取得数", 0) else "対象時点以前の財務なし"
            else:
                row["暴落開始時_SEC状態"] = "10%下落日なし"
            if signal_date:
                row.update(snapshot(signal_date, "底検知時"))
                change_key = (cik, signal_date)
                if change_key not in change_cache:
                    change_cache[change_key] = extract_financial_changes(facts, signal_date)
                row.update(change_cache[change_key])
                row["底検知時_SEC状態"] = "取得成功" if row.get("底検知時_SEC_取得数", 0) else "対象時点以前の財務なし"
            add_derived_financial_features(row)
            add_point_in_time_valuation_features(row)
            add_stage_financial_changes(row)
            add_robust_financial_transforms(row)
        except Exception as exc:
            row["底検知時_SEC状態"] = "エラー"
            row["底検知時_SECエラー"] = str(exc)
        row["底検知時_SEC_CIK"] = cik
        row["底検知時_SECアプリver"] = SEC_APP_VERSION
        row["米国研究アプリver"] = APP_VERSION
        output.append(row)
        # Rewriting a wide, growing CSV every five rows is quadratic I/O.
        # Twenty-five rows still keeps resume loss small while cutting writes 5x.
        if checkpoint_path and (position % 25 == 0 or position == total):
            atomic_csv_write(pd.DataFrame(output), checkpoint_path)
        emit(progress, phase="sec", current=position, total=total, ticker=event.get("銘柄", ""), success=sum(bool(item.get("底検知時_SEC_取得数", 0)) for item in output), errors=sum(item.get("底検知時_SEC状態") == "エラー" for item in output))
    if checkpoint_path and output:
        atomic_csv_write(pd.DataFrame(output), checkpoint_path)
    return output


def correlation_report(frame: pd.DataFrame) -> pd.DataFrame:
    targets = [column for column in ("1年後リターン", "2年後リターン", "3年後リターン", "1年内最大下落率", "2年内最大下落率", "3年内最大下落率", "候補後下落率") if column in frame.columns]
    features = [
        column for column in frame.columns
        if "_SEC_" in column
        and not column.endswith(("状態", "エラー", "主通貨", "採用根拠JSON", "変化根拠JSON", "最新採用提出日", "アプリver", "CIK"))
    ]
    numeric_features = {
        feature: pd.to_numeric(frame[feature], errors="coerce") for feature in features
    }
    rows: list[dict[str, Any]] = []
    for target in targets:
        y = pd.to_numeric(frame[target], errors="coerce")
        for feature in features:
            x = numeric_features[feature]
            valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
            if valid.sum() < 20 or x[valid].nunique() < 2 or y[valid].nunique() < 2:
                continue
            rows.append({
                "目的変数": target, "説明変数": feature, "件数": int(valid.sum()),
                "Pearson相関": float(np.corrcoef(x[valid], y[valid])[0, 1]),
                "Spearman相関": float(np.corrcoef(x[valid].rank(), y[valid].rank())[0, 1]),
            })
    return pd.DataFrame(rows).sort_values(["目的変数", "Spearman相関"], key=lambda s: s.abs() if s.name == "Spearman相関" else s, ascending=[True, False]) if rows else pd.DataFrame(columns=["目的変数", "説明変数", "件数", "Pearson相関", "Spearman相関"])


def financial_feature_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = len(frame)
    for column in frame.columns:
        if "_SEC_" not in column:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        count = int(numeric.notna().sum())
        if count == 0:
            continue
        rows.append({
            "特徴量": column, "取得件数": count,
            "取得率": count / total if total else 0.0,
            "ユニーク数": int(numeric.nunique(dropna=True)),
            "推奨候補": bool(
                total and count >= max(30, math.ceil(total * 0.70))
                and numeric.nunique(dropna=True) >= 2
            ),
        })
    return pd.DataFrame(rows).sort_values(["推奨候補", "取得率", "ユニーク数"], ascending=[False, False, False]) if rows else pd.DataFrame(columns=["特徴量", "取得件数", "取得率", "ユニーク数", "推奨候補"])


PRIMARY_MODEL_TARGETS = ("1年内最大下落率", "1年後リターン")
INSTANT_FEATURE_LABELS = (
    "総資産", "流動資産", "負債", "流動負債", "純資産", "現金等",
    "棚卸資産", "売掛債権", "長期有利子負債",
    "負債総資産比率", "純資産総資産比率", "現金総資産比率",
    "流動資産総資産比率", "流動負債総資産比率",
    "長期有利子負債総資産比率", "負債純資産倍率", "現金負債比率",
    "現金長期有利子負債比率", "流動比率", "当座比率概算",
    "運転資本", "運転資本総資産比率",
)


def model_feature_eligibility(column: str) -> tuple[bool, str]:
    if "_SEC_" not in column:
        return False, "SEC財務特徴量ではない"
    if column.startswith("同業比較_SEC_"):
        if any(token in column for token in (
            "未来価格参照件数", "分類基準", "分類粒度", "情報カットオフ日", "最大参照価格日", "同業他社数",
        )):
            return False, "同業比較の監査・母数列"
        return True, "イベント日時点以前だけで算出した同業他社比較"
    if any(token in column for token in ("取得数", "CIK", "採用根拠", "主通貨", "状態", "エラー")):
        return False, "識別子・取得状況・監査列"
    if any(token in column for token in (
        "時価総額", "企業価値EV", "PER", "PBR", "PSR", "利益利回り",
        "EV売上高倍率", "EV営業利益倍率", "グレアム型理論株価",
        "実株価理論株価乖離率", "理論株価上昇余地率",
    )):
        return True, "各局面のポイントインタイム評価倍率・理論価格乖離"
    if "発行済株式数" in column:
        return False, "株式分割未調整の可能性"
    if column.endswith("_クリップ±500%"):
        return True, "外れ値抑制済みの変化率"
    if column.endswith("符号反転"):
        return True, "黒字化・赤字化フラグ"
    if column.endswith("符号付きlog1p"):
        return True, "絶対額の符号付き対数"
    if column.endswith(("前年差率", "前年同期比")):
        return False, "クリップ版を優先"
    if column.endswith(("変化率", "変化加速度", "株価下落対比")):
        return False, "クリップ版を優先"
    if "直近年次ベース" in column or "_直近年次_" in column:
        if any(token in column for token in ("比率", "率", "ROA", "ROE", "マージン", "回転率", "カバレッジ", "変化加速度", "株価下落対比")):
            return True, "年次ベースの比率・変化"
    if column.startswith("局面変化_SEC_") and any(label in column for label in INSTANT_FEATURE_LABELS):
        return True, "同一時点概念の貸借対照表変化"
    if not column.startswith("局面変化_SEC_") and any(label in column for label in INSTANT_FEATURE_LABELS):
        if any(token in column for token in ("比率", "率")):
            return True, "貸借対照表比率"
    return False, "絶対額・期間混在・比較性不足"


def feature_priority(column: str) -> str:
    if any(token in column for token in (
        "純資産", "現金", "負債", "営業CF", "FCF", "流動比率", "当座比率",
    )):
        return "A_重点"
    if any(token in column for token in (
        "営業利益率", "純利益率", "粗利益率", "ROA", "ROE", "売上高",
    )):
        return "B_補助"
    return "C_探索"


def build_model_feature_manifest(frame: pd.DataFrame) -> pd.DataFrame:
    total = len(frame)
    minimum_count = max(30, math.ceil(total * 0.70)) if total else 30
    rows: list[dict[str, Any]] = []
    for column in frame.columns:
        if "_SEC_" not in column:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        count, unique = int(numeric.notna().sum()), int(numeric.nunique(dropna=True))
        concept_ok, reason = model_feature_eligibility(column)
        coverage_ok = count >= minimum_count
        unique_ok = unique >= 2
        selected = concept_ok and coverage_ok and unique_ok
        if not coverage_ok:
            reason = f"取得件数不足（{count} < {minimum_count}）"
        elif not unique_ok:
            reason = "値が実質一定"
        rows.append({
            "特徴量": column, "優先度": feature_priority(column), "取得件数": count,
            "取得率": count / total if total else 0.0, "ユニーク数": unique,
            "モデル採用": selected, "判定理由": reason,
        })
    return pd.DataFrame(rows).sort_values(
        ["モデル採用", "優先度", "取得率", "ユニーク数"], ascending=[False, True, False, False]
    ) if rows else pd.DataFrame(columns=["特徴量", "優先度", "取得件数", "取得率", "ユニーク数", "モデル採用", "判定理由"])


def add_validation_splits(frame: pd.DataFrame, cutoff_year: int = 2023, group_folds: int = 5) -> pd.DataFrame:
    output = frame.copy()
    if "底打ち候補日" not in output.columns:
        output["底打ち候補日"] = pd.Series(index=output.index, dtype="object")
    if "銘柄" not in output.columns:
        output["銘柄"] = pd.Series(index=output.index, dtype="object")
    dates = pd.to_datetime(output["底打ち候補日"], errors="coerce")
    output["検証用_時系列区分"] = np.where(dates.dt.year >= cutoff_year, "test", "train")
    output["検証用_時系列カットオフ年"] = cutoff_year
    output["検証用_銘柄GroupFold"] = output["銘柄"].astype(str).map(
        lambda ticker: int(hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:8], 16) % group_folds
    )
    return output


def _spearman_value(x: pd.Series, y: pd.Series) -> float | None:
    valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 3:
        return None
    ranked_x, ranked_y = x[valid].rank(), y[valid].rank()
    if ranked_x.nunique() < 2 or ranked_y.nunique() < 2:
        return None
    value = np.corrcoef(ranked_x, ranked_y)[0, 1]
    return float(value) if np.isfinite(value) else None


def time_split_feature_validation(
    frame: pd.DataFrame,
    features: list[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for target in PRIMARY_MODEL_TARGETS:
        if target not in frame.columns:
            continue
        y = pd.to_numeric(frame[target], errors="coerce")
        for feature in features:
            x = pd.to_numeric(frame[feature], errors="coerce")
            train = frame["検証用_時系列区分"].eq("train") & x.notna() & y.notna()
            test = frame["検証用_時系列区分"].eq("test") & x.notna() & y.notna()
            if train.sum() < 10 or test.sum() < 8:
                continue
            train_corr = _spearman_value(x[train], y[train])
            test_corr = _spearman_value(x[test], y[test])
            rows.append({
                "目的変数": target, "説明変数": feature,
                "学習件数": int(train.sum()), "検証件数": int(test.sum()),
                "学習Spearman": train_corr, "検証Spearman": test_corr,
                "方向一致": bool(train_corr is not None and test_corr is not None and train_corr * test_corr > 0),
                "検証絶対相関": abs(test_corr) if test_corr is not None else None,
            })
    return pd.DataFrame(rows).sort_values(
        ["方向一致", "検証絶対相関"], ascending=[False, False]
    ) if rows else pd.DataFrame(columns=["目的変数", "説明変数", "学習件数", "検証件数", "学習Spearman", "検証Spearman", "方向一致", "検証絶対相関"])


def group_fold_feature_validation(
    frame: pd.DataFrame,
    features: list[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    folds = sorted(pd.to_numeric(frame["検証用_銘柄GroupFold"], errors="coerce").dropna().unique())
    for target in PRIMARY_MODEL_TARGETS:
        if target not in frame.columns:
            continue
        y = pd.to_numeric(frame[target], errors="coerce")
        for feature in features:
            x = pd.to_numeric(frame[feature], errors="coerce")
            correlations: list[float] = []
            counts: list[int] = []
            for fold in folds:
                test = frame["検証用_銘柄GroupFold"].eq(fold) & x.notna() & y.notna()
                if test.sum() < 5:
                    continue
                correlation = _spearman_value(x[test], y[test])
                if correlation is not None:
                    correlations.append(correlation)
                    counts.append(int(test.sum()))
            if len(correlations) < 3:
                continue
            median = float(np.median(correlations))
            direction_ratio = max(
                sum(value > 0 for value in correlations),
                sum(value < 0 for value in correlations),
            ) / len(correlations)
            rows.append({
                "目的変数": target, "説明変数": feature,
                "有効Fold数": len(correlations), "Fold件数最小": min(counts),
                "Fold件数最大": max(counts), "Fold相関中央値": median,
                "Fold相関最小": min(correlations), "Fold相関最大": max(correlations),
                "方向一致率": direction_ratio,
            })
    return pd.DataFrame(rows).sort_values(
        ["方向一致率", "Fold相関中央値"],
        key=lambda series: series.abs() if series.name == "Fold相関中央値" else series,
        ascending=[False, False],
    ) if rows else pd.DataFrame(columns=["目的変数", "説明変数", "有効Fold数", "Fold件数最小", "Fold件数最大", "Fold相関中央値", "Fold相関最小", "Fold相関最大", "方向一致率"])


def backward_expanding_correlation_validation(
    frame: pd.DataFrame,
    features: list[str],
    minimum_pairs: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.to_datetime(frame.get("底打ち候補日"), errors="coerce")
    years = dates.dt.year.dropna().astype(int)
    correlation_columns = [
        "対象期間", "開始年", "終了年", "イベント数", "企業数", "目的変数",
        "説明変数", "有効件数", "Pearson相関", "Spearman相関",
        "直前窓Spearman", "直前窓からの変化", "直前窓と方向一致",
    ]
    summary_columns = ["対象期間", "開始年", "終了年", "イベント数", "企業数"]
    stability_columns = [
        "目的変数", "説明変数", "有効窓数", "全窓方向一致",
        "方向一致率", "Spearman中央値", "Spearman最小", "Spearman最大",
        "絶対相関最小", "絶対相関中央値",
    ]
    if years.empty:
        return pd.DataFrame(columns=correlation_columns), pd.DataFrame(columns=summary_columns), pd.DataFrame(columns=stability_columns)

    end_year = max(date.today().year, int(years.max()))
    minimum_year = int(years.min())
    numeric_targets = {
        target: pd.to_numeric(frame[target], errors="coerce")
        for target in PRIMARY_MODEL_TARGETS if target in frame.columns
    }
    numeric_features = {
        feature: pd.to_numeric(frame[feature], errors="coerce") for feature in features
    }
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    previous: dict[tuple[str, str], float] = {}
    for start_year in range(end_year, minimum_year - 1, -1):
        window = dates.dt.year.ge(start_year) & dates.dt.year.le(end_year)
        window_label = f"現在～{start_year}年（開始年を遡及）"
        summaries.append({
            "対象期間": window_label, "開始年": start_year, "終了年": end_year,
            "イベント数": int(window.sum()),
            "企業数": int(frame.loc[window, "銘柄"].nunique()) if "銘柄" in frame.columns else 0,
        })
        for target in PRIMARY_MODEL_TARGETS:
            if target not in frame.columns:
                continue
            y = numeric_targets[target]
            for feature in features:
                x = numeric_features[feature]
                valid = window & x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
                if valid.sum() < minimum_pairs or x[valid].nunique() < 2 or y[valid].nunique() < 2:
                    continue
                pearson = float(np.corrcoef(x[valid], y[valid])[0, 1])
                spearman = _spearman_value(x[valid], y[valid])
                if spearman is None:
                    continue
                key = (target, feature)
                prior = previous.get(key)
                rows.append({
                    "対象期間": window_label, "開始年": start_year, "終了年": end_year,
                    "イベント数": int(window.sum()),
                    "企業数": int(frame.loc[window, "銘柄"].nunique()) if "銘柄" in frame.columns else 0,
                    "目的変数": target, "説明変数": feature,
                    "有効件数": int(valid.sum()), "Pearson相関": pearson,
                    "Spearman相関": spearman, "直前窓Spearman": prior,
                    "直前窓からの変化": spearman - prior if prior is not None else None,
                    "直前窓と方向一致": bool(prior is not None and spearman * prior > 0) if prior is not None else None,
                })
                previous[key] = spearman

    correlations = pd.DataFrame(rows, columns=correlation_columns)
    summaries_frame = pd.DataFrame(summaries, columns=summary_columns)
    stability_rows: list[dict[str, Any]] = []
    if not correlations.empty:
        for (target, feature), group in correlations.groupby(["目的変数", "説明変数"]):
            values = pd.to_numeric(group["Spearman相関"], errors="coerce").dropna()
            if values.empty:
                continue
            positive, negative = int(values.gt(0).sum()), int(values.lt(0).sum())
            majority = max(positive, negative)
            stability_rows.append({
                "目的変数": target, "説明変数": feature, "有効窓数": len(values),
                "全窓方向一致": bool(positive == len(values) or negative == len(values)),
                "方向一致率": majority / len(values),
                "Spearman中央値": float(values.median()),
                "Spearman最小": float(values.min()), "Spearman最大": float(values.max()),
                "絶対相関最小": float(values.abs().min()),
                "絶対相関中央値": float(values.abs().median()),
            })
    stability = pd.DataFrame(stability_rows, columns=stability_columns)
    if not stability.empty:
        stability = stability.sort_values(
            ["全窓方向一致", "方向一致率", "絶対相関最小", "有効窓数"],
            ascending=[False, False, False, False],
        )
    return correlations, summaries_frame, stability


def industry_analysis(
    frame: pd.DataFrame,
    features: list[str],
    minimum_pairs: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    correlation_rows: list[dict[str, Any]] = []
    if "SEC_SIC大分類" not in frame.columns:
        return pd.DataFrame(), pd.DataFrame()
    for industry, group in frame.groupby("SEC_SIC大分類", dropna=False):
        summary: dict[str, Any] = {
            "SIC大分類": industry, "イベント数": len(group),
            "企業数": int(group["銘柄"].nunique()) if "銘柄" in group.columns else 0,
        }
        for target in PRIMARY_MODEL_TARGETS:
            if target in group.columns:
                values = pd.to_numeric(group[target], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
                summary[f"{target}_件数"] = len(values)
                summary[f"{target}_平均"] = float(values.mean()) if len(values) else None
                summary[f"{target}_中央値"] = float(values.median()) if len(values) else None
        summary_rows.append(summary)
        for target in PRIMARY_MODEL_TARGETS:
            if target not in group.columns:
                continue
            y = pd.to_numeric(group[target], errors="coerce")
            for feature in features:
                x = pd.to_numeric(group[feature], errors="coerce")
                valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
                if valid.sum() < minimum_pairs or x[valid].nunique() < 2 or y[valid].nunique() < 2:
                    continue
                correlation_rows.append({
                    "SIC大分類": industry, "目的変数": target, "説明変数": feature,
                    "件数": int(valid.sum()),
                    "Pearson相関": float(np.corrcoef(x[valid], y[valid])[0, 1]),
                    "Spearman相関": _spearman_value(x[valid], y[valid]),
                })
    summary_frame = pd.DataFrame(summary_rows)
    correlations = pd.DataFrame(correlation_rows)
    if not correlations.empty:
        correlations["絶対Spearman"] = pd.to_numeric(correlations["Spearman相関"], errors="coerce").abs()
        correlations = correlations.sort_values(["SIC大分類", "目的変数", "絶対Spearman"], ascending=[True, True, False])
    return summary_frame, correlations


def add_point_in_time_industry_features(frame: pd.DataFrame, db_path: Path) -> pd.DataFrame:
    """Compare each event with SIC peers using prices no later than the event date."""
    output = frame.copy()
    required = {"銘柄", "底打ち候補日", "SEC_SIC", "SEC_SIC大分類"}
    if output.empty or not required.issubset(output.columns):
        return output

    ticker_sic: dict[str, str] = {}
    ticker_major: dict[str, str] = {}
    for ticker, group in output.groupby("銘柄", sort=False):
        sic_values = group["SEC_SIC"].dropna().astype(str).str.replace(r"\.0$", "", regex=True)
        major_values = group["SEC_SIC大分類"].dropna().astype(str)
        ticker_sic[str(ticker)] = sic_values.iloc[0].zfill(4) if len(sic_values) and sic_values.iloc[0] else ""
        ticker_major[str(ticker)] = major_values.iloc[0] if len(major_values) else ""

    relevant_tickers = sorted(set(ticker_sic))
    price_arrays: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    with sqlite3.connect(db_path) as connection:
        for start in range(0, len(relevant_tickers), 500):
            chunk = relevant_tickers[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            prices = pd.read_sql_query(
                f"SELECT ticker,price_date,close FROM prices WHERE ticker IN ({placeholders}) ORDER BY ticker,price_date",
                connection, params=chunk,
            )
            if prices.empty:
                continue
            prices["price_date"] = pd.to_datetime(prices["price_date"], errors="coerce")
            prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
            prices = prices.dropna(subset=["price_date", "close"])
            for ticker, group in prices.groupby("ticker", sort=False):
                price_arrays[str(ticker)] = (
                    group["price_date"].to_numpy(dtype="datetime64[D]"),
                    group["close"].to_numpy(dtype=float),
                )

    sic2_groups: dict[str, list[str]] = {}
    major_groups: dict[str, list[str]] = {}
    for ticker in relevant_tickers:
        sic = ticker_sic.get(ticker, "")
        major = ticker_major.get(ticker, "")
        if sic:
            sic2_groups.setdefault(sic[:2], []).append(ticker)
        if major:
            major_groups.setdefault(major, []).append(ticker)

    horizons = {"1か月": 21, "3か月": 63, "6か月": 126, "12か月": 252}

    def metrics_at(ticker: str, cutoff: np.datetime64) -> tuple[dict[str, float], np.datetime64 | None]:
        arrays = price_arrays.get(ticker)
        if arrays is None:
            return {}, None
        dates, closes = arrays
        position = int(np.searchsorted(dates, cutoff, side="right")) - 1
        if position < 0:
            return {}, None
        used_date = dates[position]
        if used_date > cutoff:
            raise RuntimeError(f"look-ahead detected: ticker={ticker}, used={used_date}, cutoff={cutoff}")
        result: dict[str, float] = {}
        current = closes[position]
        if not np.isfinite(current) or current <= 0:
            return {}, used_date
        for label, days in horizons.items():
            prior_position = position - days
            if prior_position >= 0 and closes[prior_position] > 0:
                result[f"{label}リターン"] = (current / closes[prior_position] - 1.0) * 100.0
        start = max(0, position - 251)
        trailing_high = np.nanmax(closes[start:position + 1])
        if np.isfinite(trailing_high) and trailing_high > 0:
            result["12か月高値からの下落率"] = (current / trailing_high - 1.0) * 100.0
        return result, used_date

    rows: list[dict[str, Any]] = []
    for _, event in output.iterrows():
        ticker = str(event.get("銘柄", ""))
        event_date = pd.to_datetime(event.get("底打ち候補日"), errors="coerce")
        additions: dict[str, Any] = {
            "同業比較_SEC_未来価格参照件数": 0,
            "同業比較_SEC_分類基準": "SEC SIC静的分類・数値はイベント日時点限定",
        }
        if pd.isna(event_date):
            rows.append(additions)
            continue
        cutoff = np.datetime64(event_date.date(), "D")
        sic = ticker_sic.get(ticker, "")
        exact_peers = sic2_groups.get(sic[:2], []) if sic else []
        if len(exact_peers) >= 4:
            peers, level = exact_peers, "SIC2桁"
        else:
            peers, level = major_groups.get(ticker_major.get(ticker, ""), []), "SIC大分類"
        peers = [peer for peer in peers if peer != ticker and peer in price_arrays]
        target_metrics, target_used_date = metrics_at(ticker, cutoff)
        peer_metrics: list[dict[str, float]] = []
        latest_used = target_used_date
        for peer in peers:
            values, used_date = metrics_at(peer, cutoff)
            if values:
                peer_metrics.append(values)
            if used_date is not None and (latest_used is None or used_date > latest_used):
                latest_used = used_date
        if latest_used is not None and latest_used > cutoff:
            raise RuntimeError(f"industry look-ahead detected: used={latest_used}, cutoff={cutoff}")
        additions["同業比較_SEC_分類粒度"] = level
        additions["同業比較_SEC_同業他社数"] = len(peer_metrics)
        additions["同業比較_SEC_情報カットオフ日"] = event_date.date().isoformat()
        additions["同業比較_SEC_最大参照価格日"] = str(latest_used) if latest_used is not None else ""
        for metric_name in (*[f"{label}リターン" for label in horizons], "12か月高値からの下落率"):
            values = np.array(
                [item[metric_name] for item in peer_metrics if metric_name in item], dtype=float
            )
            if len(values) < 3:
                continue
            median = float(np.nanmedian(values))
            additions[f"同業比較_SEC_同業中央値_{metric_name}"] = median
            additions[f"同業比較_SEC_同業マイナス企業率_{metric_name}"] = float(np.mean(values < 0))
            if metric_name in target_metrics:
                target_value = target_metrics[metric_name]
                additions[f"同業比較_SEC_自社平均との差_{metric_name}"] = target_value - median
                additions[f"同業比較_SEC_自社業界内百分位_{metric_name}"] = float(np.mean(values <= target_value))
        rows.append(additions)

    additions_frame = pd.DataFrame(rows, index=output.index)
    return pd.concat([output, additions_frame], axis=1)


def _regression_metrics(y: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    if len(y) == 0:
        return {"corr": float("nan"), "r2": float("nan"), "mae": float("nan")}
    corr = float(np.corrcoef(y, prediction)[0, 1]) if np.std(y) > 0 and np.std(prediction) > 0 else float("nan")
    denominator = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - float(np.sum((y - prediction) ** 2)) / denominator if denominator > 0 else float("nan")
    return {"corr": corr, "r2": r2, "mae": float(np.mean(np.abs(y - prediction)))}


def _ridge_coefficients(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    return np.linalg.solve((x.T @ x) / len(y) + alpha * np.eye(x.shape[1]), (x.T @ y) / len(y))


def _elastic_coefficients(
    x: np.ndarray,
    y: np.ndarray,
    alpha: float,
    l1_ratio: float = 0.5,
    max_iter: int = 3000,
) -> np.ndarray:
    beta = np.zeros(x.shape[1], dtype=float)
    feature_norm = np.mean(x * x, axis=0)
    feature_norm[feature_norm == 0] = 1.0
    for _ in range(max_iter):
        old = beta.copy()
        prediction = x @ beta
        for position in range(x.shape[1]):
            residual = y - prediction + beta[position] * x[:, position]
            rho = float(np.dot(x[:, position], residual) / len(y))
            threshold = alpha * l1_ratio
            numerator = max(rho - threshold, 0.0) if rho > 0 else min(rho + threshold, 0.0)
            updated = numerator / (feature_norm[position] + alpha * (1 - l1_ratio))
            prediction += (updated - beta[position]) * x[:, position]
            beta[position] = updated
        if np.max(np.abs(beta - old)) < 1e-7:
            break
    return beta


def automated_regularized_models(
    frame: pd.DataFrame,
    features: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summaries: list[dict[str, Any]] = []
    coefficients: list[dict[str, Any]] = []
    predictions: list[pd.DataFrame] = []
    buckets: list[dict[str, Any]] = []
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    alpha_values = (0.001, 0.01, 0.1, 1.0, 10.0)
    for target in PRIMARY_MODEL_TARGETS:
        if target not in frame.columns:
            continue
        y_all = pd.to_numeric(frame[target], errors="coerce")
        train_mask = frame["検証用_時系列区分"].eq("train") & y_all.notna() & dates.notna()
        test_mask = frame["検証用_時系列区分"].eq("test") & y_all.notna() & dates.notna()
        if train_mask.sum() < 15 or test_mask.sum() < 8:
            continue
        candidate_values = pd.DataFrame({feature: pd.to_numeric(frame[feature], errors="coerce") for feature in features})
        train_positions = np.where(train_mask)[0]
        test_positions = np.where(test_mask)[0]
        usable = [
            feature for feature in features
            if candidate_values.loc[train_mask, feature].notna().mean() >= 0.70
            and candidate_values.loc[train_mask, feature].nunique(dropna=True) >= 2
        ]
        ranked: list[tuple[float, str]] = []
        for feature in usable:
            correlation = _spearman_value(candidate_values.loc[train_mask, feature], y_all[train_mask])
            if correlation is not None:
                ranked.append((abs(correlation), feature))
        maximum_features = max(3, min(40, int(train_mask.sum()) // 4))
        selected = [feature for _, feature in sorted(ranked, reverse=True)[:maximum_features]]
        if not selected:
            continue
        medians = candidate_values.loc[train_mask, selected].median()
        x_all = candidate_values[selected].fillna(medians).to_numpy(dtype=float)
        finite_columns = np.isfinite(x_all[train_positions]).all(axis=0)
        selected = [feature for feature, keep in zip(selected, finite_columns) if keep]
        x_all = x_all[:, finite_columns]
        if not selected:
            continue
        train_order = train_positions[np.argsort(dates.iloc[train_positions].to_numpy())]
        inner_size = max(3, int(round(len(train_order) * 0.20)))
        inner_train, inner_validation = train_order[:-inner_size], train_order[-inner_size:]
        if len(inner_train) < 8:
            continue
        for method in ("Ridge", "ElasticNet"):
            best_alpha, best_mae = None, math.inf
            for alpha in alpha_values:
                x_mean, x_std = x_all[inner_train].mean(axis=0), x_all[inner_train].std(axis=0)
                x_std[x_std == 0] = 1.0
                y_mean, y_std = float(y_all.iloc[inner_train].mean()), float(y_all.iloc[inner_train].std()) or 1.0
                x_train_std = (x_all[inner_train] - x_mean) / x_std
                y_train_std = (y_all.iloc[inner_train].to_numpy(dtype=float) - y_mean) / y_std
                beta = _ridge_coefficients(x_train_std, y_train_std, alpha) if method == "Ridge" else _elastic_coefficients(x_train_std, y_train_std, alpha)
                validation_prediction = ((x_all[inner_validation] - x_mean) / x_std @ beta) * y_std + y_mean
                validation_mae = float(np.mean(np.abs(y_all.iloc[inner_validation].to_numpy(dtype=float) - validation_prediction)))
                if validation_mae < best_mae:
                    best_alpha, best_mae = alpha, validation_mae
            if best_alpha is None:
                continue
            x_mean, x_std = x_all[train_positions].mean(axis=0), x_all[train_positions].std(axis=0)
            x_std[x_std == 0] = 1.0
            y_mean, y_std = float(y_all.iloc[train_positions].mean()), float(y_all.iloc[train_positions].std()) or 1.0
            x_train_std = (x_all[train_positions] - x_mean) / x_std
            y_train_std = (y_all.iloc[train_positions].to_numpy(dtype=float) - y_mean) / y_std
            beta = _ridge_coefficients(x_train_std, y_train_std, best_alpha) if method == "Ridge" else _elastic_coefficients(x_train_std, y_train_std, best_alpha)
            prediction_all = ((x_all - x_mean) / x_std @ beta) * y_std + y_mean
            train_metrics = _regression_metrics(y_all.iloc[train_positions].to_numpy(dtype=float), prediction_all[train_positions])
            test_metrics = _regression_metrics(y_all.iloc[test_positions].to_numpy(dtype=float), prediction_all[test_positions])
            summaries.append({
                "目的変数": target, "手法": method, "alpha": best_alpha,
                "内部検証MAE": best_mae, "特徴量数": len(selected),
                "学習件数": len(train_positions), "検証件数": len(test_positions),
                "学習相関": train_metrics["corr"], "検証相関": test_metrics["corr"],
                "学習R2": train_metrics["r2"], "検証R2": test_metrics["r2"],
                "学習MAE": train_metrics["mae"], "検証MAE": test_metrics["mae"],
            })
            for feature, weight in zip(selected, beta):
                coefficients.append({
                    "目的変数": target, "手法": method, "alpha": best_alpha,
                    "特徴量": feature, "標準化係数": weight, "絶対係数": abs(weight),
                })
            prediction_frame = frame.loc[train_mask | test_mask, [
                column for column in ("銘柄", "企業名", "底打ち候補日", "検証用_時系列区分", "検証用_銘柄GroupFold")
                if column in frame.columns
            ]].copy()
            positions = np.where((train_mask | test_mask).to_numpy())[0]
            prediction_frame["目的変数"] = target
            prediction_frame["手法"] = method
            prediction_frame["実績値"] = y_all.iloc[positions].to_numpy(dtype=float)
            prediction_frame["予測値"] = prediction_all[positions]
            predictions.append(prediction_frame)
            test_prediction = prediction_frame[prediction_frame["検証用_時系列区分"].eq("test")].sort_values("予測値", ascending=False).copy()
            if len(test_prediction) >= 5:
                rank = np.arange(len(test_prediction))
                top = max(1, math.ceil(len(test_prediction) * 0.20))
                bottom = max(top, math.floor(len(test_prediction) * 0.80))
                test_prediction["分位"] = np.where(rank < top, "上位20%", np.where(rank >= bottom, "下位20%", "中位60%"))
                for bucket, group in test_prediction.groupby("分位"):
                    buckets.append({
                        "目的変数": target, "手法": method, "分位": bucket,
                        "件数": len(group), "予測平均": float(group["予測値"].mean()),
                        "実績平均": float(group["実績値"].mean()),
                        "実績中央値": float(group["実績値"].median()),
                    })
    return (
        pd.DataFrame(summaries), pd.DataFrame(coefficients),
        pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame(),
        pd.DataFrame(buckets),
    )


@dataclass
class PipelineConfig:
    output_dir: Path
    legacy_app_path: Path
    sec_user_agent: str
    target_companies: int = 1500
    liquidity_batch_size: int = 100
    price_batch_size: int = 30
    minimum_price: float = 1.0
    minimum_dollar_volume: float = 1_000_000.0
    price_start: str = "2010-01-01"
    signal_start: str = "2012-01-01"
    refresh_universe: bool = False
    resume: bool = False


def pipeline_config_fingerprint(config: PipelineConfig) -> dict[str, Any]:
    return {
        "target_companies": config.target_companies,
        "minimum_price": config.minimum_price,
        "minimum_dollar_volume": config.minimum_dollar_volume,
        "price_start": config.price_start,
        "signal_start": config.signal_start,
        "legacy_app_sha256": file_sha256(config.legacy_app_path),
    }


def run_pipeline(config: PipelineConfig, progress: ProgressCallback | None = None, stop_event: threading.Event | None = None) -> dict[str, Any]:
    stop_event = stop_event or threading.Event()
    started = time.perf_counter()
    stage_started = started
    stage_timings: dict[str, float] = {}

    def finish_stage(name: str) -> None:
        nonlocal stage_started
        now = time.perf_counter()
        stage_timings[name] = round(now - stage_started, 3)
        stage_started = now

    config.output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = config.output_dir / "cache"
    db_path = config.output_dir / "us_stock_research.db"
    state_path = config.output_dir / "pipeline_state.json"
    fingerprint = pipeline_config_fingerprint(config)

    def save_state(phase: str, status: str = "running", **extra: Any) -> None:
        atomic_json_write(state_path, {
            "app_version": APP_VERSION, "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": status, "phase": phase, "config": fingerprint, **extra,
        })

    if config.resume and state_path.exists():
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))
        if previous_state.get("config") != fingerprint:
            raise RuntimeError(
                "前回チェックポイントと現在の設定が異なります。"
                "対象企業数・日付・閾値を前回と同じにするか、通常の開始ボタンを使ってください。"
            )
        emit(progress, phase="resume", message=f"チェックポイントを確認しました: {previous_state.get('phase', '不明')}")
    elif config.resume:
        emit(progress, phase="resume", message="状態ファイルがないため、既存CSVとキャッシュから再開地点を復元します")
    save_state("initializing")
    ensure_database(db_path)
    algorithm = load_legacy_algorithm(config.legacy_app_path)
    yf = load_yfinance()
    client = SecClient(config.sec_user_agent, cache_dir / "sec", request_interval=0.15)
    emit(progress, phase="universe", message="SECから現在上場企業一覧を取得しています")
    universe = fetch_universe(client, cache_dir, config.refresh_universe)
    universe.to_csv(config.output_dir / "us_current_universe.csv", index=False, encoding="utf-8-sig")
    finish_stage("initialization_and_universe")
    selected_path = config.output_dir / "us_selected_companies.csv"
    diagnostics_path = config.output_dir / "liquidity_diagnostics.csv"
    selected = pd.DataFrame()
    if config.resume and selected_path.exists():
        try:
            selected = pd.read_csv(selected_path, encoding="utf-8-sig", low_memory=False)
        except Exception:
            selected = pd.DataFrame()
    if not selected.empty:
        liquidity_diagnostics = pd.read_csv(diagnostics_path, encoding="utf-8-sig", low_memory=False) if diagnostics_path.exists() else selected.copy()
        emit(progress, phase="resume", message=f"企業選定を再利用しました: {len(selected)}社")
    else:
        selected, liquidity_diagnostics = liquidity_screen(
            yf, universe, config.target_companies, config.liquidity_batch_size,
            config.minimum_price, config.minimum_dollar_volume, progress, stop_event,
        )
        liquidity_diagnostics.to_csv(diagnostics_path, index=False, encoding="utf-8-sig")
        selected.to_csv(selected_path, index=False, encoding="utf-8-sig")
    finish_stage("liquidity_screen")
    save_state("liquidity_complete", selected_companies=len(selected))
    if stop_event.is_set():
        save_state("liquidity_complete", "stopped", selected_companies=len(selected))
        return {"stopped": True, "phase": "liquidity", "selected_companies": len(selected)}
    if selected.empty:
        failed = int(liquidity_diagnostics["取得状態"].eq("取得失敗").sum())
        priced = int(liquidity_diagnostics["取得状態"].eq("取得成功").sum())
        raise RuntimeError(
            "流動性スクリーニングで選定企業が0社でした。"
            f"価格取得成功={priced}社、価格取得失敗={failed}社。"
            "liquidity_diagnostics.csv の取得失敗理由を確認してください。"
        )
    price_errors = collect_long_prices(yf, selected, db_path, config.price_start, config.price_batch_size, progress, stop_event)
    finish_stage("long_price_collection")
    if stop_event.is_set():
        save_state("prices", "stopped", selected_companies=len(selected), price_errors=len(price_errors))
        return {"stopped": True, "phase": "prices", "selected_companies": len(selected), "price_errors": len(price_errors)}
    save_state("prices_complete", selected_companies=len(selected), price_errors=len(price_errors))
    raw_path = config.output_dir / "us_bottom_events.csv"
    events: list[dict[str, Any]] = []
    detection_errors: dict[str, str] = {}
    if config.resume and raw_path.exists():
        try:
            cached_events = pd.read_csv(raw_path, encoding="utf-8-sig", low_memory=False)
            versions = cached_events.get("底検知アルゴリズムver", pd.Series(dtype=str)).dropna().astype(str).unique()
            if (
                len(cached_events)
                and "暴落開始時価格" in cached_events.columns
                and set(versions) == {str(getattr(algorithm, "ALGORITHM_VERSION", ""))}
            ):
                events = cached_events.to_dict("records")
                emit(progress, phase="resume", message=f"底検知結果を再利用しました: {len(events)}イベント")
        except Exception:
            events = []
    if not events:
        events, detection_errors = detect_events(
            selected, db_path, algorithm, config.price_start, config.signal_start,
            progress, stop_event,
            checkpoint_events_path=config.output_dir / "bottom_detection_checkpoint.csv",
            checkpoint_state_path=config.output_dir / "bottom_detection_checkpoint.json",
            resume=config.resume,
        )
        pd.DataFrame(events).to_csv(raw_path, index=False, encoding="utf-8-sig")
    finish_stage("bottom_detection")
    if stop_event.is_set():
        save_state("detect", "stopped", selected_companies=len(selected), events=len(events))
        return {"stopped": True, "phase": "detect", "selected_companies": len(selected), "events": len(events)}
    save_state("detect_complete", selected_companies=len(selected), events=len(events))
    sec_checkpoint_path = config.output_dir / "sec_enrichment_checkpoint.csv"
    enriched = enrich_events_with_sec(
        events, client, progress, stop_event,
        checkpoint_path=sec_checkpoint_path, resume=config.resume,
    )
    finish_stage("sec_collection_and_enrichment")
    if stop_event.is_set():
        partial_path = config.output_dir / "us_bottom_events_with_sec_partial.csv"
        pd.DataFrame(enriched).to_csv(partial_path, index=False, encoding="utf-8-sig")
        save_state("sec", "stopped", selected_companies=len(selected), events=len(events), sec_completed=len(enriched))
        return {
            "stopped": True, "phase": "sec", "selected_companies": len(selected),
            "events": len(events), "sec_completed": len(enriched), "partial_csv": str(partial_path),
        }
    save_state("sec_complete", selected_companies=len(selected), events=len(events), sec_completed=len(enriched))
    enriched_frame = add_point_in_time_industry_features(pd.DataFrame(enriched), db_path)
    enriched_frame = add_validation_splits(enriched_frame, cutoff_year=2023, group_folds=5)
    enriched_path = config.output_dir / "us_bottom_events_with_sec.csv"
    enriched_path = resilient_csv_write(enriched_frame, enriched_path)
    peer_audit_path = config.output_dir / "point_in_time_peer_audit.csv"
    peer_audit_columns = [
        column for column in (
            "銘柄", "企業名", "底打ち候補日", "SEC_SIC", "SEC_SIC大分類",
            "同業比較_SEC_分類基準", "同業比較_SEC_分類粒度", "同業比較_SEC_同業他社数",
            "同業比較_SEC_情報カットオフ日", "同業比較_SEC_最大参照価格日",
            "同業比較_SEC_未来価格参照件数",
        ) if column in enriched_frame.columns
    ]
    peer_audit_path = resilient_csv_write(enriched_frame[peer_audit_columns], peer_audit_path)
    coverage_path = config.output_dir / "financial_feature_coverage.csv"
    coverage_path = resilient_csv_write(financial_feature_coverage(enriched_frame), coverage_path)
    model_manifest = build_model_feature_manifest(enriched_frame)
    model_manifest_path = config.output_dir / "model_feature_manifest.csv"
    model_manifest_path = resilient_csv_write(model_manifest, model_manifest_path)
    selected_features = model_manifest.loc[model_manifest["モデル採用"].eq(True), "特徴量"].tolist()
    identity_columns = [
        column for column in (
            "銘柄", "企業名", "取引所", "CIK", "底打ち候補日",
            "検証用_時系列区分", "検証用_時系列カットオフ年", "検証用_銘柄GroupFold",
        ) if column in enriched_frame.columns
    ]
    target_columns = [column for column in PRIMARY_MODEL_TARGETS if column in enriched_frame.columns]
    model_ready_path = config.output_dir / "model_ready_dataset.csv"
    model_ready_path = resilient_csv_write(
        enriched_frame[identity_columns + target_columns + selected_features], model_ready_path
    )
    time_validation_path = config.output_dir / "time_split_feature_validation.csv"
    time_validation_path = resilient_csv_write(
        time_split_feature_validation(enriched_frame, selected_features), time_validation_path
    )
    group_validation_path = config.output_dir / "group_fold_feature_validation.csv"
    group_validation_path = resilient_csv_write(
        group_fold_feature_validation(enriched_frame, selected_features), group_validation_path
    )
    backward_correlations, backward_summary, backward_stability = backward_expanding_correlation_validation(
        enriched_frame, selected_features, minimum_pairs=10
    )
    backward_correlations_path = config.output_dir / "backward_expanding_correlations.csv"
    backward_summary_path = config.output_dir / "backward_expanding_window_summary.csv"
    backward_stability_path = config.output_dir / "backward_expanding_feature_stability.csv"
    backward_correlations_path = resilient_csv_write(backward_correlations, backward_correlations_path)
    backward_summary_path = resilient_csv_write(backward_summary, backward_summary_path)
    backward_stability_path = resilient_csv_write(backward_stability, backward_stability_path)
    industry_summary, industry_correlations = industry_analysis(enriched_frame, selected_features, minimum_pairs=10)
    industry_summary_path = config.output_dir / "industry_performance_summary.csv"
    industry_correlations_path = config.output_dir / "industry_feature_correlations.csv"
    industry_summary_path = resilient_csv_write(industry_summary, industry_summary_path)
    industry_correlations_path = resilient_csv_write(industry_correlations, industry_correlations_path)
    model_summary, model_coefficients, model_predictions, model_buckets = automated_regularized_models(
        enriched_frame, selected_features
    )
    automatic_model_summary_path = config.output_dir / "automatic_model_summary.csv"
    automatic_model_coefficients_path = config.output_dir / "automatic_model_coefficients.csv"
    automatic_model_predictions_path = config.output_dir / "automatic_model_predictions.csv"
    automatic_model_buckets_path = config.output_dir / "automatic_model_quantile_comparison.csv"
    automatic_model_summary_path = resilient_csv_write(model_summary, automatic_model_summary_path)
    automatic_model_coefficients_path = resilient_csv_write(model_coefficients, automatic_model_coefficients_path)
    automatic_model_predictions_path = resilient_csv_write(model_predictions, automatic_model_predictions_path)
    automatic_model_buckets_path = resilient_csv_write(model_buckets, automatic_model_buckets_path)
    split_manifest_path = config.output_dir / "validation_split_manifest.csv"
    if not enriched_frame.empty:
        split_manifest = enriched_frame.groupby(
            ["銘柄", "企業名", "検証用_銘柄GroupFold"], dropna=False
        ).agg(
            イベント数=("底打ち候補日", "size"),
            最初のイベント=("底打ち候補日", "min"),
            最後のイベント=("底打ち候補日", "max"),
        ).reset_index()
    else:
        split_manifest = pd.DataFrame(columns=[
            "銘柄", "企業名", "検証用_銘柄GroupFold", "イベント数",
            "最初のイベント", "最後のイベント",
        ])
    split_manifest_path = resilient_csv_write(split_manifest, split_manifest_path)
    correlations = correlation_report(enriched_frame)
    correlation_path = config.output_dir / "sec_change_correlations.csv"
    correlation_path = resilient_csv_write(correlations, correlation_path)
    finish_stage("validation_modeling_and_exports")
    report = {
        "app_version": APP_VERSION,
        "yfinance_version": getattr(yf, "__version__", "unknown"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "algorithm_version": getattr(algorithm, "ALGORITHM_VERSION", ""),
        "algorithm_sha256": file_sha256(config.legacy_app_path),
        "universe_count": len(universe), "selected_companies": len(selected),
        "event_count": len(events), "sec_success_events": int(pd.to_numeric(enriched_frame.get("底検知時_SEC_取得数", pd.Series(dtype=float)), errors="coerce").gt(0).sum()),
        "price_error_count": len(price_errors), "detection_error_count": len(detection_errors),
        "point_in_time_peer_feature_count": sum(
            column.startswith("同業比較_SEC_") and model_feature_eligibility(column)[0]
            for column in enriched_frame.columns
        ),
        "future_price_reference_count": int(pd.to_numeric(
            enriched_frame.get("同業比較_SEC_未来価格参照件数", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0).sum()),
        "stopped": stop_event.is_set(), "elapsed_seconds": round(time.perf_counter() - started, 2),
        "stage_timings_seconds": stage_timings,
        "outputs": {
            "selected_companies_csv": str(config.output_dir / "us_selected_companies.csv"),
            "liquidity_diagnostics_csv": str(config.output_dir / "liquidity_diagnostics.csv"),
            "events_csv": str(raw_path), "enriched_events_csv": str(enriched_path),
            "correlations_csv": str(correlation_path), "feature_coverage_csv": str(coverage_path),
            "model_feature_manifest_csv": str(model_manifest_path),
            "model_ready_dataset_csv": str(model_ready_path),
            "time_split_validation_csv": str(time_validation_path),
            "group_fold_validation_csv": str(group_validation_path),
            "validation_split_manifest_csv": str(split_manifest_path),
            "backward_expanding_correlations_csv": str(backward_correlations_path),
            "backward_expanding_window_summary_csv": str(backward_summary_path),
            "backward_expanding_feature_stability_csv": str(backward_stability_path),
            "industry_performance_summary_csv": str(industry_summary_path),
            "industry_feature_correlations_csv": str(industry_correlations_path),
            "point_in_time_peer_audit_csv": str(peer_audit_path),
            "automatic_model_summary_csv": str(automatic_model_summary_path),
            "automatic_model_coefficients_csv": str(automatic_model_coefficients_path),
            "automatic_model_predictions_csv": str(automatic_model_predictions_path),
            "automatic_model_quantile_comparison_csv": str(automatic_model_buckets_path),
            "database": str(db_path),
        },
        "price_errors": price_errors, "detection_errors": detection_errors,
    }
    (config.output_dir / "us_stock_research_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save_state("complete", "complete", selected_companies=len(selected), events=len(events), sec_completed=len(enriched))
    emit(progress, phase="done", **report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="米国株大量底検知＋SEC財務変化収集")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--legacy-app", type=Path, default=DEFAULT_LEGACY_APP)
    parser.add_argument("--sec-user-agent", required=True)
    parser.add_argument("--target-companies", type=int, default=1500)
    parser.add_argument("--price-start", default="2010-01-01")
    parser.add_argument("--signal-start", default="2012-01-01")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config = PipelineConfig(
        args.output_dir, args.legacy_app, args.sec_user_agent, args.target_companies,
        price_start=args.price_start, signal_start=args.signal_start, resume=args.resume,
    )
    report = run_pipeline(config, progress=lambda info: print(json.dumps(info, ensure_ascii=False), flush=True))
    return 0 if not report.get("stopped") else 2


if __name__ == "__main__":
    raise SystemExit(main())
