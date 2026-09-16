from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import DATA_PATH, EXCLUDE_TOKENS, feature_columns, numeric


MODEL_NAME = "LHR-1"
MODEL_DESCRIPTION = "Long-Horizon Return Model v1 / 長期保有収益モデル"
OUTPUT_DIR = Path("outputs/us_stock_research")
TRAIN_END_YEAR = 2020
VALID_YEARS = (2021, 2022)
TEST_YEARS = (2023, 2024)
TOP_FEATURES = 30
ALPHAS = (0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0)
BLOCKED_FEATURE_TOKENS = (
    "CIK",
    "取得数",
    "取得状態",
    "状態",
    "採用根拠",
    "提出日",
    "アクセス番号",
    "taxonomy",
    "tag",
)


def corr(actual: np.ndarray, predicted: np.ndarray, rank: bool = False) -> float:
    pair = pd.DataFrame({"actual": actual, "predicted": predicted}).dropna()
    if len(pair) < 3 or pair.nunique().min() < 2:
        return np.nan
    if rank:
        pair = pair.rank(method="average")
    return float(pair.corr().iloc[0, 1])


def select_features(x: pd.DataFrame, target: pd.Series, mask: pd.Series) -> list[str]:
    scored: list[tuple[float, str, int]] = []
    for column in x.columns:
        pair = pd.DataFrame({"x": x.loc[mask, column], "y": target[mask]}).dropna()
        if len(pair) < 150 or pair["x"].nunique() < 5:
            continue
        score = pair.rank(method="average").corr().iloc[0, 1]
        if np.isfinite(score):
            scored.append((abs(float(score)), column, len(pair)))
    scored.sort(reverse=True)
    return [column for _, column, _ in scored[:TOP_FEATURES]]


def fit_preprocessor(x: np.ndarray) -> dict[str, np.ndarray]:
    lower = np.nanquantile(x, 0.01, axis=0)
    upper = np.nanquantile(x, 0.99, axis=0)
    lower = np.where(np.isfinite(lower), lower, 0.0)
    upper = np.where(np.isfinite(upper), upper, lower)
    clipped = np.clip(x, lower, upper)
    median = np.nanmedian(clipped, axis=0)
    median = np.where(np.isfinite(median), median, 0.0)
    filled = np.where(np.isfinite(clipped), clipped, median)
    mean = filled.mean(axis=0)
    std = filled.std(axis=0)
    std = np.where(std > 1e-9, std, 1.0)
    return {"lower": lower, "upper": upper, "median": median, "mean": mean, "std": std}


def transform(x: np.ndarray, prep: dict[str, np.ndarray]) -> np.ndarray:
    clipped = np.clip(x, prep["lower"], prep["upper"])
    filled = np.where(np.isfinite(clipped), clipped, prep["median"])
    return (filled - prep["mean"]) / prep["std"]


def fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    design = np.column_stack([np.ones(len(x)), x])
    penalty = np.eye(design.shape[1]) * alpha
    penalty[0, 0] = 0.0
    return np.linalg.pinv(design.T @ design + penalty) @ design.T @ y


def predict(x: np.ndarray, beta: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(x)), x]) @ beta


def raw_return_target(returns: np.ndarray, clip_bounds: tuple[float, float] | None = None):
    if clip_bounds is None:
        clip_bounds = tuple(np.nanquantile(returns, [0.01, 0.99]))
    raw = np.clip(returns, clip_bounds[0], clip_bounds[1]) / 100.0
    return raw, clip_bounds


def ensemble_score(
    raw_prediction: np.ndarray,
    rank_prediction: np.ndarray,
    raw_reference: np.ndarray,
    rank_reference: np.ndarray,
) -> np.ndarray:
    raw_std = max(float(np.std(raw_reference)), 1e-9)
    rank_std = max(float(np.std(rank_reference)), 1e-9)
    raw_z = (raw_prediction - float(np.mean(raw_reference))) / raw_std
    rank_z = (rank_prediction - float(np.mean(rank_reference))) / rank_std
    return 0.5 * raw_z + 0.5 * rank_z


