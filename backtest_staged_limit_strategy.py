from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs" / "us_stock_research"
PREDICTIONS = OUTPUT_DIR / "median_peak_to_low_forecast_predictions.csv"
EVENTS = OUTPUT_DIR / "us_bottom_events_with_sec.csv"
BENCHMARK = OUTPUT_DIR / "sp500_fred.csv"

STRATEGIES = {
    "immediate_100": {"immediate": 1.00},
    "immediate_P80_P70_P50_equal": {"immediate": 0.25, 80: 0.25, 70: 0.25, 50: 0.25},
    "equal_33_33_33": {70: 1 / 3, 50: 1 / 3, 35: 1 / 3},
    "catch_50_30_20": {70: 0.50, 50: 0.30, 35: 0.20},
    "price_25_35_40": {70: 0.25, 50: 0.35, 35: 0.40},
}


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def load_sp500() -> pd.DataFrame | None:
    if not BENCHMARK.exists() or BENCHMARK.stat().st_size == 0:
        return None
    frame = pd.read_csv(BENCHMARK)
    date_column = next((c for c in frame if c.upper() in {"DATE", "OBSERVATION_DATE"}), None)
    value_column = next((c for c in frame if c.upper() in {"SP500", "CLOSE", "VALUE"}), None)
    if not date_column or not value_column:
        return None
    result = pd.DataFrame(
        {"date": pd.to_datetime(frame[date_column], errors="coerce"), "value": numeric(frame[value_column])}
    ).dropna()
    return result.sort_values("date").drop_duplicates("date", keep="last")


def benchmark_returns(signal_dates: pd.Series, benchmark: pd.DataFrame | None) -> np.ndarray:
    if benchmark is None or benchmark.empty:
        return np.full(len(signal_dates), np.nan)
    dates = pd.DataFrame({"signal_date": pd.to_datetime(signal_dates), "row_order": np.arange(len(signal_dates))})
    starts = pd.merge_asof(
        dates.sort_values("signal_date"), benchmark, left_on="signal_date", right_on="date", direction="forward"
    ).rename(columns={"value": "start_value"})
    starts["end_target"] = starts["signal_date"] + pd.DateOffset(years=1)
    ends = pd.merge_asof(
        starts.sort_values("end_target"), benchmark, left_on="end_target", right_on="date", direction="backward"
    ).rename(columns={"value": "end_value"})
    ends["sp500_return_pct"] = (ends["end_value"] / ends["start_value"] - 1) * 100
    return ends.sort_values("row_order")["sp500_return_pct"].to_numpy()


def add_strategy(frame: pd.DataFrame, name: str, weights: dict[int | str, float]) -> pd.DataFrame:
    total_return = np.zeros(len(frame), dtype=float)
    exposure = np.zeros(len(frame), dtype=float)
    filled_count = np.zeros(len(frame), dtype=int)
    end_price = frame["one_year_end_price"].to_numpy()
    signal_price = frame["signal_price"].to_numpy()
    one_year_low = frame["one_year_low"].to_numpy()

    for level, weight in weights.items():
        if level == "immediate":
            valid = np.isfinite(signal_price) & np.isfinite(end_price)
            tranche_return = np.where(valid, end_price / signal_price - 1, 0.0)
            total_return += weight * tranche_return
            exposure += weight * valid
            filled_count += valid.astype(int)
            frame[f"{name}_即時購入_約定"] = valid
            frame[f"{name}_即時購入_約定価格"] = np.where(valid, signal_price, np.nan)
            continue
        limit_price = frame[f"予測_{level}pct指値"].to_numpy()
        valid = np.isfinite(limit_price) & np.isfinite(signal_price) & np.isfinite(one_year_low) & np.isfinite(end_price)
        immediate = valid & (limit_price >= signal_price)
        touched = valid & (one_year_low <= limit_price)
        filled = immediate | touched
        entry = np.where(immediate, signal_price, limit_price)
        tranche_return = np.where(filled, end_price / entry - 1, 0.0)
        total_return += weight * tranche_return
        exposure += weight * filled
        filled_count += filled.astype(int)
        frame[f"{name}_P{level}_約定"] = filled
        frame[f"{name}_P{level}_約定価格"] = np.where(filled, entry, np.nan)

    frame[f"{name}_約定段数"] = filled_count
    frame[f"{name}_投資比率"] = exposure
    frame[f"{name}_1年リターン_pct"] = total_return * 100
    invested_return = np.full(len(frame), np.nan, dtype=float)
    np.divide(total_return * 100, exposure, out=invested_return, where=exposure > 0)
    frame[f"{name}_投資資金ベース1年リターン_pct"] = invested_return
    return frame


