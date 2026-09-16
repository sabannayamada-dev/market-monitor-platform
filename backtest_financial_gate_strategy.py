from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs" / "us_stock_research"
MODEL_PREDICTIONS = OUT / "backward_expanding_practical_target_predictions.csv"
EVENTS = OUT / "us_bottom_events_with_sec.csv"
BENCHMARK_EVENTS = OUT / "staged_limit_backtest_events.csv"

BIG_TARGET = "1年内大化け50%"
RISK_TARGET = "1年内深掘り35%"
SAFE_BIG_TARGET = "大化け深傷回避_上昇50%以上かつ深掘り35%未満"
TEST_YEARS = (2023, 2024)
EXCLUSION_LEVELS = (0.10, 0.20, 0.30, 0.40)


def prepare() -> tuple[pd.DataFrame, pd.DataFrame]:
    predictions = pd.read_csv(MODEL_PREDICTIONS, encoding="utf-8-sig")
    wide = predictions.pivot_table(
        index=["検証年", "row_index", "銘柄"], columns="目的指標", values="予測値", aggfunc="first"
    ).reset_index()
    needed = {BIG_TARGET, RISK_TARGET, SAFE_BIG_TARGET}
    missing = needed.difference(wide.columns)
    if missing:
        raise ValueError(f"必要なモデル予測がありません: {sorted(missing)}")

    events = pd.read_csv(EVENTS, encoding="utf-8-sig", low_memory=False)
    row_indices = wide["row_index"].astype(int)
    event_data = events.loc[
        row_indices,
        ["底打ち候補日", "1年後リターン", "1年内最大上昇率", "1年内最大下落率", "SEC_SIC大分類"],
    ].reset_index(drop=True)
    wide = pd.concat([wide.reset_index(drop=True), event_data], axis=1)

    benchmark = pd.read_csv(BENCHMARK_EVENTS, encoding="utf-8-sig", low_memory=False)[
        ["row_index", "sp500_1年リターン_pct"]
    ].drop_duplicates("row_index")
    wide = wide.merge(benchmark, on="row_index", how="left")
    for column in [BIG_TARGET, RISK_TARGET, SAFE_BIG_TARGET, "1年後リターン", "1年内最大上昇率", "1年内最大下落率"]:
        wide[column] = pd.to_numeric(wide[column], errors="coerce")
    return predictions, wide


def historical_thresholds(
    predictions: pd.DataFrame, year: int, exclusion_level: float
) -> tuple[float, float]:
    historical = predictions[predictions["検証年"] < year]
    big = pd.to_numeric(
        historical.loc[historical["目的指標"] == BIG_TARGET, "予測値"], errors="coerce"
    ).dropna()
    risk = pd.to_numeric(
        historical.loc[historical["目的指標"] == RISK_TARGET, "予測値"], errors="coerce"
    ).dropna()
    return float(big.quantile(exclusion_level)), float(risk.quantile(1 - exclusion_level))


def historical_safe_big_floor(predictions: pd.DataFrame, year: int, exclusion_level: float) -> float:
    values = pd.to_numeric(
        predictions.loc[
            (predictions["検証年"] < year) & (predictions["目的指標"] == SAFE_BIG_TARGET), "予測値"
        ],
        errors="coerce",
    ).dropna()
    return float(values.quantile(exclusion_level))


def evaluate(group: pd.DataFrame, buy: pd.Series, label: str, level: float, year: int | str) -> dict:
    returns = group["1年後リターン"]
    benchmark = group["sp500_1年リターン_pct"]
    selected_returns = returns[buy]
    selected_benchmark = benchmark[buy]
    actual_big = group["1年内最大上昇率"] >= 50
    actual_deep = group["1年内最大下落率"] <= -35
    cash_included = returns.where(buy, 0.0)
    return {
        "rule": label,
        "exclusion_level_each_side": level,
        "year": year,
        "all_signals": len(group),
        "bought": int(buy.sum()),
        "excluded": int((~buy).sum()),
        "purchase_rate": buy.mean(),
        "selected_mean_return_pct": selected_returns.mean(),
        "selected_median_return_pct": selected_returns.median(),
        "selected_win_rate": (selected_returns > 0).mean(),
        "selected_loss_30pct_rate": (selected_returns <= -30).mean(),
        "selected_gain_50pct_rate": (selected_returns >= 50).mean(),
        "selected_mean_sp500_return_pct": selected_benchmark.mean(),
        "selected_excess_vs_sp500_pctpt": (selected_returns - selected_benchmark).mean(),
        "selected_sp500_outperformance_rate": (selected_returns > selected_benchmark).mean(),
        "cash_included_mean_return_pct": cash_included.mean(),
        "all_signal_mean_sp500_return_pct": benchmark.mean(),
        "big_winner_capture_rate": (buy & actual_big).sum() / actual_big.sum() if actual_big.any() else np.nan,
        "deep_loss_avoidance_rate": ((~buy) & actual_deep).sum() / actual_deep.sum() if actual_deep.any() else np.nan,
        "excluded_big_winners": int(((~buy) & actual_big).sum()),
        "excluded_deep_losses": int(((~buy) & actual_deep).sum()),
    }


