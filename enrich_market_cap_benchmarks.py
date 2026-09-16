from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from market_cap_model_analysis import (
    DEFAULT_LEGACY_APP,
    DEFAULT_PRICE_DB,
    atomic_csv_write,
    build_benchmark_equal_weight_summary,
    load_legacy_module,
    load_points,
    named_benchmark_metrics,
)


def enrich(
    output_dir: Path,
    price_db: Path,
    legacy_app: Path,
    symbols: tuple[str, ...],
) -> tuple[Path, Path]:
    source = output_dir / "model_events.csv"
    if not source.exists():
        raise FileNotFoundError(f"model_events.csvが見つかりません: {source}")
    events = pd.read_csv(source, encoding="utf-8-sig", low_memory=False)
    module = load_legacy_module(legacy_app)
    with sqlite3.connect(price_db) as connection:
        benchmarks = {symbol: load_points(connection, symbol, module) for symbol in symbols}
    missing = [symbol for symbol, points in benchmarks.items() if not points]
    if missing:
        raise RuntimeError("株価DBにない比較対象: " + ", ".join(missing))

    for symbol, points in benchmarks.items():
        metrics = events["シグナル日"].map(lambda value: named_benchmark_metrics(symbol, points, str(value)))
        metric_frame = pd.DataFrame(metrics.tolist(), index=events.index)
        for column in metric_frame.columns:
            events[column] = metric_frame[column]
        own = pd.to_numeric(events["現在まで保有リターン"], errors="coerce")
        benchmark = pd.to_numeric(events[f"比較_{symbol}_現在リターン"], errors="coerce")
        events[f"比較_{symbol}_現在まで超過"] = own - benchmark

    detailed_path = output_dir / "model_events_with_benchmarks.csv"
    summary_path = output_dir / "benchmark_equal_weight_summary.csv"
    atomic_csv_write(events, detailed_path)
    atomic_csv_write(build_benchmark_equal_weight_summary(events, symbols), summary_path)
    return detailed_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description="既存の時価総額帯別モデル出力へベンチマーク比較を追加")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/market_cap_model_analysis"))
    parser.add_argument("--price-db", type=Path, default=DEFAULT_PRICE_DB)
    parser.add_argument("--legacy-app", type=Path, default=DEFAULT_LEGACY_APP)
    parser.add_argument("--symbols", default="ACWI,SPY")
    args = parser.parse_args()
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
    detailed, summary = enrich(args.output_dir, args.price_db, args.legacy_app, symbols)
    print(f"detail: {detailed.resolve()}")
    print(f"summary: {summary.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
