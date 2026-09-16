from __future__ import annotations

from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("outputs/us_stock_research/practical_target_test_predictions.csv")
RESULT_PATH = Path("outputs/us_stock_research/practical_target_time_split_results.csv")
OUTPUT_PATH = Path("outputs/us_stock_research/practical_target_significance_tests.csv")
BOOTSTRAP_SAMPLES = 3000
RANDOM_SEED = 20260713


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def auc_and_variance(y: np.ndarray, score: np.ndarray) -> tuple[float, float]:
    positives = score[y == 1]
    negatives = score[y == 0]
    if len(positives) < 2 or len(negatives) < 2:
        return np.nan, np.nan
    comparisons = (positives[:, None] > negatives[None, :]).astype(float)
    comparisons += 0.5 * (positives[:, None] == negatives[None, :])
    positive_placements = comparisons.mean(axis=1)
    negative_placements = comparisons.mean(axis=0)
    auc = positive_placements.mean()
    variance = (
        positive_placements.var(ddof=1) / len(positives)
        + negative_placements.var(ddof=1) / len(negatives)
    )
    return float(auc), float(variance)


def auc_only(y: np.ndarray, score: np.ndarray) -> float:
    return auc_and_variance(y, score)[0]


def holm_adjust(p_values: list[float]) -> list[float]:
    order = np.argsort(p_values)
    adjusted = np.empty(len(p_values), dtype=float)
    running = 0.0
    count = len(p_values)
    for rank, index in enumerate(order):
        value = min(1.0, (count - rank) * p_values[index])
        running = max(running, value)
        adjusted[index] = running
    return adjusted.tolist()


def cluster_bootstrap(
    frame: pd.DataFrame, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    grouped = [group.index.to_numpy() for _, group in frame.groupby("銘柄", sort=False)]
    auc_values: list[float] = []
    top_bottom_differences: list[float] = []
    top_bottom_risk_ratios: list[float] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled_groups = rng.integers(0, len(grouped), size=len(grouped))
        sampled_indices = np.concatenate([grouped[index] for index in sampled_groups])
        sample = frame.loc[sampled_indices]
        y = sample["実績"].to_numpy(dtype=int)
        score = sample["予測値"].to_numpy(dtype=float)
        auc = auc_only(y, score)
        if np.isfinite(auc):
            auc_values.append(auc)
        low_threshold = np.quantile(score, 0.2)
        high_threshold = np.quantile(score, 0.8)
        low_rate = y[score <= low_threshold].mean()
        high_rate = y[score >= high_threshold].mean()
        top_bottom_differences.append(float(high_rate - low_rate))
        top_bottom_risk_ratios.append(float(high_rate / low_rate) if low_rate > 0 else np.nan)
    return (
        np.asarray(auc_values),
        np.asarray(top_bottom_differences),
        np.asarray(top_bottom_risk_ratios),
    )


def interval(values: np.ndarray) -> tuple[float, float]:
    values = values[np.isfinite(values)]
    return tuple(np.quantile(values, [0.025, 0.975]).tolist())


def main() -> None:
    predictions = pd.read_csv(INPUT_PATH, encoding="utf-8-sig")
    model_results = pd.read_csv(RESULT_PATH, encoding="utf-8-sig")
    binary_targets = set(model_results.loc[model_results["種類"] == "binary", "目的指標"])
    binary = predictions[
        predictions["目的指標"].isin(binary_targets) & predictions["実績"].isin([0.0, 1.0])
    ].copy()
    rng = np.random.default_rng(RANDOM_SEED)
    rows: list[dict] = []

    for target_name, frame in binary.groupby("目的指標", sort=False):
        frame = frame.reset_index(drop=True)
        y = frame["実績"].to_numpy(dtype=int)
        score = frame["予測値"].to_numpy(dtype=float)
        auc, variance = auc_and_variance(y, score)
        standard_error = sqrt(variance)
        z_value = (auc - 0.5) / standard_error
        p_one_sided = 1.0 - normal_cdf(z_value)
        auc_boot, difference_boot, risk_ratio_boot = cluster_bootstrap(frame, rng)
        auc_low, auc_high = interval(auc_boot)
        difference_low, difference_high = interval(difference_boot)
        rr_low, rr_high = interval(risk_ratio_boot)
        low_threshold = np.quantile(score, 0.2)
        high_threshold = np.quantile(score, 0.8)
        low_rate = y[score <= low_threshold].mean()
        high_rate = y[score >= high_threshold].mean()
        rows.append(
            {
                "目的指標": target_name,
                "検証イベント数": len(frame),
                "検証銘柄数": frame["銘柄"].nunique(),
                "陽性率": y.mean(),
                "AUC": auc,
                "DeLong型標準誤差": standard_error,
                "AUC片側p値": p_one_sided,
                "AUCクラスターブート95%下限": auc_low,
                "AUCクラスターブート95%上限": auc_high,
                "予測下位20%陽性率": low_rate,
                "予測上位20%陽性率": high_rate,
                "上位下位差": high_rate - low_rate,
                "上位下位差クラスターブート95%下限": difference_low,
                "上位下位差クラスターブート95%上限": difference_high,
                "上位下位リスク比": high_rate / low_rate if low_rate > 0 else np.nan,
                "リスク比クラスターブート95%下限": rr_low,
                "リスク比クラスターブート95%上限": rr_high,
            }
        )

    result = pd.DataFrame(rows)
    result["Holm補正p値_6目的指標"] = holm_adjust(result["AUC片側p値"].tolist())
    result["Bonferroni補正p値_全9目的指標想定"] = np.minimum(
        1.0, result["AUC片側p値"] * 9
    )
    result["5%水準_Holm有意"] = result["Holm補正p値_6目的指標"] < 0.05
    result["5%水準_銘柄クラスタCIも0.5超"] = result["AUCクラスターブート95%下限"] > 0.5
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
