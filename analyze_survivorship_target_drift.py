from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("outputs/us_stock_research/us_bottom_events_with_sec.csv")
OUTPUT_DIR = Path("outputs/us_stock_research")


def number(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)


def summarize(frame: pd.DataFrame, label: str, start_year: int, end_year: int) -> dict:
    one_year_return = number(frame, "1年後リターン")
    upside = number(frame, "1年内最大上昇率")
    drawdown = number(frame, "1年内最大下落率")
    valid = one_year_return.notna() & upside.notna() & drawdown.notna()
    sample = frame.loc[valid]
    one_year_return = one_year_return[valid]
    upside = upside[valid]
    drawdown = drawdown[valid]
    big_winner = upside >= 50
    deep_loss = drawdown <= -35
    clean_big_winner = big_winner & ~deep_loss
    annualized = number(frame, "年利換算") if "年利換算" in frame.columns else pd.Series(np.nan, index=frame.index)
    current_return = (
        number(frame, "現在まで保有リターン")
        if "現在まで保有リターン" in frame.columns
        else pd.Series(np.nan, index=frame.index)
    )
    event_dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    observation_days = (pd.Timestamp("2026-07-13") - event_dates).dt.days
    minimum_horizon = observation_days >= 365
    annualized_valid = annualized[minimum_horizon].dropna()
    current_return_valid = current_return[minimum_horizon].dropna()
    if len(annualized_valid) >= 10:
        lower, upper = annualized_valid.quantile([0.1, 0.9])
        annualized_trimmed = annualized_valid[annualized_valid.between(lower, upper)]
    else:
        annualized_trimmed = annualized_valid
    return {
        "対象期間": label,
        "開始年": start_year,
        "終了年": end_year,
        "全イベント数": len(frame),
        "1年観測完了イベント数": int(valid.sum()),
        "企業数": sample["銘柄"].nunique(),
        "1年内50%以上上昇率_pct": big_winner.mean() * 100,
        "1年内35%以上下落率_pct": deep_loss.mean() * 100,
        "大化け深傷回避率_pct": clean_big_winner.mean() * 100,
        "1年後プラス率_pct": (one_year_return > 0).mean() * 100,
        "1年後リターン平均_pct": one_year_return.mean(),
        "1年後リターン中央値_pct": one_year_return.median(),
        "1年内最大上昇率平均_pct": upside.mean(),
        "1年内最大上昇率中央値_pct": upside.median(),
        "1年内最大下落率平均_pct": drawdown.mean(),
        "1年内最大下落率中央値_pct": drawdown.median(),
        "現在保有年利_有効件数": len(annualized_valid),
        "現在保有年利平均_pct": annualized_valid.mean(),
        "現在保有年利中央値_pct": annualized_valid.median(),
        "現在保有年利10pctトリム平均_pct": annualized_trimmed.mean(),
        "現在保有年利プラス率_pct": (annualized_valid > 0).mean() * 100,
        "現在保有年利10pct点_pct": annualized_valid.quantile(0.1),
        "現在保有年利90pct点_pct": annualized_valid.quantile(0.9),
        "現在まで保有リターン平均_pct": current_return_valid.mean(),
        "現在まで保有リターン中央値_pct": current_return_valid.median(),
    }


def main() -> None:
    frame = pd.read_csv(INPUT_PATH, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    years = dates.dt.year
    valid_years = years.dropna().astype(int)
    end_year = int(valid_years.max())
    minimum_year = int(valid_years.min())

    expanding_rows = []
    for start_year in range(end_year, minimum_year - 1, -1):
        window = frame.loc[years.between(start_year, end_year)].copy()
        summary = summarize(
            window,
            f"現在～{start_year}年（開始年を遡及）",
            start_year,
            end_year,
        )
        if summary["1年観測完了イベント数"] > 0:
            expanding_rows.append(summary)

    annual_rows = []
    for year in range(minimum_year, end_year + 1):
        cohort = frame.loc[years.eq(year)].copy()
        if cohort.empty:
            continue
        summary = summarize(cohort, f"{year}年単年", year, year)
        if summary["1年観測完了イベント数"] > 0:
            annual_rows.append(summary)

    expanding = pd.DataFrame(expanding_rows)
    annual = pd.DataFrame(annual_rows)
    expanding.to_csv(
        OUTPUT_DIR / "survivorship_target_backward_expanding_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    annual.to_csv(
        OUTPUT_DIR / "survivorship_target_annual_cohorts.csv",
        index=False,
        encoding="utf-8-sig",
    )
    display_columns = [
        "対象期間",
        "1年観測完了イベント数",
        "1年内50%以上上昇率_pct",
        "1年内35%以上下落率_pct",
        "大化け深傷回避率_pct",
        "1年後リターン平均_pct",
        "1年後リターン中央値_pct",
        "現在保有年利平均_pct",
        "現在保有年利中央値_pct",
        "現在保有年利10pctトリム平均_pct",
        "現在保有年利プラス率_pct",
    ]
    print(expanding[display_columns].to_string(index=False))
    print("\nANNUAL COHORTS")
    print(annual[display_columns].to_string(index=False))


if __name__ == "__main__":
    main()
