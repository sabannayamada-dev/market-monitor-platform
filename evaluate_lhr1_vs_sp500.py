from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


OUT = Path("outputs/us_stock_research")


def bootstrap_paired(values: pd.DataFrame, column: str, iterations: int = 5000) -> dict:
    blocks = [group[column].to_numpy() for _, group in values.groupby("銘柄")]
    rng = np.random.default_rng(20260713)
    estimates = np.empty(iterations)
    for iteration in range(iterations):
        sampled = rng.integers(0, len(blocks), len(blocks))
        estimates[iteration] = np.concatenate([blocks[index] for index in sampled]).mean()
    return {
        "bootstrap_ci_low": float(np.quantile(estimates, 0.025)),
        "bootstrap_ci_high": float(np.quantile(estimates, 0.975)),
        "bootstrap_probability_positive": float((estimates > 0).mean()),
    }


def main() -> None:
    predictions = pd.read_csv(OUT / "lhr1_test_predictions.csv", encoding="utf-8-sig")
    benchmark = pd.read_csv(OUT / "financial_gate_current_hold_events.csv", encoding="utf-8-sig", low_memory=False)
    benchmark = benchmark[
        ["row_index", "sp500_current_return_pct", "sp500_current_cagr_pct"]
    ].drop_duplicates("row_index")
    frame = predictions.merge(benchmark, on="row_index", how="left")
    frame["cumulative_excess_pctpt"] = (
        frame["actual_current_return_pct"] - frame["sp500_current_return_pct"]
    )
    frame["cagr_excess_pctpt"] = frame["actual_current_cagr_pct"] - frame["sp500_current_cagr_pct"]
    frame["selection"] = np.where(frame["score_percentile"] >= 0.8, "LHR-1上位20%", "その他80%")

    rows = []
    for label, group in [("全件", frame), *list(frame.groupby("selection"))]:
        row = {
            "model": "LHR-1",
            "selection": label,
            "events": len(group),
            "stock_mean_current_return_pct": group["actual_current_return_pct"].mean(),
            "stock_median_current_return_pct": group["actual_current_return_pct"].median(),
            "sp500_mean_current_return_pct": group["sp500_current_return_pct"].mean(),
            "stock_mean_cagr_pct": group["actual_current_cagr_pct"].mean(),
            "stock_median_cagr_pct": group["actual_current_cagr_pct"].median(),
            "sp500_mean_cagr_pct": group["sp500_current_cagr_pct"].mean(),
            "mean_cagr_excess_pctpt": group["cagr_excess_pctpt"].mean(),
            "cagr_outperformance_rate": (group["cagr_excess_pctpt"] > 0).mean(),
            "loss_rate": (group["actual_current_return_pct"] < 0).mean(),
            "gain_100pct_rate": (group["actual_current_return_pct"] >= 100).mean(),
        }
        row.update(bootstrap_paired(group, "cagr_excess_pctpt"))
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "lhr1_sp500_comparison.csv", index=False, encoding="utf-8-sig")
    frame.to_csv(OUT / "lhr1_sp500_comparison_events.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
