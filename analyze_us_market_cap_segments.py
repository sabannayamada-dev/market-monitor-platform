from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DATA_PATH = Path("outputs/us_stock_research/us_bottom_events_with_sec.csv")
BENCHMARK_PATH = Path("outputs/us_stock_research/financial_gate_current_hold_events.csv")
OUTPUT_DIR = Path("outputs/us_stock_research")
MARKET_CAP = "底検知時_SEC_時価総額"
DATE = "底打ち候補日"
TICKER = "銘柄"
RETURN = "現在まで保有リターン"
CAGR = "年利換算"
RETURN_1Y = "1年後リターン"
SP500_RETURN = "sp500_current_return_pct"
SP500_CAGR = "sp500_current_cagr_pct"
SP500_RETURN_1Y = "sp500_1年リターン_pct"


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def summarize(group: pd.DataFrame, label: str, segment: str) -> dict[str, float | int | str]:
    current_excess = group[CAGR] - group[SP500_CAGR]
    one_year_excess = group[RETURN_1Y] - group[SP500_RETURN_1Y]
    return {
        "period": label,
        "segment": segment,
        "events": len(group),
        "tickers": group[TICKER].nunique(),
        "market_cap_median_usd": group[MARKET_CAP].median(),
        "current_return_mean_pct": group[RETURN].mean(),
        "current_return_median_pct": group[RETURN].median(),
        "current_cagr_mean_pct": group[CAGR].mean(),
        "current_cagr_median_pct": group[CAGR].median(),
        "sp500_current_cagr_mean_pct": group[SP500_CAGR].mean(),
        "current_cagr_excess_mean_pctpt": current_excess.mean(),
        "current_cagr_excess_median_pctpt": current_excess.median(),
        "current_cagr_beat_sp500_rate": (current_excess > 0).mean(),
        "loss_rate": (group[RETURN] < 0).mean(),
        "gain_100pct_rate": (group[RETURN] >= 100).mean(),
        "one_year_available": one_year_excess.notna().sum(),
        "one_year_return_mean_pct": group[RETURN_1Y].mean(),
        "sp500_one_year_return_mean_pct": group[SP500_RETURN_1Y].mean(),
        "one_year_excess_mean_pctpt": one_year_excess.mean(),
        "one_year_beat_sp500_rate": (one_year_excess > 0).mean(),
    }


