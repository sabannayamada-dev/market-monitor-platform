from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from market_cap_model_analysis import (
    DEFAULT_MARKET_CAP_CSV,
    DEFAULT_OUTPUT_DIR,
    SizeHistory,
    atomic_csv_write,
    build_market_cap_summaries,
    routed_model,
)


def enrich(
    output_dir: Path,
    market_cap_csv: Path,
    small_max_jpy: float,
    large_min_jpy: float,
) -> dict[str, Path]:
    source = output_dir / "model_events.csv"
    if not source.exists():
        raise FileNotFoundError(f"model_events.csvが見つかりません: {source}")
    events = pd.read_csv(source, encoding="utf-8-sig", low_memory=False)
    history = SizeHistory(market_cap_csv, small_max_jpy, large_min_jpy)

    calculated: list[tuple[str, float | None, str, float | None, str, float | None, float | None]] = []
    for ticker, event_date, event_price in events[["銘柄", "シグナル日", "シグナル価格"]].itertuples(index=False):
        price = pd.to_numeric(event_price, errors="coerce")
        size, estimated_cap, source_name = history.lookup(
            str(ticker), str(event_date), float(price) if pd.notna(price) else None, None
        )
        current_cap, reference_price, reference_date = history.snapshot_details(str(ticker))
        ratio = (
            estimated_cap / current_cap
            if estimated_cap is not None and current_cap is not None and current_cap > 0 else None
        )
        calculated.append((size, estimated_cap, source_name, current_cap, reference_date, reference_price, ratio))

    result = pd.DataFrame(calculated, columns=[
        "時価総額区分", "時価総額円", "時価総額情報種別", "現在時価総額円",
        "時価総額基準日", "時価総額基準価格", "時価総額推定倍率",
    ])
    for column in result.columns:
        events[column] = result[column]
    events["自動振分モデルID"] = events["時価総額区分"].fillna("").map(routed_model)
    events["自動振分一致"] = events["モデルID"] == events["自動振分モデルID"]

    detail_path = output_dir / "model_events_with_market_cap_estimate.csv"
    band_path = output_dir / "market_cap_band_model_summary.csv"
    routed_path = output_dir / "routed_model_summary.csv"
    coverage_path = output_dir / "market_cap_coverage_summary.csv"
    atomic_csv_write(events, detail_path)
    band, routed, coverage = build_market_cap_summaries(events)
    atomic_csv_write(band, band_path)
    atomic_csv_write(routed, routed_path)
    atomic_csv_write(coverage, coverage_path)
    return {
        "detail": detail_path,
        "band_summary": band_path,
        "routed_summary": routed_path,
        "coverage": coverage_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="既存モデルイベントへ現在時価総額からの推定値を追加")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--market-cap-csv", type=Path, default=DEFAULT_MARKET_CAP_CSV)
    parser.add_argument("--small-max-oku", type=float, default=700.0)
    parser.add_argument("--large-min-oku", type=float, default=5000.0)
    args = parser.parse_args()
    outputs = enrich(
        args.output_dir,
        args.market_cap_csv,
        args.small_max_oku * 100_000_000.0,
        args.large_min_oku * 100_000_000.0,
    )
    for name, path in outputs.items():
        print(f"{name}: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