def describe(frame: pd.DataFrame, name: str, tranche_count: int) -> dict[str, float | int | str]:
    returns = frame[f"{name}_1年リターン_pct"]
    invested = frame[f"{name}_投資比率"]
    benchmark = frame["sp500_1年リターン_pct"]
    valid_benchmark = returns.notna() & benchmark.notna()
    return {
        "strategy": name,
        "events": len(frame),
        "mean_return_pct": returns.mean(),
        "median_return_pct": returns.median(),
        "win_rate": (returns > 0).mean(),
        "loss_30pct_rate": (returns <= -30).mean(),
        "gain_50pct_rate": (returns >= 50).mean(),
        "mean_capital_exposure": invested.mean(),
        "mean_invested_capital_return_pct": frame[f"{name}_投資資金ベース1年リターン_pct"].mean(),
        "no_fill_rate": (invested == 0).mean(),
        "all_tranches_fill_rate": (frame[f"{name}_約定段数"] == tranche_count).mean(),
        "mean_signal_buy_return_pct": frame["signal_buy_1年リターン_pct"].mean(),
        "sp500_comparable_events": int(valid_benchmark.sum()),
        "mean_sp500_return_pct": benchmark[valid_benchmark].mean(),
        "mean_excess_vs_sp500_pctpt": (returns[valid_benchmark] - benchmark[valid_benchmark]).mean(),
        "sp500_outperformance_rate": (returns[valid_benchmark] > benchmark[valid_benchmark]).mean(),
    }


def cluster_bootstrap_excess(
    frame: pd.DataFrame, name: str, iterations: int = 5000, seed: int = 20260713
) -> dict[str, float]:
    valid = frame[["銘柄", f"{name}_1年リターン_pct", "sp500_1年リターン_pct"]].dropna().copy()
    valid["excess"] = valid[f"{name}_1年リターン_pct"] - valid["sp500_1年リターン_pct"]
    ticker_values = [group["excess"].to_numpy() for _, group in valid.groupby("銘柄")]
    rng = np.random.default_rng(seed)
    estimates = np.empty(iterations)
    cluster_count = len(ticker_values)
    for iteration in range(iterations):
        sampled = rng.integers(0, cluster_count, cluster_count)
        estimates[iteration] = np.concatenate([ticker_values[index] for index in sampled]).mean()
    return {
        "excess_bootstrap_ci_low_pctpt": float(np.quantile(estimates, 0.025)),
        "excess_bootstrap_ci_high_pctpt": float(np.quantile(estimates, 0.975)),
        "bootstrap_probability_excess_positive": float((estimates > 0).mean()),
    }


def main() -> None:
    predictions = pd.read_csv(PREDICTIONS, encoding="utf-8-sig")
    events = pd.read_csv(EVENTS, encoding="utf-8-sig", low_memory=False)
    source_columns = [
        "底打ち候補日価格",
        "1年後リターン",
        "1年内最大下落率",
        "SEC_SIC大分類",
    ]
    source = events.loc[predictions["row_index"].astype(int), source_columns].reset_index(drop=True)
    frame = pd.concat([predictions.reset_index(drop=True), source], axis=1)
    frame["signal_price"] = numeric(frame["底打ち候補日価格"])
    frame["signal_buy_1年リターン_pct"] = numeric(frame["1年後リターン"])
    frame["one_year_end_price"] = frame["signal_price"] * (1 + frame["signal_buy_1年リターン_pct"] / 100)
    frame["one_year_low"] = frame["signal_price"] * (1 + numeric(frame["1年内最大下落率"]) / 100)
    required = ["signal_price", "one_year_end_price", "one_year_low", "予測_35pct指値", "予測_50pct指値", "予測_70pct指値"]
    frame = frame.dropna(subset=required).copy()
    frame["sp500_1年リターン_pct"] = benchmark_returns(frame["底打ち候補日"], load_sp500())

    summaries = []
    for name, weights in STRATEGIES.items():
        frame = add_strategy(frame, name, weights)
        row = describe(frame, name, len(weights))
        row.update(cluster_bootstrap_excess(frame, name))
        summaries.append(row)

    summary = pd.DataFrame(summaries)
    cohort_rows = []
    frame["signal_year"] = pd.to_datetime(frame["底打ち候補日"]).dt.year
    for name in STRATEGIES:
        for year, group in frame.groupby("signal_year"):
            row = describe(group, name, len(STRATEGIES[name]))
            row["signal_year"] = int(year)
            cohort_rows.append(row)

    frame.to_csv(OUTPUT_DIR / "staged_limit_backtest_events.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_DIR / "staged_limit_backtest_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(cohort_rows).to_csv(
        OUTPUT_DIR / "staged_limit_backtest_by_year.csv", index=False, encoding="utf-8-sig"
    )
    print(summary.to_string(index=False))
    if summary["sp500_comparable_events"].max() == 0:
        print("\nS&P 500 comparison is pending: place FRED SP500 CSV at", BENCHMARK)


if __name__ == "__main__":
    main()
