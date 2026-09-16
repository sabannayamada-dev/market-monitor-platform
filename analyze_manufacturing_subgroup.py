from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import auc_score, correlation, rankdata


BASE = Path("outputs/us_stock_research")
SOURCE = BASE / "us_bottom_events_with_sec.csv"
BOOTSTRAPS = 2000


def bootstrap_auc(frame: pd.DataFrame, rng: np.random.Generator) -> tuple[float, float]:
    groups = [group.index.to_numpy() for _, group in frame.groupby("銘柄", sort=False)]
    values = []
    for _ in range(BOOTSTRAPS):
        picked = rng.integers(0, len(groups), len(groups))
        sample = frame.loc[np.concatenate([groups[index] for index in picked])]
        value = auc_score(sample["実績"].to_numpy(dtype=int), sample["予測値"].to_numpy())
        if np.isfinite(value):
            values.append(value)
    return tuple(np.quantile(values, [0.025, 0.975]).tolist())


def main() -> None:
    source = pd.read_csv(SOURCE, encoding="utf-8-sig", low_memory=False)
    manufacturing_indices = set(source.index[source["SEC_SIC大分類"].eq("製造")])
    rng = np.random.default_rng(20260713)

    practical = pd.read_csv(BASE / "practical_target_test_predictions.csv", encoding="utf-8-sig")
    practical = practical[practical["row_index"].isin(manufacturing_indices)].copy()
    binary_names = {
        "1年内大化け50%",
        "大化け深傷回避_上昇50%以上かつ深掘り35%未満",
        "1年内深掘り35%",
    }
    practical_rows = []
    for name, group in practical[practical["目的指標"].isin(binary_names)].groupby("目的指標"):
        y = group["実績"].to_numpy(dtype=int)
        score = group["予測値"].to_numpy(dtype=float)
        low, high = np.quantile(score, [0.2, 0.8])
        ci_low, ci_high = bootstrap_auc(group.reset_index(drop=True), rng)
        practical_rows.append(
            {
                "目的指標": name,
                "イベント数": len(group),
                "企業数": group["銘柄"].nunique(),
                "陽性率": y.mean(),
                "AUC": auc_score(y, score),
                "銘柄クラスタ95%下限": ci_low,
                "銘柄クラスタ95%上限": ci_high,
                "予測下位20%実現率": y[score <= low].mean(),
                "予測上位20%実現率": y[score >= high].mean(),
            }
        )
    practical_summary = pd.DataFrame(practical_rows)
    practical_summary.to_csv(
        BASE / "manufacturing_practical_target_summary.csv", index=False, encoding="utf-8-sig"
    )

    rolling = pd.read_csv(
        BASE / "backward_expanding_practical_target_predictions.csv", encoding="utf-8-sig"
    )
    rolling = rolling[rolling["row_index"].isin(manufacturing_indices)].copy()
    rolling_rows = []
    for name, target_frame in rolling.groupby("目的指標"):
        maximum_year = int(target_frame["検証年"].max())
        minimum_year = int(target_frame["検証年"].min())
        for start_year in range(maximum_year, minimum_year - 1, -1):
            group = target_frame[target_frame["検証年"] >= start_year]
            y = group["実績"].to_numpy(dtype=int)
            score = group["予測値"].to_numpy(dtype=float)
            rolling_rows.append(
                {
                    "目的指標": name,
                    "開始年": start_year,
                    "イベント数": len(group),
                    "企業数": group["銘柄"].nunique(),
                    "陽性率": y.mean(),
                    "AUC": auc_score(y, score),
                }
            )
    pd.DataFrame(rolling_rows).to_csv(
        BASE / "manufacturing_backward_expanding_summary.csv", index=False, encoding="utf-8-sig"
    )

    median_prediction = pd.read_csv(
        BASE / "median_peak_to_low_forecast_predictions.csv", encoding="utf-8-sig"
    )
    median_prediction = median_prediction[
        median_prediction["row_index"].isin(manufacturing_indices)
    ].copy()
    actual = pd.to_numeric(
        median_prediction["判定前最高値→候補後最安値下落率"], errors="coerce"
    ).to_numpy()
    predicted = pd.to_numeric(median_prediction["予測_50pct下落率"], errors="coerce").to_numpy()
    baseline = pd.to_numeric(
        median_prediction["単純中央値予測下落率"], errors="coerce"
    ).to_numpy()
    model_mae = np.mean(np.abs(actual - predicted))
    baseline_mae = np.mean(np.abs(actual - baseline))
    median_summary = pd.DataFrame(
        [
            {
                "イベント数": len(median_prediction),
                "企業数": median_prediction["銘柄"].nunique(),
                "予測より深く下落した割合": np.mean(actual < predicted),
                "MAE_pctpt": model_mae,
                "中央値絶対誤差_pctpt": np.median(np.abs(actual - predicted)),
                "単純中央値MAE_pctpt": baseline_mae,
                "MAE改善率_pct": (1 - model_mae / baseline_mae) * 100,
                "Pearson相関": correlation(actual, predicted),
                "Spearman相関": correlation(rankdata(actual), rankdata(predicted)),
            }
        ]
    )
    median_summary.to_csv(
        BASE / "manufacturing_median_peak_to_low_summary.csv", index=False, encoding="utf-8-sig"
    )
    median_prediction["製造業内危険度五分位"] = pd.qcut(
        median_prediction["予測_50pct下落率"].rank(method="first"),
        5,
        labels=[5, 4, 3, 2, 1],
    ).astype(int)
    median_quantiles = (
        median_prediction.groupby("製造業内危険度五分位", observed=True)
        .agg(
            件数=("銘柄", "size"),
            予測下落率平均=("予測_50pct下落率", "mean"),
            実際下落率平均=("判定前最高値→候補後最安値下落率", "mean"),
            実際下落率中央値=("判定前最高値→候補後最安値下落率", "median"),
        )
        .reset_index()
        .sort_values("製造業内危険度五分位")
    )
    median_quantiles.to_csv(
        BASE / "manufacturing_median_peak_to_low_quantiles.csv", index=False, encoding="utf-8-sig"
    )

    print("PRACTICAL TARGETS")
    print(practical_summary.to_string(index=False))
    print("\nMEDIAN FORECAST")
    print(median_summary.to_string(index=False))
    print("\nMEDIAN QUANTILES")
    print(median_quantiles.to_string(index=False))


if __name__ == "__main__":
    main()
