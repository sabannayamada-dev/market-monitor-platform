from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import (
    DATA_PATH,
    EXCLUDE_TOKENS,
    auc_score,
    feature_columns,
    numeric,
    ridge_fit_predict,
    standardize_train_test,
)


OUTPUT_DIR = Path("outputs/us_stock_research")
MINIMUM_TRAIN_ROWS = 250
MINIMUM_TEST_ROWS = 25
TOP_FEATURES = 30


def prepare_targets(frame: pd.DataFrame) -> dict[str, pd.Series]:
    upside = numeric(frame["1年内最大上昇率"])
    drawdown = numeric(frame["1年内最大下落率"])
    return {
        "1年内大化け50%": pd.Series(
            np.where(upside.notna(), (upside >= 50).astype(float), np.nan), index=frame.index
        ),
        "1年内深掘り35%": pd.Series(
            np.where(drawdown.notna(), (drawdown <= -35).astype(float), np.nan), index=frame.index
        ),
        "大化け深傷回避_上昇50%以上かつ深掘り35%未満": pd.Series(
            np.where(
                upside.notna() & drawdown.notna(),
                ((upside >= 50) & (drawdown > -35)).astype(float),
                np.nan,
            ),
            index=frame.index,
        ),
    }


def select_features(
    x: pd.DataFrame, y: pd.Series, train_mask: pd.Series, candidates: list[str]
) -> list[str]:
    scored: list[tuple[float, str]] = []
    for column in candidates:
        pair = pd.DataFrame({"x": x.loc[train_mask, column], "y": y[train_mask]}).dropna()
        if len(pair) < 100 or pair["x"].nunique() < 5 or pair["y"].nunique() < 2:
            continue
        auc = auc_score(pair["y"].to_numpy(dtype=int), pair["x"].to_numpy(dtype=float))
        if np.isfinite(auc):
            scored.append((abs(auc - 0.5), column))
    scored.sort(reverse=True)
    return [column for _, column in scored[:TOP_FEATURES]]


def main() -> None:
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    years = dates.dt.year
    candidates = [
        column
        for column in feature_columns(frame)
        if not any(token in column for token in EXCLUDE_TOKENS)
    ]
    x_all = frame[candidates].apply(numeric)
    targets = prepare_targets(frame)
    prediction_rows: list[dict] = []

    for target_name, y in targets.items():
        for test_year in range(2015, 2026):
            train_mask = dates.notna() & (years < test_year) & y.notna()
            test_mask = dates.notna() & (years == test_year) & y.notna()
            if train_mask.sum() < MINIMUM_TRAIN_ROWS or test_mask.sum() < MINIMUM_TEST_ROWS:
                continue
            selected = select_features(x_all, y, train_mask, candidates)
            if not selected:
                continue
            x_train = x_all.loc[train_mask, selected].to_numpy(dtype=float)
            x_test = x_all.loc[test_mask, selected].to_numpy(dtype=float)
            x_train, x_test = standardize_train_test(x_train, x_test)
            y_train = y[train_mask].to_numpy(dtype=float)
            y_test = y[test_mask].to_numpy(dtype=float)

            train_years = years[train_mask].to_numpy(dtype=int)
            inner_year = int(np.max(train_years))
            inner_train = train_years < inner_year
            inner_valid = train_years == inner_year
            if inner_train.sum() < 150 or inner_valid.sum() < 25:
                inner_train = np.arange(len(y_train)) % 5 != 0
                inner_valid = ~inner_train
            best_alpha, best_metric = 100.0, -np.inf
            for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0):
                _, validation_prediction = ridge_fit_predict(
                    x_train[inner_train], y_train[inner_train], x_train[inner_valid], alpha
                )
                validation_auc = auc_score(y_train[inner_valid], validation_prediction)
                metric = abs(validation_auc - 0.5) if np.isfinite(validation_auc) else -np.inf
                if metric > best_metric:
                    best_metric, best_alpha = metric, alpha
            _, prediction = ridge_fit_predict(x_train, y_train, x_test, best_alpha)
            for index, actual, predicted in zip(frame.index[test_mask], y_test, prediction):
                prediction_rows.append(
                    {
                        "目的指標": target_name,
                        "検証年": test_year,
                        "row_index": int(index),
                        "銘柄": frame.at[index, "銘柄"],
                        "実績": float(actual),
                        "予測値": float(predicted),
                        "学習件数": int(train_mask.sum()),
                        "選択特徴量数": len(selected),
                        "alpha": best_alpha,
                    }
                )

    predictions = pd.DataFrame(prediction_rows)
    annual_rows: list[dict] = []
    expanding_rows: list[dict] = []
    for target_name, target_frame in predictions.groupby("目的指標", sort=False):
        for test_year, group in target_frame.groupby("検証年"):
            actual = group["実績"].to_numpy(dtype=int)
            predicted = group["予測値"].to_numpy(dtype=float)
            annual_rows.append(
                {
                    "目的指標": target_name,
                    "検証年": int(test_year),
                    "イベント数": len(group),
                    "企業数": group["銘柄"].nunique(),
                    "陽性率": actual.mean(),
                    "AUC": auc_score(actual, predicted),
                }
            )
        minimum_year = int(target_frame["検証年"].min())
        maximum_year = int(target_frame["検証年"].max())
        for start_year in range(maximum_year, minimum_year - 1, -1):
            group = target_frame[target_frame["検証年"] >= start_year]
            actual = group["実績"].to_numpy(dtype=int)
            predicted = group["予測値"].to_numpy(dtype=float)
            low = predicted <= np.quantile(predicted, 0.2)
            high = predicted >= np.quantile(predicted, 0.8)
            expanding_rows.append(
                {
                    "目的指標": target_name,
                    "対象期間": f"現在～{start_year}年（開始年を遡及）",
                    "開始年": start_year,
                    "終了年": maximum_year,
                    "イベント数": len(group),
                    "企業数": group["銘柄"].nunique(),
                    "陽性率": actual.mean(),
                    "AUC": auc_score(actual, predicted),
                    "予測下位20%陽性率": actual[low].mean(),
                    "予測上位20%陽性率": actual[high].mean(),
                    "上位下位差": actual[high].mean() - actual[low].mean(),
                }
            )

    predictions.to_csv(
        OUTPUT_DIR / "backward_expanding_practical_target_predictions.csv",
        index=False,
        encoding="utf-8-sig",
    )
    annual = pd.DataFrame(annual_rows)
    annual.to_csv(
        OUTPUT_DIR / "backward_expanding_practical_target_annual.csv",
        index=False,
        encoding="utf-8-sig",
    )
    expanding = pd.DataFrame(expanding_rows)
    expanding.to_csv(
        OUTPUT_DIR / "backward_expanding_practical_target_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print("ANNUAL")
    print(annual.to_string(index=False))
    print("\nEXPANDING")
    print(expanding.to_string(index=False))


if __name__ == "__main__":
    main()
