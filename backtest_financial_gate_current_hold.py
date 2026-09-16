from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs" / "us_stock_research"
DECISIONS = OUT / "financial_gate_backtest_decisions.csv"
EVENTS = OUT / "us_bottom_events_with_sec.csv"
SP500 = OUT / "sp500_fred.csv"


def load_sp500() -> pd.DataFrame:
    frame = pd.read_csv(SP500)
    frame["DATE"] = pd.to_datetime(frame["DATE"], errors="coerce")
    frame["SP500"] = pd.to_numeric(frame["SP500"], errors="coerce")
    return frame.dropna().sort_values("DATE")


def add_sp500_current_return(frame: pd.DataFrame) -> pd.DataFrame:
    benchmark = load_sp500()
    end_date = benchmark["DATE"].max()
    end_value = benchmark.loc[benchmark["DATE"] == end_date, "SP500"].iloc[-1]
    ordered = frame.sort_values("底打ち候補日").copy()
    ordered = pd.merge_asof(
        ordered,
        benchmark.rename(columns={"DATE": "sp500_start_date", "SP500": "sp500_start_value"}),
        left_on="底打ち候補日",
        right_on="sp500_start_date",
        direction="forward",
    )
    ordered["sp500_end_date"] = end_date
    ordered["sp500_current_return_pct"] = (end_value / ordered["sp500_start_value"] - 1) * 100
    years = (end_date - ordered["sp500_start_date"]).dt.days / 365.25
    ordered["sp500_current_cagr_pct"] = ((end_value / ordered["sp500_start_value"]) ** (1 / years) - 1) * 100
    return ordered.sort_index()


def summarize(frame: pd.DataFrame, buy: pd.Series, rule: str, level: float) -> dict:
    stock_return = frame["現在まで保有リターン"]
    stock_cagr = frame["年利換算"]
    sp_return = frame["sp500_current_return_pct"]
    sp_cagr = frame["sp500_current_cagr_pct"]
    selected = buy & stock_return.notna() & stock_cagr.notna() & sp_return.notna()
    return {
        "rule": rule,
        "exclusion_level": level,
        "signals": len(frame),
        "bought": int(selected.sum()),
        "purchase_rate": selected.mean(),
        "stock_mean_cumulative_return_pct": stock_return[selected].mean(),
        "stock_median_cumulative_return_pct": stock_return[selected].median(),
        "sp500_mean_cumulative_return_pct": sp_return[selected].mean(),
        "sp500_median_cumulative_return_pct": sp_return[selected].median(),
        "mean_cumulative_excess_vs_sp500_pctpt": (stock_return[selected] - sp_return[selected]).mean(),
        "median_cumulative_excess_vs_sp500_pctpt": (stock_return[selected] - sp_return[selected]).median(),
        "stock_mean_cagr_pct": stock_cagr[selected].mean(),
        "stock_median_cagr_pct": stock_cagr[selected].median(),
        "sp500_mean_cagr_pct": sp_cagr[selected].mean(),
        "mean_cagr_excess_vs_sp500_pctpt": (stock_cagr[selected] - sp_cagr[selected]).mean(),
        "sp500_cagr_outperformance_rate": (stock_cagr[selected] > sp_cagr[selected]).mean(),
        "loss_rate": (stock_return[selected] < 0).mean(),
        "gain_100pct_rate": (stock_return[selected] >= 100).mean(),
        "cash_included_mean_cumulative_return_pct": stock_return.where(buy, 0.0).mean(),
    }


def main() -> None:
    decisions = pd.read_csv(DECISIONS, encoding="utf-8-sig")
    events = pd.read_csv(EVENTS, encoding="utf-8-sig", low_memory=False)
    event_columns = ["現在まで保有リターン", "年利換算"]
    event_data = events.loc[decisions["row_index"].astype(int), event_columns].reset_index(drop=True)
    frame = pd.concat([decisions.reset_index(drop=True), event_data], axis=1)
    frame["底打ち候補日"] = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    frame["現在まで保有リターン"] = pd.to_numeric(frame["現在まで保有リターン"], errors="coerce")
    frame["年利換算"] = pd.to_numeric(frame["年利換算"], errors="coerce")
    frame = add_sp500_current_return(frame)

    rows = []
    baseline_frame = frame[frame["exclusion_level_each_side"] == 0.10].copy()
    rows.append(summarize(baseline_frame, pd.Series(True, index=baseline_frame.index), "即時購入100%", 0.0))
    for level, group in frame.groupby("exclusion_level_each_side"):
        rows.append(summarize(group, group["safe_big_gate_buy"].astype(bool), "大化けかつ深傷回避モデル", float(level)))
        rows.append(summarize(group, group["big_gate_buy"].astype(bool), "大化け低確率だけ除外", float(level)))

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "financial_gate_current_hold_summary.csv", index=False, encoding="utf-8-sig")
    frame.to_csv(OUT / "financial_gate_current_hold_events.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