def main() -> None:
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    years = dates.dt.year
    returns = numeric(frame["現在まで保有リターン"])
    annualized = numeric(frame["年利換算"])
    cohort_rank = returns.groupby(years).rank(method="average", pct=True)
    candidates = [
        column
        for column in feature_columns(frame)
        if not any(token in column for token in EXCLUDE_TOKENS)
        and not any(token in column for token in BLOCKED_FEATURE_TOKENS)
        and not any(token in column for token in ("現在", "将来", "候補後", "1年", "2年", "3年"))
    ]
    x_all = frame[candidates].apply(numeric)
    valid = dates.notna() & returns.notna()
    train_mask = valid & (years <= TRAIN_END_YEAR)
    validation_mask = valid & years.isin(VALID_YEARS)
    test_mask = valid & years.isin(TEST_YEARS)
    selected = select_features(x_all, cohort_rank, train_mask)
    if not selected:
        raise RuntimeError("LHR-1で利用できる財務特徴量がありません")

    train_x_raw = x_all.loc[train_mask, selected].to_numpy(dtype=float)
    validation_x_raw = x_all.loc[validation_mask, selected].to_numpy(dtype=float)
    test_x_raw = x_all.loc[test_mask, selected].to_numpy(dtype=float)
    prep = fit_preprocessor(train_x_raw)
    train_x = transform(train_x_raw, prep)
    validation_x = transform(validation_x_raw, prep)
    test_x = transform(test_x_raw, prep)
    train_return = returns[train_mask].to_numpy(dtype=float)
    train_rank_y = cohort_rank[train_mask].to_numpy(dtype=float)
    validation_return = returns[validation_mask].to_numpy(dtype=float)
    test_return = returns[test_mask].to_numpy(dtype=float)
    train_raw_y, train_clip = raw_return_target(train_return)

    tuning_rows = []
    best_alpha = None
    best_metric = -np.inf
    for alpha in ALPHAS:
        raw_beta = fit_ridge(train_x, train_raw_y, alpha)
        rank_beta = fit_ridge(train_x, train_rank_y, alpha)
        train_raw_prediction = predict(train_x, raw_beta)
        train_rank_prediction = predict(train_x, rank_beta)
        score = ensemble_score(
            predict(validation_x, raw_beta),
            predict(validation_x, rank_beta),
            train_raw_prediction,
            train_rank_prediction,
        )
        spearman = corr(validation_return, score, rank=True)
        top = score >= np.quantile(score, 0.8)
        top_mean = float(np.mean(validation_return[top]))
        tuning_rows.append(
            {"model": MODEL_NAME, "alpha": alpha, "validation_spearman": spearman, "top20_mean_return_pct": top_mean}
        )
        metric = spearman if np.isfinite(spearman) else -np.inf
        if metric > best_metric:
            best_metric = metric
            best_alpha = alpha

    fit_mask = train_mask | validation_mask
    fit_x_raw = x_all.loc[fit_mask, selected].to_numpy(dtype=float)
    prep = fit_preprocessor(fit_x_raw)
    fit_x = transform(fit_x_raw, prep)
    test_x = transform(test_x_raw, prep)
    fit_return = returns[fit_mask].to_numpy(dtype=float)
    fit_rank_y = cohort_rank[fit_mask].to_numpy(dtype=float)
    fit_raw_y, fit_clip = raw_return_target(fit_return)
    raw_beta = fit_ridge(fit_x, fit_raw_y, float(best_alpha))
    rank_beta = fit_ridge(fit_x, fit_rank_y, float(best_alpha))
    fit_raw_prediction = predict(fit_x, raw_beta)
    fit_rank_prediction = predict(fit_x, rank_beta)
    test_raw_prediction = predict(test_x, raw_beta)
    test_rank_prediction = predict(test_x, rank_beta)
    test_score = ensemble_score(
        test_raw_prediction,
        test_rank_prediction,
        fit_raw_prediction,
        fit_rank_prediction,
    )

    predictions = frame.loc[test_mask, ["銘柄", "企業名", "底打ち候補日"]].copy()
    predictions.insert(0, "row_index", frame.index[test_mask].astype(int))
    predictions["model"] = MODEL_NAME
    predictions["LHR1_score"] = test_score
    predictions["predicted_winsorized_return_pct"] = test_raw_prediction * 100
    predictions["predicted_within_year_return_rank"] = test_rank_prediction
    predictions["actual_current_return_pct"] = test_return
    predictions["actual_current_cagr_pct"] = annualized[test_mask].to_numpy(dtype=float)
    predictions["score_percentile"] = pd.Series(test_score).rank(pct=True).to_numpy()

    quantile_rows = []
    predictions["score_quintile"] = pd.qcut(
        predictions["LHR1_score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]
    ).astype(int)
    for quintile, group in predictions.groupby("score_quintile"):
        quantile_rows.append(
            {
                "model": MODEL_NAME,
                "score_quintile": int(quintile),
                "events": len(group),
                "mean_current_return_pct": group["actual_current_return_pct"].mean(),
                "median_current_return_pct": group["actual_current_return_pct"].median(),
                "mean_current_cagr_pct": group["actual_current_cagr_pct"].mean(),
                "gain_100pct_rate": (group["actual_current_return_pct"] >= 100).mean(),
                "loss_rate": (group["actual_current_return_pct"] < 0).mean(),
            }
        )

    coefficient_rows = []
    for feature, raw_coef, rank_coef in zip(selected, raw_beta[1:], rank_beta[1:]):
        coefficient_rows.append(
            {
                "model": MODEL_NAME,
                "feature": feature,
                "raw_return_coefficient": raw_coef,
                "cohort_rank_coefficient": rank_coef,
                "combined_abs_strength": abs(raw_coef) + abs(rank_coef),
            }
        )
    coefficients = pd.DataFrame(coefficient_rows).sort_values("combined_abs_strength", ascending=False)
    summary = pd.DataFrame(
        [
            {
                "model": MODEL_NAME,
                "description": MODEL_DESCRIPTION,
                "train_period": f"～{TRAIN_END_YEAR}",
                "validation_period": f"{VALID_YEARS[0]}～{VALID_YEARS[-1]}",
                "test_period": f"{TEST_YEARS[0]}～{TEST_YEARS[-1]}",
                "train_validation_events": int(fit_mask.sum()),
                "test_events": int(test_mask.sum()),
                "candidate_features": len(candidates),
                "selected_features": len(selected),
                "alpha": best_alpha,
                "test_pearson": corr(test_return, test_score),
                "test_spearman": corr(test_return, test_score, rank=True),
                "test_baseline_mean_return_pct": float(np.mean(test_return)),
                "test_baseline_median_return_pct": float(np.median(test_return)),
                "test_top20_mean_return_pct": predictions.loc[predictions["score_percentile"] >= 0.8, "actual_current_return_pct"].mean(),
                "test_top20_median_return_pct": predictions.loc[predictions["score_percentile"] >= 0.8, "actual_current_return_pct"].median(),
                "test_bottom20_mean_return_pct": predictions.loc[predictions["score_percentile"] <= 0.2, "actual_current_return_pct"].mean(),
                "target_clip_low_pct": fit_clip[0],
                "target_clip_high_pct": fit_clip[1],
                "caution": "特徴量は時点情報のみ。目的変数は2026-07時点までの累積収益で、過去時点に同ラベルは未確定だったため厳密な歴史的売買再現ではない。",
            }
        ]
    )

    summary.to_csv(OUTPUT_DIR / "lhr1_model_summary.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(OUTPUT_DIR / "lhr1_test_predictions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(quantile_rows).to_csv(OUTPUT_DIR / "lhr1_quintile_performance.csv", index=False, encoding="utf-8-sig")
    coefficients.to_csv(OUTPUT_DIR / "lhr1_coefficients.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tuning_rows).to_csv(OUTPUT_DIR / "lhr1_alpha_tuning.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame({"model": MODEL_NAME, "feature": selected}).to_csv(
        OUTPUT_DIR / "lhr1_feature_manifest.csv", index=False, encoding="utf-8-sig"
    )
    np.savez_compressed(
        OUTPUT_DIR / "lhr1_evaluation_model_artifact.npz",
        features=np.asarray(selected, dtype=str),
        raw_beta=raw_beta,
        rank_beta=rank_beta,
        **prep,
        raw_reference_mean=np.mean(fit_raw_prediction),
        raw_reference_std=np.std(fit_raw_prediction),
        rank_reference_mean=np.mean(fit_rank_prediction),
        rank_reference_std=np.std(fit_rank_prediction),
    )

    # Deployment artifact: use every sufficiently matured event through 2024.
    deployment_mask = valid & (years <= TEST_YEARS[-1])
    deployment_selected = select_features(x_all, cohort_rank, deployment_mask)
    deployment_x_raw = x_all.loc[deployment_mask, deployment_selected].to_numpy(dtype=float)
    deployment_prep = fit_preprocessor(deployment_x_raw)
    deployment_x = transform(deployment_x_raw, deployment_prep)
    deployment_returns = returns[deployment_mask].to_numpy(dtype=float)
    deployment_rank_y = cohort_rank[deployment_mask].to_numpy(dtype=float)
    deployment_raw_y, deployment_clip = raw_return_target(deployment_returns)
    deployment_raw_beta = fit_ridge(deployment_x, deployment_raw_y, float(best_alpha))
    deployment_rank_beta = fit_ridge(deployment_x, deployment_rank_y, float(best_alpha))
    deployment_raw_reference = predict(deployment_x, deployment_raw_beta)
    deployment_rank_reference = predict(deployment_x, deployment_rank_beta)
    all_x = transform(x_all[deployment_selected].to_numpy(dtype=float), deployment_prep)
    all_raw_prediction = predict(all_x, deployment_raw_beta)
    all_rank_prediction = predict(all_x, deployment_rank_beta)
    all_score = ensemble_score(
        all_raw_prediction,
        all_rank_prediction,
        deployment_raw_reference,
        deployment_rank_reference,
    )
    deployment_scores = frame[["銘柄", "企業名", "底打ち候補日"]].copy()
    deployment_scores.insert(0, "row_index", frame.index.astype(int))
    deployment_scores["model"] = MODEL_NAME
    deployment_scores["LHR1_score"] = all_score
    deployment_scores["score_percentile"] = pd.Series(all_score).rank(pct=True).to_numpy()
    deployment_scores["actual_current_return_pct_reference_only"] = returns
    deployment_scores.to_csv(
        OUTPUT_DIR / "lhr1_deployment_scores.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame({"model": MODEL_NAME, "feature": deployment_selected}).to_csv(
        OUTPUT_DIR / "lhr1_deployment_feature_manifest.csv", index=False, encoding="utf-8-sig"
    )
    np.savez_compressed(
        OUTPUT_DIR / "lhr1_deployment_model_artifact.npz",
        features=np.asarray(deployment_selected, dtype=str),
        raw_beta=deployment_raw_beta,
        rank_beta=deployment_rank_beta,
        **deployment_prep,
        raw_reference_mean=np.mean(deployment_raw_reference),
        raw_reference_std=np.std(deployment_raw_reference),
        rank_reference_mean=np.mean(deployment_rank_reference),
        rank_reference_std=np.std(deployment_rank_reference),
        target_clip_low=deployment_clip[0],
        target_clip_high=deployment_clip[1],
    )
    (OUTPUT_DIR / "lhr1_model_artifact.npz").unlink(missing_ok=True)
    metadata = {
        "model": MODEL_NAME,
        "description": MODEL_DESCRIPTION,
        "target": "現在まで保有した場合の累積リターン",
        "training_heads": ["1%-99% winsorized cumulative return", "within-signal-year cumulative-return percentile"],
        "feature_timing": "bottom-signal date or earlier only",
        "legacy_models_preserved": True,
        "evaluation_artifact": "lhr1_evaluation_model_artifact.npz",
        "deployment_artifact": "lhr1_deployment_model_artifact.npz",
        "deployment_training_period": "through 2024",
    }
    (OUTPUT_DIR / "lhr1_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print("\nQUINTILES")
    print(pd.DataFrame(quantile_rows).to_string(index=False))
    print("\nTOP FEATURES")
    print(coefficients.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
