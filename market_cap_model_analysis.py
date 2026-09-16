from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Callable

import numpy as np
import pandas as pd


APP_VERSION = "0.5.0"
DEFAULT_LEGACY_APP = Path(r"C:\Users\saban\Documents\Codex\2026-06-23\python-csv\app.py")
DEFAULT_PRICE_DB = DEFAULT_LEGACY_APP.with_name("stock_cache.db")
DEFAULT_OUTPUT_DIR = Path("outputs/market_cap_model_analysis")
DEFAULT_MARKET_CAP_CSV = Path(__file__).resolve().parent / "data" / "current_market_cap_snapshot.csv"
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ModelPreset:
    model_id: str
    display_name: str
    stock_drawdown_percent: float
    recent_low_drawdown_percent: float
    stock_min_peak_age: int
    max_range_percent: float
    max_abs_return_percent: float
    setup_required_days: int
    breakout_min_return_percent: float
    expansion_return_percent: float
    breakout_volume_ratio: float
    expansion_volume_ratio: float
    confirmation_days: int
    hold_tolerance: float
    cooldown_days: int = 60


@dataclass(frozen=True)
class RoutingThresholdSet:
    name: str
    small_max_jpy: float
    large_min_jpy: float


DEFAULT_ROUTING_SETS = (
    RoutingThresholdSet("標準", 70_000_000_000.0, 500_000_000_000.0),
    RoutingThresholdSet("中型1100-3300", 110_000_000_000.0, 330_000_000_000.0),
    RoutingThresholdSet("中型広め", 50_000_000_000.0, 700_000_000_000.0),
)


MID_MODEL = ModelPreset(
    "JP-MID-BTM-v1", "中型株モデル（現行v5.3固定）",
    40.0, 45.0, 120, 30.0, 2.5, 10,
    2.0, 5.0, 1.5, 2.0, 2, 0.98, 60,
)
SMALL_MODEL = ModelPreset(
    "JP-SMALL-BTM-v0", "小型株モデル（実験）",
    52.5, 57.5, 100, 48.0, 4.5, 9,
    3.0, 6.0, 2.0, 2.75, 3, 0.97, 60,
)
LARGE_MODEL = ModelPreset(
    "JP-LARGE-BTM-v0", "大型株モデル（実験）",
    30.0, 35.0, 100, 22.0, 2.0, 10,
    1.5, 3.5, 1.25, 1.6, 3, 0.985, 60,
)


@dataclass
class AnalysisConfig:
    legacy_app_path: Path
    price_db_path: Path
    output_dir: Path
    start_date: str = "2012-01-01"
    end_date: str = "2024-12-31"
    currency: str = "JPY"
    benchmark_symbol: str = "ACWI"
    comparison_benchmark_symbols: tuple[str, ...] = ("ACWI", "SPY")
    company_limit: int = 0
    size_history_csv: Path | None = None
    small_max_jpy: float = 70_000_000_000.0
    large_min_jpy: float = 500_000_000_000.0
    run_all_models: bool = True
    resume: bool = False
    models: tuple[ModelPreset, ...] = (SMALL_MODEL, MID_MODEL, LARGE_MODEL)
    routing_threshold_sets: tuple[RoutingThresholdSet, ...] = DEFAULT_ROUTING_SETS


def parse_routing_threshold_sets(value: str) -> tuple[RoutingThresholdSet, ...]:
    sets: list[RoutingThresholdSet] = []
    names: set[str] = set()
    for raw_item in value.replace("\n", ";").split(";"):
        item = raw_item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 3:
            raise ValueError(f"境界セットは セット名:小型上限億円:大型下限億円 で指定してください: {item}")
        name, small_text, large_text = parts
        if not name or name in names:
            raise ValueError(f"境界セット名が空、または重複しています: {name or item}")
        small_max = float(small_text) * 100_000_000.0
        large_min = float(large_text) * 100_000_000.0
        if small_max <= 0 or large_min <= small_max:
            raise ValueError(f"時価総額境界が不正です: {item}")
        sets.append(RoutingThresholdSet(name, small_max, large_min))
        names.add(name)
    if not sets:
        raise ValueError("時価総額境界セットを1つ以上指定してください")
    return tuple(sets)


