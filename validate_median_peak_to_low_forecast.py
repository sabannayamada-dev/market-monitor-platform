from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import (
    DATA_PATH,
    correlation,
    feature_columns,
    numeric,
    rankdata,
    ridge_fit_predict,
    standardize_train_test,
)


OUTPUT_DIR = Path("outputs/us_stock_research")
TARGET = "判定前最高値→候補後最安値下落率"
TRAIN_END_YEAR = 2020
CALIBRATION_START_YEAR = 2021
CALIBRATION_END_YEAR = 2022
TEST_START_YEAR = 2023
TEST_END_YEAR = 2024
TOP_FEATURES = 30

EXCLUDE_TOKENS = (
    "リターン",
    "最大上昇",
    "最大下落",
    "候補後下落",
    "最高値→",
    "最安値",
    "成否",
    "底位置",
    "年利換算",
)


def select_features(
    x: pd.DataFrame, y: pd.Series, mask: pd.Series, candidates: list[str]
) -> list[str]:
    scored: list[tuple[float, str]] = []
    for column in candidates:
        pair = pd.DataFrame({"x": x.loc[mask, column], "y": y[mask]}).dropna()
        if len(pair) < 150 or pair["x"].nunique() < 5:
            continue
        rho = correlation(
            rankdata(pair["x"].to_numpy(dtype=float)),
            rankdata(pair["y"].to_numpy(dtype=float)),
        )
        if np.isfinite(rho):
            scored.append((abs(rho), column))
    scored.sort(reverse=True)
    return [column for _, column in scored[:TOP_FEATURES]]


def metrics(actual: np.ndarray, prediction: np.ndarray, prefix: str) -> dict:
    error = actual - prediction
    return {
        f"{prefix}_件数": len(actual),
        f"{prefix}_実際が予測以下の割合": float(np.mean(actual <= prediction)),
        f"{prefix}_実際が予測より深く下落した割合": float(np.mean(actual < prediction)),
        f"{prefix}_MAE_pctpt": float(np.mean(np.abs(error))),
        f"{prefix}_中央値絶対誤差_pctpt": float(np.median(np.abs(error))),
        f"{prefix}_平均誤差_実際マイナス予測": float(np.mean(error)),
        f"{prefix}_Pearson相関": correlation(actual, prediction),
        f"{prefix}_Spearman相関": correlation(rankdata(actual), rankdata(prediction)),
        f"{prefix}_50pctピンボール損失": float(np.mean(np.abs(error)) * 0.5),
    }