def cluster_bootstrap_difference(
    frame: pd.DataFrame,
    selected_quintile: int,
    iterations: int = 5000,
) -> pd.DataFrame:
    rng = np.random.default_rng(20260713)
    groups = [group.index.to_numpy() for _, group in frame.groupby(TICKER)]
    metrics = {
        "current_cagr_excess_difference_pctpt": [],
        "current_cagr_median_difference_pctpt": [],
        "one_year_return_difference_pctpt": [],
        "one_year_excess_difference_pctpt": [],
        "loss_rate_difference": [],
    }
    for _ in range(iterations):
        sampled = rng.integers(0, len(groups), len(groups))
        indexes = np.concatenate([groups[index] for index in sampled])
        sample = frame.loc[indexes]
        selected = sample["year_market_cap_quintile"].eq(selected_quintile)
        other = ~selected
        if selected.sum() < 5 or other.sum() < 5:
            continue
        selected_current = (sample.loc[selected, CAGR] - sample.loc[selected, SP500_CAGR]).mean()
        other_current = (sample.loc[other, CAGR] - sample.loc[other, SP500_CAGR]).mean()
        metrics["current_cagr_excess_difference_pctpt"].append(selected_current - other_current)
        metrics["current_cagr_median_difference_pctpt"].append(
            sample.loc[selected, CAGR].median() - sample.loc[other, CAGR].median()
        )
        metrics["one_year_return_difference_pctpt"].append(
            sample.loc[selected, RETURN_1Y].mean() - sample.loc[other, RETURN_1Y].mean()
        )
        selected_one_year = (
            sample.loc[selected, RETURN_1Y] - sample.loc[selected, SP500_RETURN_1Y]
        ).mean()
        other_one_year = (sample.loc[other, RETURN_1Y] - sample.loc[other, SP500_RETURN_1Y]).mean()
        metrics["one_year_excess_difference_pctpt"].append(selected_one_year - other_one_year)
        selected_loss = (sample.loc[selected, RETURN] < 0).mean()
        other_loss = (sample.loc[other, RETURN] < 0).mean()
        metrics["loss_rate_difference"].append(selected_loss - other_loss)
    rows = []
    for metric, values in metrics.items():
        array = np.asarray(values, dtype=float)
        array = array[np.isfinite(array)]
        rows.append(
            {
                "selected_quintile": selected_quintile,
                "metric": metric,
                "iterations": len(array),
                "estimate_mean": np.mean(array),
                "ci_2_5": np.quantile(array, 0.025),
                "ci_97_5": np.quantile(array, 0.975),
                "probability_positive": np.mean(array > 0),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    benchmark = pd.read_csv(BENCHMARK_PATH, encoding="utf-8-sig", low_memory=False)
    frame[DATE] = pd.to_datetime(frame[DATE], errors="coerce")
    benchmark[DATE] = pd.to_datetime(benchmark[DATE], errors="coerce")
    for column in (MARKET_CAP, RETURN, CAGR, RETURN_1Y):
        frame[column] = numeric(frame[column])
    for column in (SP500_RETURN, SP500_CAGR, SP500_RETURN_1Y):
        benchmark[column] = numeric(benchmark[column])

    benchmark = (
        benchmark[[TICKER, DATE, SP500_RETURN, SP500_CAGR, SP500_RETURN_1Y]]
        .drop_duplicates([TICKER, DATE], keep="last")
    )
    data = frame.merge(benchmark, on=[TICKER, DATE], how="left", validate="many_to_one")
    data = data.loc[data[DATE].notna() & data[MARKET_CAP].gt(0) & data[CAGR].notna()].copy()
    data["signal_year"] = data[DATE].dt.year.astype(int)
    data["year_market_cap_percentile"] = data.groupby("signal_year")[MARKET_CAP].rank(
        method="average", pct=True
    )
    data["year_market_cap_quintile"] = np.ceil(data["year_market_cap_percentile"] * 5).clip(1, 5).astype(int)
    data["period"] = np.select(
        [
            data["signal_year"].le(2020),
            data["signal_year"].isin([2021, 2022]),
            data["signal_year"].isin([2023, 2024]),
        ],
        ["train_through_2020", "validation_2021_2022", "test_2023_2024"],
        default="recent_incomplete_2025_2026",
    )

    fixed_edges = [0, 5e8, 1e9, 3e9, 1e10, 3e10, 1e11, np.inf]
    fixed_labels = ["<0.5B", "0.5-1B", "1-3B", "3-10B", "10-30B", "30-100B", ">=100B"]
    data["fixed_market_cap_band_usd"] = pd.cut(
        data[MARKET_CAP], bins=fixed_edges, labels=fixed_labels, right=False
    )

    quintile_rows = []
    fixed_rows = []
    for period, period_frame in data.groupby("period", sort=False):
        for quintile, group in period_frame.groupby("year_market_cap_quintile"):
            quintile_rows.append(summarize(group, period, f"Q{int(quintile)}"))
        for band, group in period_frame.groupby("fixed_market_cap_band_usd", observed=True):
            fixed_rows.append(summarize(group, period, str(band)))
    for quintile, group in data.groupby("year_market_cap_quintile"):
        quintile_rows.append(summarize(group, "all", f"Q{int(quintile)}"))
    for band, group in data.groupby("fixed_market_cap_band_usd", observed=True):
        fixed_rows.append(summarize(group, "all", str(band)))

    quintiles = pd.DataFrame(quintile_rows)
    fixed = pd.DataFrame(fixed_rows)
    train_table = quintiles.loc[quintiles["period"].eq("train_through_2020")]
    selected_quintile = int(
        train_table.sort_values(
            ["one_year_return_mean_pct", "current_cagr_median_pct"],
            ascending=False,
        )["segment"].iloc[0][1:]
    )
    test = data.loc[data["period"].eq("test_2023_2024")].copy()
    bootstrap = cluster_bootstrap_difference(test, selected_quintile)

    selection_rows = []
    for period in ("train_through_2020", "validation_2021_2022", "test_2023_2024"):
        period_frame = data.loc[data["period"].eq(period)]
        for name, group in (
            (f"Q{selected_quintile}_selected_on_train", period_frame.loc[period_frame["year_market_cap_quintile"].eq(selected_quintile)]),
            ("other_quintiles", period_frame.loc[~period_frame["year_market_cap_quintile"].eq(selected_quintile)]),
        ):
            selection_rows.append(summarize(group, period, name))

    coverage = pd.DataFrame(
        [
            {
                "source_events": len(frame),
                "valid_market_cap_and_cagr_events": len(data),
                "unique_tickers": data[TICKER].nunique(),
                "market_cap_coverage_rate": len(data) / len(frame),
                "sp500_current_benchmark_coverage_rate": data[SP500_CAGR].notna().mean(),
                "one_year_return_coverage_rate": data[RETURN_1Y].notna().mean(),
                "selected_quintile_from_train": selected_quintile,
                "market_cap_median_usd": data[MARKET_CAP].median(),
                "market_cap_p10_usd": data[MARKET_CAP].quantile(0.1),
                "market_cap_p90_usd": data[MARKET_CAP].quantile(0.9),
            }
        ]
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    quintiles.to_csv(OUTPUT_DIR / "us_market_cap_quintile_analysis.csv", index=False, encoding="utf-8-sig")
    fixed.to_csv(OUTPUT_DIR / "us_market_cap_fixed_band_analysis.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(selection_rows).to_csv(
        OUTPUT_DIR / "us_market_cap_out_of_time_selection.csv", index=False, encoding="utf-8-sig"
    )
    bootstrap.to_csv(OUTPUT_DIR / "us_market_cap_bootstrap.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUTPUT_DIR / "us_market_cap_analysis_coverage.csv", index=False, encoding="utf-8-sig")
    data[[TICKER, "企業名", DATE, MARKET_CAP, "signal_year", "year_market_cap_percentile", "year_market_cap_quintile", "fixed_market_cap_band_usd", RETURN, CAGR, RETURN_1Y, SP500_CAGR, SP500_RETURN_1Y]].to_csv(
        OUTPUT_DIR / "us_market_cap_event_assignments.csv", index=False, encoding="utf-8-sig"
    )
    print(coverage.to_string(index=False))
    print("\nOUT-OF-TIME SELECTION")
    print(pd.DataFrame(selection_rows).to_string(index=False))
    print("\nBOOTSTRAP")
    print(bootstrap.to_string(index=False))


if __name__ == "__main__":
    main()