def emit(callback: ProgressCallback | None, **payload: Any) -> None:
    if callback:
        callback(payload)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def atomic_csv_write(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    try:
        temporary.replace(path)
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        temporary.replace(fallback)


def load_legacy_module(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"現行app.pyが見つかりません: {path}")
    spec = importlib.util.spec_from_file_location("legacy_market_cap_bottom_model", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"app.pyを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ("PricePoint", "SignalConfig", "DEFAULT_SIGNAL_CONFIG", "analyze_stability"):
        if not hasattr(module, name):
            raise RuntimeError(f"app.pyに必要な要素がありません: {name}")
    return module


def validate_database(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"株価DBが見つかりません: {path}")
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"stocks", "daily_prices"}.issubset(tables):
            raise RuntimeError("選択したDBにstocksまたはdaily_pricesテーブルがありません")


def available_companies(config: AnalysisConfig) -> pd.DataFrame:
    query = """
        SELECT s.symbol, s.name, s.currency, s.exchange, s.market_cap,
               MIN(p.price_date) AS price_start, MAX(p.price_date) AS price_end,
               COUNT(*) AS price_days
        FROM stocks s JOIN daily_prices p ON p.symbol=s.symbol
        WHERE s.security_type='stock' AND s.currency=?
        GROUP BY s.symbol, s.name, s.currency, s.exchange, s.market_cap
        HAVING COUNT(*) >= 600
        ORDER BY s.symbol
    """
    with sqlite3.connect(config.price_db_path) as connection:
        frame = pd.read_sql_query(query, connection, params=(config.currency,))
    if config.company_limit > 0:
        frame = frame.head(config.company_limit)
    return frame


def load_points(connection: sqlite3.Connection, symbol: str, module: Any) -> list[Any]:
    rows = connection.execute(
        """SELECT timestamp,price_date,close,high,low,volume
           FROM daily_prices WHERE symbol=? ORDER BY price_date""",
        (symbol,),
    ).fetchall()
    return [
        module.PricePoint(
            timestamp=int(row[0]), date_text=str(row[1]), close=float(row[2]),
            high=float(row[3]) if row[3] is not None else None,
            low=float(row[4]) if row[4] is not None else None,
            volume=int(row[5]) if row[5] is not None else None,
        )
        for row in rows if row[2] is not None and float(row[2]) > 0
    ]


def preset_to_signal_config(module: Any, preset: ModelPreset) -> Any:
    return replace(
        module.DEFAULT_SIGNAL_CONFIG,
        stock_drawdown_percent=preset.stock_drawdown_percent,
        recent_low_drawdown_percent=preset.recent_low_drawdown_percent,
        stock_min_peak_age=preset.stock_min_peak_age,
        max_range_percent=preset.max_range_percent,
        max_abs_return_percent=preset.max_abs_return_percent,
        setup_required_days=preset.setup_required_days,
        breakout_min_return_percent=preset.breakout_min_return_percent,
        expansion_return_percent=preset.expansion_return_percent,
        breakout_volume_ratio=preset.breakout_volume_ratio,
        expansion_volume_ratio=preset.expansion_volume_ratio,
        confirmation_days=preset.confirmation_days,
        hold_tolerance=preset.hold_tolerance,
        cooldown_days=preset.cooldown_days,
    )


def fast_analyze_stability(points: list[Any], module: Any, signal_config: Any) -> dict[str, Any]:
    """Run the legacy v5.3 signal rules without building UI-only daily series."""
    points = module.clean_price_points(points)
    minimum_days = max(
        signal_config.min_peak_history,
        signal_config.range_window,
        signal_config.breakout_window,
    ) + signal_config.confirmation_days
    if len(points) < minimum_days:
        return {"signals": [], "bottomEvaluations": [], "priceTriggerCount": 0}

    closes = [point.close for point in points]
    returns: list[float | None] = [None]
    for index in range(1, len(points)):
        returns.append((closes[index] / closes[index - 1] - 1) * 100)
    start_index = max(
        signal_config.min_peak_history,
        signal_config.range_window + 1,
        signal_config.volatility_window + 1,
        signal_config.breakout_window,
    )
    setup_flags = [False] * len(points)
    setup_details: dict[int, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    last_signal_index = -signal_config.cooldown_days
    preliminary_count = 0

    for index in range(start_index, len(points)):
        point = points[index]
        peak_start = max(0, index - signal_config.peak_window)
        peak_slice = closes[peak_start:index]
        rolling_peak = max(peak_slice)
        peak_relative_index = len(peak_slice) - 1 - peak_slice[::-1].index(rolling_peak)
        peak_index = peak_start + peak_relative_index
        peak_age = index - peak_index
        previous_close = closes[index - 1]
        recent = closes[index - signal_config.range_window:index]
        recent_low = min(recent)
        close_drawdown = (1 - previous_close / rolling_peak) * 100
        recent_low_drawdown = (1 - recent_low / rolling_peak) * 100
        drawdown_pass = (
            close_drawdown >= signal_config.stock_drawdown_percent
            or recent_low_drawdown >= max(
                signal_config.stock_drawdown_percent,
                signal_config.recent_low_drawdown_percent,
            )
        )
        time_pass = peak_age >= signal_config.stock_min_peak_age
        range_low, range_high = min(recent), max(recent)
        range_percent = (range_high - range_low) / range_low * 100 if range_low > 0 else 100.0
        range_pass = range_percent <= signal_config.max_range_percent
        volatility_values = [
            abs(value)
            for value in returns[index - signal_config.volatility_window:index]
            if value is not None
        ]
        volatility = mean(volatility_values)
        volatility_pass = volatility <= signal_config.max_abs_return_percent
        setup_pass = bool(drawdown_pass and time_pass and range_pass and volatility_pass)
        setup_flags[index] = setup_pass
        setup_details[index] = {
            "rollingPeak": round(rolling_peak, 4),
            "peakDate": points[peak_index].date_text,
            "peakAgeTradingDays": peak_age,
            "peakAgeMonths": round(peak_age / 21, 1),
            "drawdownPercent": round(close_drawdown, 2),
            "recentLowDrawdownPercent": round(recent_low_drawdown, 2),
            "range60": round(range_percent, 2),
            "volatility20": round(volatility, 2),
            "drawdownPass": drawdown_pass,
            "timePass": time_pass,
            "rangePass": range_pass,
            "volatilityPass": volatility_pass,
        }

        density_start = max(start_index, index - signal_config.setup_density_window)
        setup_days = sum(setup_flags[density_start:index])
        setup_ready = setup_days >= signal_config.setup_required_days
        prior_box_high = max(closes[index - signal_config.breakout_window:index])
        prior_short_high = max(closes[index - signal_config.expansion_window:index])
        daily_return = returns[index] or 0.0
        previous_volumes = [
            item.volume
            for item in points[index - signal_config.volatility_window:index]
            if item.volume is not None and item.volume > 0
        ]
        average_volume = mean(previous_volumes) if len(previous_volumes) == signal_config.volatility_window else None
        volume_ratio = (
            point.volume / average_volume
            if point.volume is not None and point.volume > 0 and average_volume else None
        )
        close_strength = (
            (point.close - point.low) / (point.high - point.low)
            if point.high is not None and point.low is not None and point.high > point.low else 0.0
        )
        breakout_level = prior_box_high * (1 + signal_config.breakout_buffer_percent / 100)
        expansion_level = prior_short_high * (1 + signal_config.breakout_buffer_percent / 100)
        volume_breakout = bool(
            setup_ready
            and point.close >= breakout_level
            and daily_return >= signal_config.breakout_min_return_percent
            and volume_ratio is not None
            and volume_ratio >= signal_config.breakout_volume_ratio
            and close_strength >= signal_config.close_strength_limit
        )
        volume_expansion = bool(
            setup_ready
            and point.close >= expansion_level
            and daily_return >= signal_config.expansion_return_percent
            and volume_ratio is not None
            and volume_ratio >= signal_config.expansion_volume_ratio
            and close_strength >= signal_config.close_strength_limit
        )
        trigger_type = "volume_breakout" if volume_breakout else "volume_expansion" if volume_expansion else None
        if trigger_type:
            preliminary_count += 1
            pending.append({
                "index": index,
                "date": point.date_text,
                "close": point.close,
                "level": breakout_level if volume_breakout else expansion_level,
                "type": trigger_type,
                "dailyReturn": daily_return,
                "volumeRatio": volume_ratio,
                "closeStrength": close_strength,
                "setupIndex": index - 1,
            })

        confirmed = None
        remaining: list[dict[str, Any]] = []
        for trigger in pending:
            age = index - trigger["index"]
            if age < signal_config.confirmation_days:
                remaining.append(trigger)
                continue
            if age > signal_config.confirmation_days:
                continue
            held = min(closes[trigger["index"]:index + 1]) >= trigger["level"] * signal_config.hold_tolerance
            followed = point.close >= trigger["close"]
            if held and followed and index - last_signal_index >= signal_config.cooldown_days and confirmed is None:
                confirmed = trigger
        pending = remaining
        if confirmed:
            last_signal_index = index
            setup = setup_details[confirmed["setupIndex"]]
            signals.append({
                "triggerDate": confirmed["date"],
                "confirmationDate": point.date_text,
                "triggerType": f'{confirmed["type"]}_confirmed',
                "triggerClose": round(confirmed["close"], 4),
                "confirmationClose": round(point.close, 4),
                "breakoutLevel": round(confirmed["level"], 4),
                "dailyReturn": round(confirmed["dailyReturn"], 2),
                "volumeRatio": round(confirmed["volumeRatio"], 2),
                "closeStrength": round(confirmed["closeStrength"], 2),
                **setup,
                **module.forward_performance(points, index, signal_config.forward_windows),
            })
    return {
        "signals": signals,
        "bottomEvaluations": module.bottom_signal_evaluations(signals, points),
        "priceTriggerCount": preliminary_count,
    }


def nearest_index(dates: list[str], target: str) -> int | None:
    position = int(np.searchsorted(np.asarray(dates), target, side="left"))
    return position if position < len(dates) else None


def completed_years(start_date: str) -> tuple[int, ...]:
    start_year = pd.Timestamp(start_date).year
    last_complete_year = datetime.now().year - 1
    return tuple(range(start_year, last_complete_year + 1))


def last_index_on_or_before(dates: list[str], target: str) -> int | None:
    position = int(np.searchsorted(np.asarray(dates), target, side="right")) - 1
    return position if position >= 0 else None


def stock_year_end_metrics(
    points: list[Any],
    signal_date: str,
    signal_price: float,
    years: tuple[int, ...],
) -> dict[str, Any]:
    if not points or not math.isfinite(signal_price) or signal_price <= 0:
        return {}
    dates = [point.date_text for point in points]
    output: dict[str, Any] = {}
    for year in years:
        date_column = f"{year}年末評価日"
        return_column = f"{year}年末まで保有リターン"
        if signal_date > f"{year}-12-31":
            output[date_column] = None
            output[return_column] = None
            continue
        position = last_index_on_or_before(dates, f"{year}-12-31")
        if position is None or dates[position] < signal_date or not dates[position].startswith(f"{year}-"):
            output[date_column] = None
            output[return_column] = None
            continue
        output[date_column] = dates[position]
        output[return_column] = (points[position].close / signal_price - 1) * 100
    return output


def benchmark_year_end_metrics(
    benchmark: list[Any],
    signal_date: str,
    years: tuple[int, ...],
    prefix: str,
) -> dict[str, Any]:
    if not benchmark:
        return {}
    dates = [point.date_text for point in benchmark]
    start = nearest_index(dates, signal_date)
    if start is None:
        return {}
    base = benchmark[start].close
    output: dict[str, Any] = {}
    for year in years:
        date_column = f"{prefix}{year}年末評価日"
        return_column = f"{prefix}{year}年末まで保有リターン"
        if dates[start] > f"{year}-12-31":
            output[date_column] = None
            output[return_column] = None
            continue
        position = last_index_on_or_before(dates, f"{year}-12-31")
        if position is None or position < start or not dates[position].startswith(f"{year}-"):
            output[date_column] = None
            output[return_column] = None
            continue
        output[date_column] = dates[position]
        output[return_column] = (benchmark[position].close / base - 1) * 100
    return output


def forward_metrics(points: list[Any], signal_date: str, signal_price: float) -> dict[str, Any]:
    dates = [point.date_text for point in points]
    position = nearest_index(dates, signal_date)
    if position is None or signal_price <= 0:
        return {}
    output: dict[str, Any] = {}
    for days in (20, 60, 126, 252, 504, 756):
        end = position + days
        if end >= len(points):
            output[f"{days}日後リターン"] = None
            output[f"{days}日以内最大上昇率"] = None
            output[f"{days}日以内最大下落率"] = None
            continue
        future = points[position + 1:end + 1]
        highs = [point.high if point.high is not None else point.close for point in future]
        lows = [point.low if point.low is not None else point.close for point in future]
        output[f"{days}日後リターン"] = (points[end].close / signal_price - 1) * 100
        output[f"{days}日以内最大上昇率"] = (max(highs) / signal_price - 1) * 100
        output[f"{days}日以内最大下落率"] = (min(lows) / signal_price - 1) * 100
    current = points[-1]
    output["現在まで保有リターン"] = (current.close / signal_price - 1) * 100
    try:
        held_days = (pd.Timestamp(current.date_text) - pd.Timestamp(signal_date)).days
        output["現在まで年利換算"] = (
            ((current.close / signal_price) ** (365.25 / held_days) - 1) * 100
            if held_days > 0 else None
        )
    except Exception:
        output["現在まで年利換算"] = None
    return output


def benchmark_metrics(benchmark: list[Any], signal_date: str) -> dict[str, Any]:
    dates = [point.date_text for point in benchmark]
    position = nearest_index(dates, signal_date)
    if position is None:
        return {}
    base = benchmark[position].close
    output: dict[str, Any] = {"ベンチマーク買付日": dates[position]}
    for days in (20, 60, 126, 252, 504, 756):
        end = position + days
        output[f"ベンチマーク{days}日リターン"] = (
            (benchmark[end].close / base - 1) * 100 if end < len(benchmark) else None
        )
    output["ベンチマーク現在リターン"] = (benchmark[-1].close / base - 1) * 100
    return output


def benchmark_symbols(config: AnalysisConfig) -> tuple[str, ...]:
    symbols: list[str] = []
    for raw in (config.benchmark_symbol, *config.comparison_benchmark_symbols):
        symbol = str(raw).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def named_benchmark_metrics(symbol: str, benchmark: list[Any], signal_date: str) -> dict[str, Any]:
    metrics = benchmark_metrics(benchmark, signal_date)
    if not metrics:
        return {}
    output = {f"比較_{symbol}_買付日": metrics.get("ベンチマーク買付日")}
    for days in (20, 60, 126, 252, 504, 756):
        output[f"比較_{symbol}_{days}日リターン"] = metrics.get(f"ベンチマーク{days}日リターン")
    output[f"比較_{symbol}_現在リターン"] = metrics.get("ベンチマーク現在リターン")
    return output


HEADER_ALIASES = {
    "ticker": ("ticker", "symbol", "銘柄", "企業番号", "証券コード"),
    "date": ("date", "effective_date", "基準日", "日付", "時点"),
    "market_cap": ("market_cap", "時価総額", "時価総額円", "底検知時の推定時価総額（円）"),
    "size_group": ("size_group", "時価総額区分", "規模区分", "モデル区分"),
}
CURRENT_MARKET_CAP_ALIASES = (
    "current_market_cap_oku", "現在時価総額(億円)", "現在時価総額（億円）",
)
CURRENT_PRICE_ALIASES = ("current_price", "現在価格", "現在株価")
CURRENT_DATE_ALIASES = ("current_date", "現在日", "時価総額基準日")


def normalize_header(value: Any) -> str:
    return "".join(character for character in str(value).strip().lower() if character not in " _-（）()")


def find_column(columns: list[str], aliases: tuple[str, ...]) -> str:
    mapping = {normalize_header(column): column for column in columns}
    for alias in aliases:
        if normalize_header(alias) in mapping:
            return mapping[normalize_header(alias)]
    return ""


class SizeHistory:
    def __init__(self, path: Path | None, small_max: float, large_min: float) -> None:
        self.small_max = small_max
        self.large_min = large_min
        self.rows: dict[str, list[tuple[pd.Timestamp | None, float | None, str]]] = {}
        self.current_snapshots: dict[str, tuple[float, float | None, pd.Timestamp | None]] = {}
        self.mode = "none"
        if path and path.exists():
            frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
            ticker_col = find_column(list(frame.columns), HEADER_ALIASES["ticker"])
            if not ticker_col:
                raise RuntimeError("企業分類CSVに銘柄列がありません")
            current_cap_col = find_column(list(frame.columns), CURRENT_MARKET_CAP_ALIASES)
            if current_cap_col:
                current_price_col = find_column(list(frame.columns), CURRENT_PRICE_ALIASES)
                current_date_col = find_column(list(frame.columns), CURRENT_DATE_ALIASES)
                self.mode = "current_market_cap_estimate"
                snapshot_keys: dict[str, tuple[pd.Timestamp, int]] = {}
                for _, row in frame.iterrows():
                    ticker = str(row[ticker_col]).strip().upper()
                    cap_oku = pd.to_numeric(str(row[current_cap_col]).replace(",", ""), errors="coerce")
                    if not ticker or pd.isna(cap_oku) or float(cap_oku) <= 0:
                        continue
                    reference_price = (
                        pd.to_numeric(str(row[current_price_col]).replace(",", ""), errors="coerce")
                        if current_price_col else np.nan
                    )
                    as_of = pd.to_datetime(row[current_date_col], errors="coerce") if current_date_col else pd.NaT
                    key = (
                        as_of if pd.notna(as_of) else pd.Timestamp.min,
                        int(pd.notna(reference_price) and float(reference_price) > 0),
                    )
                    if ticker in snapshot_keys and key < snapshot_keys[ticker]:
                        continue
                    snapshot_keys[ticker] = key
                    self.current_snapshots[ticker] = (
                        float(cap_oku) * 100_000_000.0,
                        float(reference_price) if pd.notna(reference_price) and float(reference_price) > 0 else None,
                        as_of if pd.notna(as_of) else None,
                    )
                if not self.current_snapshots:
                    raise RuntimeError("現在時価総額CSVに有効な時価総額がありません")
                return
            date_col = find_column(list(frame.columns), HEADER_ALIASES["date"])
            cap_col = find_column(list(frame.columns), HEADER_ALIASES["market_cap"])
            group_col = find_column(list(frame.columns), HEADER_ALIASES["size_group"])
            if not cap_col and not group_col:
                raise RuntimeError("企業分類CSVに時価総額または規模区分列がありません")
            self.mode = "point_in_time" if date_col else "static_lookahead_risk"
            for _, row in frame.iterrows():
                ticker = str(row[ticker_col]).strip().upper()
                when = pd.to_datetime(row[date_col], errors="coerce") if date_col else None
                cap = pd.to_numeric(str(row[cap_col]).replace(",", ""), errors="coerce") if cap_col else np.nan
                group = str(row[group_col]).strip() if group_col else ""
                self.rows.setdefault(ticker, []).append((when if pd.notna(when) else None, float(cap) if pd.notna(cap) else None, group))
            for ticker in self.rows:
                self.rows[ticker].sort(key=lambda item: item[0] if item[0] is not None else pd.Timestamp.min)

    def _size(self, cap: float | None, group: str = "") -> str:
        normalized = group.lower()
        if normalized in {"small", "小型", "小型株"}:
            return "small"
        if normalized in {"large", "大型", "大型株"}:
            return "large"
        if normalized in {"mid", "medium", "中型", "中型株"}:
            return "mid"
        if cap is None:
            return ""
        return "small" if cap < self.small_max else "large" if cap >= self.large_min else "mid"

    def snapshot_details(self, ticker: str) -> tuple[float | None, float | None, str]:
        snapshot = self.current_snapshots.get(ticker.upper())
        if not snapshot:
            return None, None, ""
        cap, price, as_of = snapshot
        return cap, price, str(as_of.date()) if as_of is not None else ""

    def lookup(
        self,
        ticker: str,
        event_date: str,
        event_price: float | None = None,
        latest_db_price: float | None = None,
    ) -> tuple[str, float | None, str]:
        snapshot = self.current_snapshots.get(ticker.upper())
        if snapshot:
            current_cap, snapshot_price, _as_of = snapshot
            reference_price = snapshot_price or latest_db_price
            if event_price is None or event_price <= 0 or reference_price is None or reference_price <= 0:
                return "", None, "current_market_cap_price_missing"
            estimated_cap = current_cap * event_price / reference_price
            source = (
                "estimated_from_current_market_cap"
                if snapshot_price is not None else "estimated_from_current_market_cap_db_price"
            )
            return self._size(estimated_cap), estimated_cap, source
        candidates = self.rows.get(ticker.upper(), [])
        if not candidates:
            return "", None, "missing"
        event = pd.Timestamp(event_date)
        eligible = [item for item in candidates if item[0] is None or item[0] <= event]
        if not eligible:
            return "", None, "future_row_rejected"
        when, cap, group = eligible[-1]
        size = self._size(cap, group)
        source = "point_in_time" if when is not None else "static_lookahead_risk"
        return size, cap, source


def routed_model(size_group: str) -> str:
    return {"small": SMALL_MODEL.model_id, "mid": MID_MODEL.model_id, "large": LARGE_MODEL.model_id}.get(size_group, "")


def event_rows(
    company: pd.Series,
    points: list[Any],
    benchmarks: dict[str, list[Any]],
    analysis: dict[str, Any],
    preset: ModelPreset,
    size_history: SizeHistory,
    config: AnalysisConfig,
) -> list[dict[str, Any]]:
    signals = {str(item.get("confirmationDate")): item for item in analysis.get("signals", [])}
    year_ends = completed_years(config.start_date)
    rows: list[dict[str, Any]] = []
    for evaluation in analysis.get("bottomEvaluations", []):
        event_date = str(evaluation.get("date") or "")
        if not event_date or event_date < config.start_date or event_date > config.end_date:
            continue
        signal = signals.get(event_date, {})
        event_price = float(evaluation.get("price") or 0)
        latest_db_price = float(points[-1].close) if points else None
        size_group, cap, size_source = size_history.lookup(
            str(company["symbol"]), event_date, event_price, latest_db_price
        )
        current_cap, cap_reference_price, cap_reference_date = size_history.snapshot_details(str(company["symbol"]))
        primary_benchmark = benchmarks.get(config.benchmark_symbol.upper(), [])
        row = {
            "モデルID": preset.model_id,
            "モデル名": preset.display_name,
            "銘柄": company["symbol"],
            "企業名": company.get("name", ""),
            "取引所": company.get("exchange", ""),
            "シグナル日": event_date,
            "シグナル価格": evaluation.get("price"),
            "トリガー日": signal.get("triggerDate"),
            "トリガー種別": signal.get("triggerType"),
            "下落率": signal.get("drawdownPercent"),
            "直近安値下落率": signal.get("recentLowDrawdownPercent"),
            "ピーク経過営業日": signal.get("peakAgeTradingDays"),
            "60日レンジ": signal.get("range60"),
            "20日ボラ": signal.get("volatility20"),
            "出来高倍率": signal.get("volumeRatio"),
            "日次騰落率": signal.get("dailyReturn"),
            "終値強度": signal.get("closeStrength"),
            "候補後最安値下落率": evaluation.get("drawdownAfterPercent"),
            "候補後判定": evaluation.get("verdict"),
            "実底まで営業日": evaluation.get("tradingDaysToMinAfter"),
            "時価総額円": cap,
            "時価総額区分": size_group,
            "時価総額情報種別": size_source,
            "現在時価総額円": current_cap,
            "時価総額基準日": cap_reference_date,
            "時価総額基準価格": cap_reference_price,
            "時価総額推定倍率": (
                cap / current_cap if cap is not None and current_cap is not None and current_cap > 0 else None
            ),
            "自動振分モデルID": routed_model(size_group),
            "自動振分一致": preset.model_id == routed_model(size_group) if size_group else None,
            "アルゴリズムver": "5.3-compatible-fast-path",
            **forward_metrics(points, event_date, float(evaluation.get("price") or 0)),
            **stock_year_end_metrics(points, event_date, event_price, year_ends),
            **benchmark_metrics(primary_benchmark, event_date),
            **benchmark_year_end_metrics(primary_benchmark, event_date, year_ends, "ベンチマーク"),
        }
        for symbol, benchmark in benchmarks.items():
            row.update(named_benchmark_metrics(symbol, benchmark, event_date))
            row.update(benchmark_year_end_metrics(
                benchmark, event_date, year_ends, f"比較_{symbol}_"
            ))
            benchmark_current = row.get(f"比較_{symbol}_現在リターン")
            own_current = row.get("現在まで保有リターン")
            row[f"比較_{symbol}_現在まで超過"] = (
                own_current - benchmark_current
                if own_current is not None and benchmark_current is not None else None
            )
            for year in year_ends:
                own_year = row.get(f"{year}年末まで保有リターン")
                benchmark_year = row.get(f"比較_{symbol}_{year}年末まで保有リターン")
                row[f"比較_{symbol}_{year}年末まで超過"] = (
                    own_year - benchmark_year
                    if own_year is not None and benchmark_year is not None else None
                )
        for year in year_ends:
            own_year = row.get(f"{year}年末まで保有リターン")
            benchmark_year = row.get(f"ベンチマーク{year}年末まで保有リターン")
            row[f"{year}年末ベンチマーク超過"] = (
                own_year - benchmark_year
                if own_year is not None and benchmark_year is not None else None
            )
        for days in (20, 60, 126, 252, 504, 756):
            own = row.get(f"{days}日後リターン")
            bench = row.get(f"ベンチマーク{days}日リターン")
            row[f"{days}日ベンチマーク超過"] = own - bench if own is not None and bench is not None else None
        row["126日偽底フラグ"] = (
            float(row["126日以内最大下落率"]) <= -15
            if row.get("126日以内最大下落率") is not None else None
        )
        row["756日大化けフラグ"] = (
            float(row["756日後リターン"]) >= 100
            if row.get("756日後リターン") is not None else None
        )
        rows.append(row)
    return rows


def numeric_summary(group: pd.DataFrame, period: str, model_id: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "期間": period, "モデルID": model_id, "イベント数": len(group),
        "銘柄数": group["銘柄"].nunique() if not group.empty else 0,
    }
    for days in (60, 126, 252, 504, 756):
        column = f"{days}日後リターン"
        values = pd.to_numeric(group.get(column, pd.Series(dtype=float)), errors="coerce")
        row[f"{days}日_件数"] = int(values.notna().sum())
        row[f"{days}日_平均"] = values.mean()
        row[f"{days}日_中央値"] = values.median()
        row[f"{days}日_勝率"] = (values > 0).mean() if values.notna().any() else np.nan
        excess = pd.to_numeric(group.get(f"{days}日ベンチマーク超過", pd.Series(dtype=float)), errors="coerce")
        row[f"{days}日_超過平均"] = excess.mean()
        row[f"{days}日_超過中央値"] = excess.median()
    false_signal = group.get("126日偽底フラグ", pd.Series(dtype=bool)).dropna()
    huge_winner = group.get("756日大化けフラグ", pd.Series(dtype=bool)).dropna()
    row["126日偽底率"] = false_signal.astype(bool).mean() if len(false_signal) else np.nan
    row["756日大化け率"] = huge_winner.astype(bool).mean() if len(huge_winner) else np.nan
    current = pd.to_numeric(group.get("現在まで保有リターン", pd.Series(dtype=float)), errors="coerce")
    row["現在保有平均"] = current.mean()
    row["現在保有中央値"] = current.median()
    benchmark_current = pd.to_numeric(group.get("ベンチマーク現在リターン", pd.Series(dtype=float)), errors="coerce")
    current_excess = current - benchmark_current
    row["現在ベンチマーク平均"] = benchmark_current.mean()
    row["現在ベンチマーク中央値"] = benchmark_current.median()
    row["現在ベンチマーク超過平均"] = current_excess.mean()
    row["現在ベンチマーク超過中央値"] = current_excess.median()
    return row


def build_benchmark_equal_weight_summary(
    events: pd.DataFrame,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame()
    for model_id, group in events.groupby("モデルID"):
        own = pd.to_numeric(group.get("現在まで保有リターン"), errors="coerce")
        row: dict[str, Any] = {
            "モデルID": model_id,
            "モデル名": group["モデル名"].iloc[0] if "モデル名" in group else "",
            "イベント数": len(group),
            "個別株現在リターン件数": int(own.notna().sum()),
            "個別株現在リターン平均": own.mean(),
            "個別株現在リターン中央値": own.median(),
        }
        for symbol in symbols:
            benchmark = pd.to_numeric(group.get(f"比較_{symbol}_現在リターン"), errors="coerce")
            excess = own - benchmark
            row[f"{symbol}現在リターン件数"] = int(benchmark.notna().sum())
            row[f"{symbol}同額購入平均"] = benchmark.mean()
            row[f"{symbol}同額購入中央値"] = benchmark.median()
            row[f"個別株-{symbol}平均差"] = excess.mean()
            row[f"個別株-{symbol}中央値差"] = excess.median()
            row[f"個別株が{symbol}超過した割合"] = (excess > 0).mean() if excess.notna().any() else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def build_year_end_holding_outputs(
    events: pd.DataFrame,
    years: tuple[int, ...],
    symbols: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail_rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()
    base_columns = [
        "モデルID", "モデル名", "銘柄", "企業名", "シグナル日", "シグナル価格", "時価総額区分",
    ]
    for _, event in events.iterrows():
        for year in years:
            own = pd.to_numeric(pd.Series([event.get(f"{year}年末まで保有リターン")]), errors="coerce").iloc[0]
            if pd.isna(own):
                continue
            row = {column: event.get(column) for column in base_columns}
            row.update({
                "評価年": year,
                "評価日": event.get(f"{year}年末評価日"),
                "保有リターン": own,
            })
            for symbol in symbols:
                row[f"{symbol}評価日"] = event.get(f"比較_{symbol}_{year}年末評価日")
                row[f"{symbol}リターン"] = event.get(f"比較_{symbol}_{year}年末まで保有リターン")
                row[f"{symbol}超過リターン"] = event.get(f"比較_{symbol}_{year}年末まで超過")
            detail_rows.append(row)
    detail = pd.DataFrame(detail_rows)
    summary_rows: list[dict[str, Any]] = []
    if detail.empty:
        return detail, pd.DataFrame()
    for (year, model_id), group in detail.groupby(["評価年", "モデルID"], sort=True):
        own = pd.to_numeric(group["保有リターン"], errors="coerce")
        row: dict[str, Any] = {
            "評価年": int(year),
            "モデルID": model_id,
            "モデル名": group["モデル名"].iloc[0],
            "イベント数": len(group),
            "銘柄数": group["銘柄"].nunique(),
            "保有リターン平均": own.mean(),
            "保有リターン中央値": own.median(),
            "プラス割合": (own > 0).mean(),
        }
        for symbol in symbols:
            benchmark = pd.to_numeric(group.get(f"{symbol}リターン"), errors="coerce")
            excess = pd.to_numeric(group.get(f"{symbol}超過リターン"), errors="coerce")
            row[f"{symbol}リターン平均"] = benchmark.mean()
            row[f"{symbol}リターン中央値"] = benchmark.median()
            row[f"{symbol}超過平均"] = excess.mean()
            row[f"{symbol}超過中央値"] = excess.median()
            row[f"{symbol}超過割合"] = (excess > 0).mean() if excess.notna().any() else np.nan
        summary_rows.append(row)
    return detail, pd.DataFrame(summary_rows)


def write_year_end_holding_outputs(
    events: pd.DataFrame,
    output_dir: Path,
    years: tuple[int, ...],
    symbols: tuple[str, ...],
) -> dict[str, Path]:
    detail, summary = build_year_end_holding_outputs(events, years, symbols)
    paths = {
        "year_end_holding_returns": output_dir / "year_end_holding_returns.csv",
        "year_end_holding_summary": output_dir / "year_end_holding_summary.csv",
    }
    atomic_csv_write(detail, paths["year_end_holding_returns"])
    atomic_csv_write(summary, paths["year_end_holding_summary"])
    return paths


def enrich_existing_year_end_returns(
    legacy_app_path: Path,
    price_db_path: Path,
    output_dir: Path,
    benchmark_symbol: str = "ACWI",
    comparison_benchmark_symbols: tuple[str, ...] = ("ACWI", "SPY"),
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    events_path = output_dir / "model_events.csv"
    if not events_path.exists():
        raise FileNotFoundError(f"先に比較分析を実行してください: {events_path}")
    validate_database(price_db_path)
    module = load_legacy_module(legacy_app_path)
    source = pd.read_csv(events_path, encoding="utf-8-sig", low_memory=False)
    if source.empty:
        raise ValueError("model_events.csvにイベントがありません")
    signal_years = pd.to_datetime(source["シグナル日"], errors="coerce").dt.year.dropna()
    start_year = int(signal_years.min()) if not signal_years.empty else 2012
    years = tuple(range(start_year, datetime.now().year))
    symbols = tuple(dict.fromkeys(
        symbol.strip().upper()
        for symbol in (benchmark_symbol, *comparison_benchmark_symbols)
        if symbol.strip()
    ))
    primary = benchmark_symbol.strip().upper()
    records: list[dict[str, Any]] = []
    tickers = source["銘柄"].astype(str).drop_duplicates().tolist()
    with sqlite3.connect(price_db_path) as connection:
        benchmarks = {symbol: load_points(connection, symbol, module) for symbol in symbols}
        for current, ticker in enumerate(tickers, start=1):
            points = load_points(connection, ticker, module)
            part = source.loc[source["銘柄"].astype(str) == ticker]
            for _, raw in part.iterrows():
                row = raw.to_dict()
                signal_date = str(row.get("シグナル日") or "")
                signal_price = float(pd.to_numeric(pd.Series([row.get("シグナル価格")]), errors="coerce").iloc[0])
                row.update(stock_year_end_metrics(points, signal_date, signal_price, years))
                primary_points = benchmarks.get(primary, [])
                row.update(benchmark_year_end_metrics(primary_points, signal_date, years, "ベンチマーク"))
                for symbol, benchmark in benchmarks.items():
                    row.update(benchmark_year_end_metrics(benchmark, signal_date, years, f"比較_{symbol}_"))
                    for year in years:
                        own = row.get(f"{year}年末まで保有リターン")
                        bench = row.get(f"比較_{symbol}_{year}年末まで保有リターン")
                        row[f"比較_{symbol}_{year}年末まで超過"] = (
                            own - bench if own is not None and bench is not None else None
                        )
                for year in years:
                    own = row.get(f"{year}年末まで保有リターン")
                    bench = row.get(f"ベンチマーク{year}年末まで保有リターン")
                    row[f"{year}年末ベンチマーク超過"] = (
                        own - bench if own is not None and bench is not None else None
                    )
                records.append(row)
            emit(progress, phase="year_end", current=current, total=len(tickers), symbol=ticker)
    enriched = pd.DataFrame(records)
    enriched_path = output_dir / "model_events_with_year_end_returns.csv"
    atomic_csv_write(enriched, enriched_path)
    paths = write_year_end_holding_outputs(enriched, output_dir, years, symbols)
    return {
        "app_version": APP_VERSION,
        "event_count": len(enriched),
        "ticker_count": len(tickers),
        "start_year": years[0] if years else None,
        "end_year": years[-1] if years else None,
        "enriched_events": str(enriched_path),
        **{key: str(value) for key, value in paths.items()},
    }


def build_market_cap_summaries(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if events.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    classified = events[
        events.get("時価総額区分", pd.Series("", index=events.index))
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    ].copy()
    band_rows: list[dict[str, Any]] = []
    for size_group, size_frame in classified.groupby("時価総額区分"):
        for model_id, model_frame in size_frame.groupby("モデルID"):
            band_rows.append({
                "時価総額区分": size_group,
                **numeric_summary(model_frame, "全期間", model_id),
            })

    routed = classified.loc[
        classified.get("自動振分一致", pd.Series(False, index=classified.index)).fillna(False).astype(bool)
    ]
    routed_rows: list[dict[str, Any]] = []
    for model_id, group in routed.groupby("モデルID"):
        routed_rows.append(numeric_summary(group, "自動振分", model_id))

    source = events.get("時価総額情報種別", pd.Series("missing", index=events.index)).fillna("missing")
    coverage_rows: list[dict[str, Any]] = []
    for source_name, group in events.assign(_source=source).groupby("_source", dropna=False):
        coverage_rows.append({
            "時価総額情報種別": source_name,
            "イベント数": len(group),
            "イベント割合": len(group) / len(events),
            "銘柄数": group["銘柄"].nunique(),
        })
    for size_group, group in classified.groupby("時価総額区分"):
        coverage_rows.append({
            "時価総額情報種別": f"band:{size_group}",
            "イベント数": len(group),
            "イベント割合": len(group) / len(events),
            "銘柄数": group["銘柄"].nunique(),
        })
    return pd.DataFrame(band_rows), pd.DataFrame(routed_rows), pd.DataFrame(coverage_rows)


def build_threshold_set_simulations(
    events: pd.DataFrame,
    threshold_sets: tuple[RoutingThresholdSet, ...],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if events.empty or not threshold_sets:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    caps = pd.to_numeric(events.get("時価総額円"), errors="coerce")
    detail_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []

    for order, threshold_set in enumerate(threshold_sets, start=1):
        bands = pd.Series("", index=events.index, dtype="object")
        valid = caps.notna() & caps.gt(0)
        bands.loc[valid & caps.lt(threshold_set.small_max_jpy)] = "small"
        bands.loc[valid & caps.ge(threshold_set.large_min_jpy)] = "large"
        bands.loc[valid & bands.eq("")] = "mid"
        expected_models = bands.map(routed_model)
        selected = events.loc[expected_models.ne("") & events["モデルID"].eq(expected_models)].copy()
        selected.insert(0, "境界セット順", order)
        selected.insert(1, "境界セット名", threshold_set.name)
        selected.insert(2, "小型上限億円", threshold_set.small_max_jpy / 100_000_000.0)
        selected.insert(3, "大型下限億円", threshold_set.large_min_jpy / 100_000_000.0)
        selected.insert(4, "セット時価総額区分", bands.loc[selected.index].values)
        detail_frames.append(selected)

        summary = numeric_summary(selected, "境界セット", "AUTO-ROUTED")
        model_counts = selected["モデルID"].value_counts()
        summary_rows.append({
            "境界セット順": order,
            "境界セット名": threshold_set.name,
            "小型上限億円": threshold_set.small_max_jpy / 100_000_000.0,
            "大型下限億円": threshold_set.large_min_jpy / 100_000_000.0,
            "時価総額推定可能行数": int(valid.sum()),
            "小型モデル採用数": int(model_counts.get(SMALL_MODEL.model_id, 0)),
            "中型モデル採用数": int(model_counts.get(MID_MODEL.model_id, 0)),
            "大型モデル採用数": int(model_counts.get(LARGE_MODEL.model_id, 0)),
            **summary,
        })
        for model_id, model_frame in selected.groupby("モデルID"):
            model_rows.append({
                "境界セット順": order,
                "境界セット名": threshold_set.name,
                "小型上限億円": threshold_set.small_max_jpy / 100_000_000.0,
                "大型下限億円": threshold_set.large_min_jpy / 100_000_000.0,
                **numeric_summary(model_frame, "境界セット内訳", model_id),
            })

    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    return detail, pd.DataFrame(summary_rows), pd.DataFrame(model_rows)


def write_threshold_set_simulations(
    events: pd.DataFrame,
    output_dir: Path,
    threshold_sets: tuple[RoutingThresholdSet, ...],
) -> dict[str, Path]:
    detail, summary, model_summary = build_threshold_set_simulations(events, threshold_sets)
    paths = {
        "threshold_set_routed_events": output_dir / "threshold_set_routed_events.csv",
        "threshold_set_summary": output_dir / "threshold_set_summary.csv",
        "threshold_set_model_summary": output_dir / "threshold_set_model_summary.csv",
    }
    atomic_csv_write(detail, paths["threshold_set_routed_events"])
    atomic_csv_write(summary, paths["threshold_set_summary"])
    atomic_csv_write(model_summary, paths["threshold_set_model_summary"])
    return paths


def rerun_threshold_set_simulations(
    output_dir: Path,
    threshold_sets: tuple[RoutingThresholdSet, ...],
) -> dict[str, Path]:
    events_path = output_dir / "model_events.csv"
    if not events_path.exists():
        raise FileNotFoundError(f"先に比較分析を実行してください: {events_path}")
    events = pd.read_csv(events_path, encoding="utf-8-sig", low_memory=False)
    cap_count = (
        int(pd.to_numeric(events["時価総額円"], errors="coerce").notna().sum())
        if "時価総額円" in events.columns else 0
    )
    enriched_path = output_dir / "model_events_with_market_cap_estimate.csv"
    if cap_count == 0 and enriched_path.exists():
        enriched = pd.read_csv(enriched_path, encoding="utf-8-sig", low_memory=False)
        keys = ["モデルID", "銘柄", "シグナル日"]
        same_events = len(enriched) == len(events) and all(
            key in enriched.columns and key in events.columns
            and enriched[key].astype(str).reset_index(drop=True).equals(events[key].astype(str).reset_index(drop=True))
            for key in keys
        )
        if same_events and "時価総額円" in enriched.columns:
            events = enriched
            cap_count = int(pd.to_numeric(events["時価総額円"], errors="coerce").notna().sum())
    if cap_count == 0:
        raise ValueError("イベントCSVに推定時価総額がありません。v0.4.0で比較分析を実行してください")
    return write_threshold_set_simulations(events, output_dir, threshold_sets)


def build_summaries(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if events.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    events = events.copy()
    events["シグナル日"] = pd.to_datetime(events["シグナル日"], errors="coerce")
    events["シグナル年"] = events["シグナル日"].dt.year
    rows: list[dict[str, Any]] = []
    for model_id, group in events.groupby("モデルID"):
        rows.append(numeric_summary(group, "全期間", model_id))
        for year, year_group in group.groupby("シグナル年"):
            rows.append(numeric_summary(year_group, str(int(year)), model_id))
        for label, mask in (
            ("2012-2020", group["シグナル年"].between(2012, 2020)),
            ("2021-2022", group["シグナル年"].between(2021, 2022)),
            ("2023-2024", group["シグナル年"].between(2023, 2024)),
        ):
            rows.append(numeric_summary(group.loc[mask], label, model_id))

    top_rows: list[dict[str, Any]] = []
    for model_id, group in events.groupby("モデルID"):
        ordered = group.assign(
            _current_numeric=pd.to_numeric(group["現在まで保有リターン"], errors="coerce")
        ).sort_values("_current_numeric", ascending=False)
        for removed in (0, 1, 3, 5, 10):
            trimmed = ordered.iloc[removed:]
            summary = numeric_summary(trimmed, "全期間", model_id)
            top_rows.append({"上位除外数": removed, **summary})

    membership = (
        events.assign(値=1)
        .pivot_table(index=["銘柄", "シグナル日"], columns="モデルID", values="値", aggfunc="max", fill_value=0)
        .reset_index()
    )
    model_columns = [column for column in membership.columns if column not in {"銘柄", "シグナル日"}]
    membership["検出モデル数"] = membership[model_columns].sum(axis=1)
    membership["検出モデル"] = membership[model_columns].apply(
        lambda row: ",".join(column for column in model_columns if row[column] == 1), axis=1
    )
    return pd.DataFrame(rows), pd.DataFrame(top_rows), membership


def config_fingerprint(config: AnalysisConfig) -> dict[str, Any]:
    return {
        "app_version": APP_VERSION,
        "legacy_app_sha256": file_sha256(config.legacy_app_path),
        "price_db": str(config.price_db_path.resolve()),
        "start_date": config.start_date,
        "end_date": config.end_date,
        "currency": config.currency,
        "benchmark_symbol": config.benchmark_symbol,
        "comparison_benchmark_symbols": list(benchmark_symbols(config)),
        "company_limit": config.company_limit,
        "size_history_csv": str(config.size_history_csv.resolve()) if config.size_history_csv else "",
        "small_max_jpy": config.small_max_jpy,
        "large_min_jpy": config.large_min_jpy,
        "routing_threshold_sets": [asdict(item) for item in config.routing_threshold_sets],
        "run_all_models": config.run_all_models,
        "models": [asdict(model) for model in config.models],
    }


def run_analysis(
    config: AnalysisConfig,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    stop_event = stop_event or threading.Event()
    started = time.perf_counter()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if len(config.routing_threshold_sets) > 1 and not config.run_all_models:
        raise ValueError("複数の時価総額境界セットを比較する場合は、3モデル横並び比較を有効にしてください")
    validate_database(config.price_db_path)
    module = load_legacy_module(config.legacy_app_path)
    fingerprint = config_fingerprint(config)
    state_path = config.output_dir / "analysis_state.json"
    checkpoint_path = config.output_dir / "events_checkpoint.csv"
    processed: set[str] = set()
    event_records: list[dict[str, Any]] = []

    if config.resume and state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("config") != fingerprint:
            raise RuntimeError("前回と設定が異なるため再開できません。通常の開始を使用してください")
        processed = set(map(str, state.get("processed_symbols", [])))
        if checkpoint_path.exists() and checkpoint_path.stat().st_size > 5:
            event_records = pd.read_csv(checkpoint_path, encoding="utf-8-sig", low_memory=False).to_dict("records")

    companies = available_companies(config)
    if companies.empty:
        raise RuntimeError("解析対象企業がありません")
    size_history = SizeHistory(config.size_history_csv, config.small_max_jpy, config.large_min_jpy)
    with sqlite3.connect(config.price_db_path) as connection:
        benchmarks = {
            symbol: load_points(connection, symbol, module)
            for symbol in benchmark_symbols(config)
        }
        for symbol, points in benchmarks.items():
            if not points:
                emit(progress, phase="warning", message=f"ベンチマーク{symbol}がDBにないため比較列は空欄になります")
        total = len(companies)
        for position, (_, company) in enumerate(companies.iterrows(), start=1):
            symbol = str(company["symbol"])
            if symbol in processed:
                emit(progress, phase="analyze", current=position, total=total, symbol=symbol, events=len(event_records), resumed=len(processed))
                continue
            if stop_event.is_set():
                break
            try:
                points = load_points(connection, symbol, module)
                for preset in config.models:
                    signal_config = preset_to_signal_config(module, preset)
                    analysis = fast_analyze_stability(points, module, signal_config)
                    records = event_rows(company, points, benchmarks, analysis, preset, size_history, config)
                    if config.run_all_models:
                        event_records.extend(records)
                    else:
                        event_records.extend(row for row in records if row.get("自動振分一致") is True)
            except Exception as exc:
                emit(progress, phase="warning", message=f"{symbol}: {exc}")
            processed.add(symbol)
            if position % 10 == 0 or position == total:
                atomic_csv_write(pd.DataFrame(event_records), checkpoint_path)
                atomic_json_write(state_path, {
                    "status": "running", "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "config": fingerprint, "processed_symbols": sorted(processed), "events": len(event_records),
                })
            elapsed = max(time.perf_counter() - started, 0.001)
            rate = position / elapsed
            emit(
                progress, phase="analyze", current=position, total=total, symbol=symbol,
                events=len(event_records), resumed=0, eta_seconds=(total - position) / rate if rate else None,
            )

    events = pd.DataFrame(event_records)
    events_path = config.output_dir / "model_events.csv"
    year_end_events_path = config.output_dir / "model_events_with_year_end_returns.csv"
    atomic_csv_write(events, events_path)
    atomic_csv_write(events, year_end_events_path)
    summary, top_exclusion, overlap = build_summaries(events)
    summary_path = config.output_dir / "model_summary.csv"
    top_path = config.output_dir / "top_winner_exclusion.csv"
    overlap_path = config.output_dir / "model_signal_overlap.csv"
    atomic_csv_write(summary, summary_path)
    atomic_csv_write(top_exclusion, top_path)
    atomic_csv_write(overlap, overlap_path)
    benchmark_summary = build_benchmark_equal_weight_summary(events, benchmark_symbols(config))
    benchmark_summary_path = config.output_dir / "benchmark_equal_weight_summary.csv"
    atomic_csv_write(benchmark_summary, benchmark_summary_path)
    year_end_paths = write_year_end_holding_outputs(
        events, config.output_dir, completed_years(config.start_date), benchmark_symbols(config)
    )
    band_summary, routed_summary, cap_coverage = build_market_cap_summaries(events)
    band_summary_path = config.output_dir / "market_cap_band_model_summary.csv"
    routed_summary_path = config.output_dir / "routed_model_summary.csv"
    cap_coverage_path = config.output_dir / "market_cap_coverage_summary.csv"
    atomic_csv_write(band_summary, band_summary_path)
    atomic_csv_write(routed_summary, routed_summary_path)
    atomic_csv_write(cap_coverage, cap_coverage_path)
    threshold_paths = write_threshold_set_simulations(events, config.output_dir, config.routing_threshold_sets)
    threshold_events_path = threshold_paths["threshold_set_routed_events"]
    threshold_summary_path = threshold_paths["threshold_set_summary"]
    threshold_model_summary_path = threshold_paths["threshold_set_model_summary"]
    settings_path = config.output_dir / "model_settings.json"
    atomic_json_write(settings_path, fingerprint)
    report = {
        "app_version": APP_VERSION,
        "legacy_algorithm_version": getattr(module, "ALGORITHM_VERSION", ""),
        "stopped": stop_event.is_set(),
        "company_count": len(companies),
        "processed_company_count": len(processed),
        "event_count": len(events),
        "size_history_mode": size_history.mode,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "outputs": {
            "events": str(events_path), "summary": str(summary_path),
            "events_with_year_end_returns": str(year_end_events_path),
            "top_winner_exclusion": str(top_path), "overlap": str(overlap_path),
            "benchmark_equal_weight_summary": str(benchmark_summary_path),
            "year_end_holding_returns": str(year_end_paths["year_end_holding_returns"]),
            "year_end_holding_summary": str(year_end_paths["year_end_holding_summary"]),
            "market_cap_band_model_summary": str(band_summary_path),
            "routed_model_summary": str(routed_summary_path),
            "market_cap_coverage_summary": str(cap_coverage_path),
            "threshold_set_routed_events": str(threshold_events_path),
            "threshold_set_summary": str(threshold_summary_path),
            "threshold_set_model_summary": str(threshold_model_summary_path),
            "settings": str(settings_path),
        },
    }
    atomic_json_write(config.output_dir / "analysis_report.json", report)
    atomic_json_write(state_path, {
        "status": "stopped" if stop_event.is_set() else "complete",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "config": fingerprint, "processed_symbols": sorted(processed), "events": len(events),
    })
    emit(progress, phase="done", **report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="時価総額帯別・固定閾値底検知モデル比較")
    parser.add_argument("--legacy-app", type=Path, default=DEFAULT_LEGACY_APP)
    parser.add_argument("--price-db", type=Path, default=DEFAULT_PRICE_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-date", default="2012-01-01")
    parser.add_argument("--end-date", default="2024-12-31")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--benchmark", default="ACWI")
    parser.add_argument("--comparison-benchmarks", default="ACWI,SPY")
    parser.add_argument(
        "--routing-sets",
        default="標準:700:5000;中型1100-3300:1100:3300;中型広め:500:7000",
        help="セット名:小型上限億円:大型下限億円 をセミコロン区切りで指定",
    )
    parser.add_argument(
        "--size-history", type=Path,
        default=DEFAULT_MARKET_CAP_CSV if DEFAULT_MARKET_CAP_CSV.exists() else None,
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    routing_sets = parse_routing_threshold_sets(args.routing_sets)
    primary_set = routing_sets[0]
    config = AnalysisConfig(
        args.legacy_app, args.price_db, args.output_dir,
        start_date=args.start_date, end_date=args.end_date,
        company_limit=args.limit, resume=args.resume,
        benchmark_symbol=args.benchmark,
        size_history_csv=args.size_history,
        small_max_jpy=primary_set.small_max_jpy,
        large_min_jpy=primary_set.large_min_jpy,
        routing_threshold_sets=routing_sets,
        comparison_benchmark_symbols=tuple(
            symbol.strip().upper() for symbol in args.comparison_benchmarks.split(",") if symbol.strip()
        ),
    )
    report = run_analysis(config, lambda payload: print(json.dumps(payload, ensure_ascii=False), flush=True))
    return 2 if report.get("stopped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