def main() -> None:
    predictions, frame = prepare()
    test = frame[frame["検証年"].isin(TEST_YEARS)].dropna(
        subset=[BIG_TARGET, RISK_TARGET, SAFE_BIG_TARGET, "1年後リターン", "sp500_1年リターン_pct"]
    ).copy()
    results = []
    decisions = []

    baseline = pd.Series(True, index=test.index)
    results.append(evaluate(test, baseline, "即時購入100%", 0.0, "2023-2024"))

    for level in EXCLUSION_LEVELS:
        combined_all_buy = pd.Series(False, index=test.index)
        risk_all_buy = pd.Series(False, index=test.index)
        big_all_buy = pd.Series(False, index=test.index)
        safe_big_all_buy = pd.Series(False, index=test.index)
        for year in TEST_YEARS:
            year_mask = test["検証年"] == year
            big_floor, risk_ceiling = historical_thresholds(predictions, year, level)
            safe_big_floor = historical_safe_big_floor(predictions, year, level)
            combined_buy = year_mask & (test[BIG_TARGET] >= big_floor) & (test[RISK_TARGET] <= risk_ceiling)
            risk_buy = year_mask & (test[RISK_TARGET] <= risk_ceiling)
            big_buy = year_mask & (test[BIG_TARGET] >= big_floor)
            safe_big_buy = year_mask & (test[SAFE_BIG_TARGET] >= safe_big_floor)
            combined_all_buy |= combined_buy
            risk_all_buy |= risk_buy
            big_all_buy |= big_buy
            safe_big_all_buy |= safe_big_buy
            for label, buy in (
                ("両方ゲート", combined_buy),
                ("深傷リスクだけ除外", risk_buy),
                ("大化け低確率だけ除外", big_buy),
                ("大化けかつ深傷回避モデル", safe_big_buy),
            ):
                results.append(evaluate(test[year_mask], buy[year_mask], label, level, year))
            year_decisions = test[year_mask].copy()
            year_decisions["big_probability_floor"] = big_floor
            year_decisions["deep_risk_ceiling"] = risk_ceiling
            year_decisions["combined_buy"] = combined_buy[year_mask]
            year_decisions["risk_gate_buy"] = risk_buy[year_mask]
            year_decisions["big_gate_buy"] = big_buy[year_mask]
            year_decisions["safe_big_gate_buy"] = safe_big_buy[year_mask]
            year_decisions["safe_big_probability_floor"] = safe_big_floor
            year_decisions["exclusion_level_each_side"] = level
            decisions.append(year_decisions)
        for label, buy in (
            ("両方ゲート", combined_all_buy),
            ("深傷リスクだけ除外", risk_all_buy),
            ("大化け低確率だけ除外", big_all_buy),
            ("大化けかつ深傷回避モデル", safe_big_all_buy),
        ):
            results.append(evaluate(test, buy, label, level, "2023-2024"))

    result_frame = pd.DataFrame(results)
    decision_frame = pd.concat(decisions, ignore_index=True)
    result_frame.to_csv(OUT / "financial_gate_backtest_summary.csv", index=False, encoding="utf-8-sig")
    decision_frame.to_csv(OUT / "financial_gate_backtest_decisions.csv", index=False, encoding="utf-8-sig")
    print(result_frame[result_frame["year"] == "2023-2024"].to_string(index=False))


if __name__ == "__main__":
    main()