def main() -> None:
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    years = dates.dt.year
    y = numeric(frame[TARGET])
    candidates = [
        column
        for column in feature_columns(frame)
        if not any(token in column for token in EXCLUDE_TOKENS)
    ]
    x_all = frame[candidates].apply(numeric)
    train_mask = dates.notna() & years.le(TRAIN_END_YEAR) & y.notna()
    calibration_mask = (
        dates.notna()
        & years.between(CALIBRATION_START_YEAR, CALIBRATION_END_YEAR)
        & y.notna()
    )
    test_mask = dates.notna() & years.between(TEST_START_YEAR, TEST_END_YEAR) & y.notna()
    selected = select_features(x_all, y, train_mask, candidates)

    x_train = x_all.loc[train_mask, selected].to_numpy(dtype=float)
    x_calibration = x_all.loc[calibration_mask, selected].to_numpy(dtype=float)
    x_test = x_all.loc[test_mask, selected].to_numpy(dtype=float)
    x_train, x_combined = standardize_train_test(
        x_train, np.vstack([x_calibration, x_test])
    )
    x_calibration = x_combined[: len(x_calibration)]
    x_test = x_combined[len(x_calibration) :]
    y_train = y[train_mask].to_numpy(dtype=float)
    y_calibration = y[calibration_mask].to_numpy(dtype=float)
    y_test = y[test_mask].to_numpy(dtype=float)

    train_years = years[train_mask].to_numpy(dtype=int)
    inner_train = train_years < TRAIN_END_YEAR
    inner_valid = train_years == TRAIN_END_YEAR
    if inner_train.sum() < 200 or inner_valid.sum() < 30:
        inner_train = np.arange(len(y_train)) % 5 != 0
        inner_valid = ~inner_train
    best_alpha = 100.0
    best_mae = np.inf
    for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0):
        _, validation_prediction = ridge_fit_predict(
            x_train[inner_train], y_train[inner_train], x_train[inner_valid], alpha
        )
        mae = np.mean(np.abs(y_train[inner_valid] - validation_prediction))
        if mae < best_mae:
            best_mae, best_alpha = mae, alpha

    _, calibration_raw = ridge_fit_predict(
        x_train, y_train, x_calibration, best_alpha
    )
    _, test_raw = ridge_fit_predict(x_train, y_train, x_test, best_alpha)
    calibration_offset = float(np.median(y_calibration - calibration_raw))
    calibration_prediction = np.minimum(calibration_raw + calibration_offset, 0.0)
    test_prediction = np.minimum(test_raw + calibration_offset, 0.0)

    baseline_median = float(np.median(y_train))
    baseline_prediction = np.full_like(y_test, baseline_median)
    summary = {
        "学習期間": f"～{TRAIN_END_YEAR}",
        "中央値校正期間": f"{CALIBRATION_START_YEAR}～{CALIBRATION_END_YEAR}",
        "最終テスト期間": f"{TEST_START_YEAR}～{TEST_END_YEAR}",
        "学習件数": int(train_mask.sum()),
        "校正件数": int(calibration_mask.sum()),
        "テスト件数": int(test_mask.sum()),
        "選択特徴量数": len(selected),
        "alpha": best_alpha,
        "中央値校正オフセット_pctpt": calibration_offset,
        **metrics(y_calibration, calibration_prediction, "校正"),
        **metrics(y_test, test_prediction, "テスト"),
        "単純中央値予測_MAE_pctpt": float(np.mean(np.abs(y_test - baseline_prediction))),
        "モデルMAE改善率_pct": float(
            (1 - np.mean(np.abs(y_test - test_prediction)) / np.mean(np.abs(y_test - baseline_prediction)))
            * 100
        ),
    }

    test_indices = frame.index[test_mask]
    output = frame.loc[
        test_indices,
        ["銘柄", "企業名", "底打ち候補日", "判定前最高値", "候補後最安値", TARGET],
    ].copy()
    output.insert(0, "row_index", test_indices.to_numpy(dtype=int))
    output["予測_50pct下落率"] = test_prediction
    output["単純中央値予測下落率"] = baseline_median
    output["予測_50pct最安値"] = (
        numeric(output["判定前最高値"]) * (1 + output["予測_50pct下落率"] / 100)
    )
    output["実際が予測より深く下落"] = numeric(output[TARGET]) < output["予測_50pct下落率"]
    output["予測誤差_pctpt"] = numeric(output[TARGET]) - output["予測_50pct下落率"]
    output["絶対誤差_pctpt"] = output["予測誤差_pctpt"].abs()
    output["予測危険度五分位"] = pd.qcut(
        output["予測_50pct下落率"].rank(method="first"), 5, labels=[5, 4, 3, 2, 1]
    ).astype(int)

    residuals = y_calibration - calibration_raw
    candidate_prices = numeric(frame.loc[test_indices, "底打ち候補日価格"]).to_numpy()
    prior_highs = numeric(frame.loc[test_indices, "判定前最高値"]).to_numpy()
    actual_lows = numeric(frame.loc[test_indices, "候補後最安値"]).to_numpy()
    one_year_upside = numeric(frame.loc[test_indices, "1年内最大上昇率"]).to_numpy()
    execution_rows = []
    median_limit_prices = prior_highs * (1 + test_prediction / 100)
    for probability in (0.35, 0.40, 0.50, 0.60, 0.70, 0.80):
        offset = float(np.quantile(residuals, probability))
        drawdown_prediction = np.minimum(test_raw + offset, 0.0)
        limit_prices = prior_highs * (1 + drawdown_prediction / 100)
        valid = np.isfinite(limit_prices) & np.isfinite(candidate_prices) & np.isfinite(actual_lows)
        immediate = valid & (candidate_prices <= limit_prices)
        eventually_executes = valid & (actual_lows <= limit_prices)
        waits = valid & ~immediate
        missed = waits & ~eventually_executes
        big_winner = np.isfinite(one_year_upside) & (one_year_upside >= 50)
        execution_rows.append(
            {
                "目標約定確率": probability,
                "校正残差オフセット_pctpt": offset,
                "テスト実測約定率": eventually_executes[valid].mean(),
                "シグナル時即時購入率": immediate[valid].mean(),
                "指値待ち対象件数": int(waits.sum()),
                "指値待ち後約定率": eventually_executes[waits].mean() if waits.any() else np.nan,
                "未約定率": missed[valid].mean(),
                "未約定かつ1年内50%上昇率": (missed & big_winner).sum() / valid.sum(),
                "中央値指値からの価格上乗せ率平均": float(
                    np.nanmean(limit_prices[valid] / median_limit_prices[valid] - 1)
                ),
            }
        )
        probability_label = int(probability * 100)
        output[f"予測_{probability_label}pct下落率"] = drawdown_prediction
        output[f"予測_{probability_label}pct指値"] = limit_prices

    quantiles = (
        output.groupby("予測危険度五分位", observed=True)
        .agg(
            件数=(TARGET, "size"),
            予測下落率平均=("予測_50pct下落率", "mean"),
            実際下落率平均=(TARGET, "mean"),
            実際下落率中央値=(TARGET, "median"),
            絶対誤差中央値=("絶対誤差_pctpt", "median"),
            予測超過下落率=("実際が予測より深く下落", "mean"),
        )
        .reset_index()
        .sort_values("予測危険度五分位")
    )

    pd.DataFrame([summary]).to_csv(
        OUTPUT_DIR / "median_peak_to_low_forecast_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    output.to_csv(
        OUTPUT_DIR / "median_peak_to_low_forecast_predictions.csv",
        index=False,
        encoding="utf-8-sig",
    )
    quantiles.to_csv(
        OUTPUT_DIR / "median_peak_to_low_forecast_quantiles.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame({"特徴量": selected}).to_csv(
        OUTPUT_DIR / "median_peak_to_low_forecast_features.csv",
        index=False,
        encoding="utf-8-sig",
    )
    execution = pd.DataFrame(execution_rows)
    execution.to_csv(
        OUTPUT_DIR / "median_peak_to_low_execution_probability.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(pd.DataFrame([summary]).to_string(index=False))
    print("\nQUANTILES")
    print(quantiles.to_string(index=False))
    print("\nEXECUTION PROBABILITY")
    print(execution.to_string(index=False))


if __name__ == "__main__":
    main()
