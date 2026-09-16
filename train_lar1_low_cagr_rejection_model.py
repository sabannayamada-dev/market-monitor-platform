from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import DATA_PATH, EXCLUDE_TOKENS, feature_columns, numeric
from train_lhr1_long_horizon_model import (
    BLOCKED_FEATURE_TOKENS,
    fit_preprocessor,
    transform,
)


MODEL_NAME = "LAR-1"
MODEL_DESCRIPTION = "Low Annualized Return Rejection Model v1 / 低年利除外モデル"
OUTPUT_DIR = Path("outputs/us_stock_research")
TRAIN_END_YEAR = 2020
VALID_YEARS = (2021, 2022)
TEST_YEARS = (2023, 2024)
LOW_CAGR_FRACTION = 0.30
TOP_FEATURES = 50
BOOSTING_STAGES = (25, 50, 100, 150, 200)
REJECTION_FRACTIONS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
LEARNING_RATE = 0.05
MIN_LEAF = 30


def safe_divide(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else np.nan


def classification_metrics(actual_low: np.ndarray, rejected: np.ndarray) -> dict[str, float]:
    actual = np.asarray(actual_low, dtype=bool)
    predicted = np.asarray(rejected, dtype=bool)
    true_positive = int(np.sum(actual & predicted))
    false_positive = int(np.sum(~actual & predicted))
    false_negative = int(np.sum(actual & ~predicted))
    precision = safe_divide(true_positive, true_positive + false_positive)
    recall = safe_divide(true_positive, true_positive + false_negative)
    beta2 = 0.25
    f05 = safe_divide((1 + beta2) * precision * recall, beta2 * precision + recall)
    return {
        "precision": precision,
        "recall": recall,
        "f0_5": f05,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
    }


def select_features(x: pd.DataFrame, low_label: pd.Series, mask: pd.Series) -> list[str]:
    scored: list[tuple[float, str, int]] = []
    for column in x.columns:
        pair = pd.DataFrame({"x": x.loc[mask, column], "y": low_label[mask]}).dropna()
        if len(pair) < 150 or pair["x"].nunique() < 5 or pair["y"].nunique() < 2:
            continue
        score = pair.corr(numeric_only=True).iloc[0, 1]
        if np.isfinite(score):
            scored.append((abs(float(score)), column, len(pair)))
    scored.sort(reverse=True)
    return [column for _, column, _ in scored[:TOP_FEATURES]]


def rejection_mask(scores: np.ndarray, rejection_fraction: float) -> np.ndarray:
    count = max(1, int(round(len(scores) * rejection_fraction)))
    order = np.argsort(np.asarray(scores), kind="mergesort")
    rejected = np.zeros(len(scores), dtype=bool)
    rejected[order[-count:]] = True
    return rejected


def sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_boosted_stumps(
    x: np.ndarray,
    y: np.ndarray,
    stages: int,
) -> tuple[float, list[tuple[int, float, float, float]]]:
    base_rate = float(np.clip(np.mean(y), 1e-5, 1.0 - 1e-5))
    intercept = float(np.log(base_rate / (1.0 - base_rate)))
    raw_score = np.full(len(y), intercept, dtype=float)
    stumps: list[tuple[int, float, float, float]] = []
    thresholds = [
        np.unique(np.quantile(x[:, feature_index], np.linspace(0.05, 0.95, 19)))
        for feature_index in range(x.shape[1])
    ]
    for _ in range(stages):
        probability = sigmoid(raw_score)
        gradient = y - probability
        hessian = np.maximum(probability * (1.0 - probability), 1e-6)
        best_gain = -np.inf
        best_stump: tuple[int, float, float, float] | None = None
        for feature_index, candidates in enumerate(thresholds):
            values = x[:, feature_index]
            for threshold in candidates:
                left = values <= threshold
                left_count = int(left.sum())
                right_count = len(values) - left_count
                if left_count < MIN_LEAF or right_count < MIN_LEAF:
                    continue
                left_gradient = float(np.sum(gradient[left]))
                right_gradient = float(np.sum(gradient[~left]))
                left_hessian = float(np.sum(hessian[left]))
                right_hessian = float(np.sum(hessian[~left]))
                left_value = left_gradient / (left_hessian + 1.0)
                right_value = right_gradient / (right_hessian + 1.0)
                gain = left_gradient**2 / (left_hessian + 1.0) + right_gradient**2 / (right_hessian + 1.0)
                if gain > best_gain:
                    best_gain = gain
                    best_stump = (feature_index, float(threshold), left_value, right_value)
        if best_stump is None:
            break
        feature_index, threshold, left_value, right_value = best_stump
        raw_score += LEARNING_RATE * np.where(
            x[:, feature_index] <= threshold,
            left_value,
            right_value,
        )
        stumps.append(best_stump)
    return intercept, stumps


def predict_boosted_stumps(
    x: np.ndarray,
    intercept: float,
    stumps: list[tuple[int, float, float, float]],
    stages: int | None = None,
) -> np.ndarray:
    raw_score = np.full(len(x), intercept, dtype=float)
    selected_stumps = stumps if stages is None else stumps[:stages]
    for feature_index, threshold, left_value, right_value in selected_stumps:
        raw_score += LEARNING_RATE * np.where(
            x[:, feature_index] <= threshold,
            left_value,
            right_value,
        )
    return sigmoid(raw_score)


def summarize_selection(
    annualized: np.ndarray,
    returns: np.ndarray,
    actual_low: np.ndarray,
    rejected: np.ndarray,
    selection: str,
) -> dict[str, float | int | str]:
    kept = ~rejected
    metrics = classification_metrics(actual_low, rejected)
    return {
        "model": MODEL_NAME,
        "selection": selection,
        "events": int(len(annualized)),
        "rejected_events": int(rejected.sum()),
        "retained_events": int(kept.sum()),
        "rejection_rate": float(rejected.mean()),
        "low_cagr_rate_before": float(actual_low.mean()),
        "low_cagr_rate_after": float(actual_low[kept].mean()),
        "low_cagr_rejection_precision": metrics["precision"],
        "low_cagr_rejection_recall": metrics["recall"],
        "f0_5": metrics["f0_5"],
        "all_mean_cagr_pct": float(np.mean(annualized)),
        "retained_mean_cagr_pct": float(np.mean(annualized[kept])),
        "rejected_mean_cagr_pct": float(np.mean(annualized[rejected])),
        "all_median_cagr_pct": float(np.median(annualized)),
        "retained_median_cagr_pct": float(np.median(annualized[kept])),
        "rejected_median_cagr_pct": float(np.median(annualized[rejected])),
        "all_mean_current_return_pct": float(np.mean(returns)),
        "retained_mean_current_return_pct": float(np.mean(returns[kept])),
        "rejected_mean_current_return_pct": float(np.mean(returns[rejected])),
        "retained_loss_rate": float(np.mean(returns[kept] < 0)),
        "rejected_loss_rate": float(np.mean(returns[rejected] < 0)),
    }


def cluster_bootstrap(predictions: pd.DataFrame, iterations: int = 5000) -> pd.DataFrame:
    rng = np.random.default_rng(20260713)
    groups = [group.index.to_numpy() for _, group in predictions.groupby("銘柄", dropna=False)]
    samples: dict[str, list[float]] = {
        "low_cagr_precision_lift": [],
        "retained_mean_cagr_gain_pctpt": [],
        "retained_median_cagr_gain_pctpt": [],
        "loss_rate_reduction": [],
    }
    for _ in range(iterations):
        selected_groups = rng.integers(0, len(groups), size=len(groups))
        indexes = np.concatenate([groups[index] for index in selected_groups])
        sample = predictions.loc[indexes]
        rejected = sample["LAR1_decision"].eq("除外候補")
        kept = ~rejected
        if not rejected.any() or not kept.any():
            continue
        low = sample["actual_low_cagr_within_year"].astype(bool)
        cagr = sample["actual_current_cagr_pct"].astype(float)
        loss = sample["actual_current_return_pct"].astype(float) < 0
        samples["low_cagr_precision_lift"].append(float(low[rejected].mean() - low.mean()))
        samples["retained_mean_cagr_gain_pctpt"].append(float(cagr[kept].mean() - cagr.mean()))
        samples["retained_median_cagr_gain_pctpt"].append(float(cagr[kept].median() - cagr.median()))
        samples["loss_rate_reduction"].append(float(loss.mean() - loss[kept].mean()))
    rows = []
    for metric, values in samples.items():
        array = np.asarray(values, dtype=float)
        rows.append(
            {
                "model": MODEL_NAME,
                "metric": metric,
                "bootstrap_iterations": len(array),
                "estimate_mean": float(np.mean(array)),
                "ci_2_5": float(np.quantile(array, 0.025)),
                "ci_97_5": float(np.quantile(array, 0.975)),
                "probability_positive": float(np.mean(array > 0)),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    years = dates.dt.year
    annualized = numeric(frame["年利換算"])
    returns = numeric(frame["現在まで保有リターン"])

    # The target is relative to events from the same signal year. This prevents
    # holding-period length and broad market regime from becoming the label.
    within_year_cagr_rank = annualized.groupby(years).rank(method="average", pct=True)
    low_label = (within_year_cagr_rank <= LOW_CAGR_FRACTION).astype(float)
    valid = dates.notna() & annualized.notna() & returns.notna() & within_year_cagr_rank.notna()
    train_mask = valid & (years <= TRAIN_END_YEAR)
    validation_mask = valid & years.isin(VALID_YEARS)
    test_mask = valid & years.isin(TEST_YEARS)

    candidates = [
        column
        for column in feature_columns(frame)
        if not any(token in column for token in EXCLUDE_TOKENS)
        and not any(token in column for token in BLOCKED_FEATURE_TOKENS)
        and not any(token in column for token in ("現在", "将来", "候補後", "1年", "2年", "3年"))
    ]
    x_all = frame[candidates].apply(numeric)
    selected = select_features(x_all, low_label, train_mask)
    if not selected:
        raise RuntimeError("LAR-1で利用できる財務特徴量がありません")

    train_x_raw = x_all.loc[train_mask, selected].to_numpy(dtype=float)
    validation_x_raw = x_all.loc[validation_mask, selected].to_numpy(dtype=float)
    prep = fit_preprocessor(train_x_raw)
    train_x = transform(train_x_raw, prep)
    validation_x = transform(validation_x_raw, prep)
    train_y = low_label[train_mask].to_numpy(dtype=float)
    validation_y = low_label[validation_mask].to_numpy(dtype=bool)

    tuning_rows: list[dict[str, float]] = []
    best: tuple[float, float, float, float] | None = None
    intercept, all_stumps = fit_boosted_stumps(train_x, train_y, max(BOOSTING_STAGES))
    for stages in BOOSTING_STAGES:
        validation_score = predict_boosted_stumps(validation_x, intercept, all_stumps, stages)
        for fraction in REJECTION_FRACTIONS:
            rejected = rejection_mask(validation_score, fraction)
            metrics = classification_metrics(validation_y, rejected)
            tuning_rows.append(
                {
                    "model": MODEL_NAME,
                    "boosting_stages": stages,
                    "rejection_fraction": fraction,
                    "precision": metrics["precision"],
                    "recall": metrics["recall"],
                    "f0_5": metrics["f0_5"],
                }
            )
            candidate = (metrics["f0_5"], metrics["precision"], metrics["recall"], -fraction)
            if best is None or candidate > best:
                best = candidate
                best_stages = int(stages)
                best_fraction = float(fraction)

    fit_mask = train_mask | validation_mask
    fit_x_raw = x_all.loc[fit_mask, selected].to_numpy(dtype=float)
    test_x_raw = x_all.loc[test_mask, selected].to_numpy(dtype=float)
    prep = fit_preprocessor(fit_x_raw)
    fit_x = transform(fit_x_raw, prep)
    test_x = transform(test_x_raw, prep)
    fit_y = low_label[fit_mask].to_numpy(dtype=float)
    intercept, stumps = fit_boosted_stumps(fit_x, fit_y, best_stages)
    test_risk = predict_boosted_stumps(test_x, intercept, stumps)
    rejected = rejection_mask(test_risk, best_fraction)
    test_low = low_label[test_mask].to_numpy(dtype=bool)
    test_annualized = annualized[test_mask].to_numpy(dtype=float)
    test_returns = returns[test_mask].to_numpy(dtype=float)

    predictions = frame.loc[test_mask, ["銘柄", "企業名", "底打ち候補日"]].copy()
    predictions.insert(0, "row_index", frame.index[test_mask].astype(int))
    predictions["model"] = MODEL_NAME
    predictions["LAR1_low_cagr_risk_score"] = test_risk
    predictions["risk_percentile"] = pd.Series(test_risk).rank(pct=True).to_numpy()
    predictions["LAR1_decision"] = np.where(rejected, "除外候補", "維持候補")
    predictions["actual_current_cagr_pct"] = test_annualized
    predictions["actual_current_return_pct"] = test_returns
    predictions["actual_low_cagr_within_year"] = test_low
    bootstrap = cluster_bootstrap(predictions)

    summary = pd.DataFrame(
        [
            summarize_selection(
                test_annualized,
                test_returns,
                test_low,
                rejected,
                "2023-2024 out-of-time test",
            )
        ]
    )
    summary.insert(2, "description", MODEL_DESCRIPTION)
    summary["low_cagr_definition"] = f"同じ底検知年の年利下位{LOW_CAGR_FRACTION:.0%}"
    summary["train_period"] = f"～{TRAIN_END_YEAR}"
    summary["validation_period"] = f"{VALID_YEARS[0]}～{VALID_YEARS[-1]}"
    summary["test_period"] = f"{TEST_YEARS[0]}～{TEST_YEARS[-1]}"
    summary["selected_features"] = len(selected)
    summary["boosting_stages"] = best_stages
    summary["learning_rate"] = LEARNING_RATE
    summary["selected_rejection_fraction"] = best_fraction
    summary["caution"] = (
        "特徴量は時点情報のみ。目的変数は2026-07時点までの年利換算で、"
        "過去時点では未確定だったため厳密な歴史的売買再現ではない。"
    )

    importance = pd.Series(0.0, index=selected)
    for feature_index, _, left_value, right_value in stumps:
        importance.iloc[feature_index] += abs(left_value - right_value)
    coefficients = importance.rename("split_importance").rename_axis("feature").reset_index()
    coefficients.insert(0, "model", MODEL_NAME)
    coefficients = coefficients.sort_values("split_importance", ascending=False)

    summary.to_csv(OUTPUT_DIR / "lar1_model_summary.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(OUTPUT_DIR / "lar1_test_predictions.csv", index=False, encoding="utf-8-sig")
    coefficients.to_csv(OUTPUT_DIR / "lar1_coefficients.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tuning_rows).to_csv(OUTPUT_DIR / "lar1_threshold_tuning.csv", index=False, encoding="utf-8-sig")
    bootstrap.to_csv(OUTPUT_DIR / "lar1_bootstrap_validation.csv", index=False, encoding="utf-8-sig")

    np.savez_compressed(
        OUTPUT_DIR / "lar1_evaluation_model_artifact.npz",
        model_name=np.array([MODEL_NAME]),
        features=np.asarray(selected, dtype=str),
        intercept=np.array([intercept]),
        stumps=np.asarray(stumps, dtype=float),
        lower=prep["lower"],
        upper=prep["upper"],
        median=prep["median"],
        mean=prep["mean"],
        std=prep["std"],
        rejection_fraction=np.array([best_fraction]),
    )

    # Deployment artifact: train on every completed event through 2024, then
    # score all rows. Its historical scores are in-sample and are not evidence.
    deployment_mask = valid & (years <= TEST_YEARS[-1])
    deployment_x_raw = x_all.loc[deployment_mask, selected].to_numpy(dtype=float)
    deployment_prep = fit_preprocessor(deployment_x_raw)
    deployment_x = transform(deployment_x_raw, deployment_prep)
    deployment_intercept, deployment_stumps = fit_boosted_stumps(
        deployment_x,
        low_label[deployment_mask].to_numpy(dtype=float),
        best_stages,
    )
    all_x = transform(x_all[selected].to_numpy(dtype=float), deployment_prep)
    all_risk = predict_boosted_stumps(
        all_x,
        deployment_intercept,
        deployment_stumps,
    )
    deployment_scores = frame[["銘柄", "企業名", "底打ち候補日"]].copy()
    deployment_scores.insert(0, "row_index", frame.index.astype(int))
    deployment_scores["model"] = MODEL_NAME
    deployment_scores["LAR1_low_cagr_risk_score"] = all_risk
    deployment_scores["risk_percentile"] = pd.Series(all_risk).rank(pct=True).to_numpy()
    deployment_scores["LAR1_decision"] = np.where(
        deployment_scores["risk_percentile"] >= 1.0 - best_fraction,
        "除外候補",
        "維持候補",
    )
    deployment_scores.to_csv(OUTPUT_DIR / "lar1_deployment_scores.csv", index=False, encoding="utf-8-sig")
    np.savez_compressed(
        OUTPUT_DIR / "lar1_deployment_model_artifact.npz",
        model_name=np.array([MODEL_NAME]),
        features=np.asarray(selected, dtype=str),
        intercept=np.array([deployment_intercept]),
        stumps=np.asarray(deployment_stumps, dtype=float),
        lower=deployment_prep["lower"],
        upper=deployment_prep["upper"],
        median=deployment_prep["median"],
        mean=deployment_prep["mean"],
        std=deployment_prep["std"],
        rejection_fraction=np.array([best_fraction]),
    )
    pd.DataFrame({"model": MODEL_NAME, "feature": selected}).to_csv(
        OUTPUT_DIR / "lar1_feature_manifest.csv", index=False, encoding="utf-8-sig"
    )
    (OUTPUT_DIR / "lar1_metadata.json").write_text(
        json.dumps(
            {
                "model": MODEL_NAME,
                "description": MODEL_DESCRIPTION,
                "target": f"within-signal-year bottom {LOW_CAGR_FRACTION:.0%} current CAGR",
                "algorithm": "pure-numpy gradient boosted decision stumps",
                "selected_boosting_stages": best_stages,
                "learning_rate": LEARNING_RATE,
                "selected_rejection_fraction": best_fraction,
                "evaluation_train_through": TEST_YEARS[-1],
                "deployment_training_is_in_sample_for_historical_rows": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
