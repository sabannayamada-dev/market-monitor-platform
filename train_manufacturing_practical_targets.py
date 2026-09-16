from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import (
    DATA_PATH,
    auc_score,
    feature_columns,
    numeric,
    ridge_fit_predict,
    standardize_train_test,
)


OUTPUT = Path("outputs/us_stock_research/manufacturing_only_model_summary.csv")


def main() -> None:
    frame = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    frame = frame[frame["SEC_SIC大分類"].eq("製造")].copy()
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    upside = numeric(frame["1年内最大上昇率"])
    drawdown = numeric(frame["1年内最大下落率"])
    targets = {
        "1年内大化け50%": pd.Series(
            np.where(upside.notna(), (upside >= 50).astype(float), np.nan), index=frame.index
        ),
        "1年内深掘り35%": pd.Series(
            np.where(drawdown.notna(), (drawdown <= -35).astype(float), np.nan), index=frame.index
        ),
        "大化け深傷回避": pd.Series(
            np.where(
                upside.notna() & drawdown.notna(),
                ((upside >= 50) & (drawdown > -35)).astype(float),
                np.nan,
            ),
            index=frame.index,
        ),
    }
    excluded = ("リターン", "最大上昇", "最大下落", "候補後下落", "最高値→", "最安値", "成否", "底位置", "年利")
    candidates = [c for c in feature_columns(frame) if not any(t in c for t in excluded)]
    x_all = frame[candidates].apply(numeric)
    rows = []
    for name, target in targets.items():
        train = (dates.dt.year < 2023) & target.notna()
        test = (dates.dt.year >= 2023) & target.notna()
        scored = []
        for column in candidates:
            pair = pd.DataFrame({"x": x_all.loc[train, column], "y": target[train]}).dropna()
            if len(pair) < 80 or pair["x"].nunique() < 5 or pair["y"].nunique() < 2:
                continue
            auc = auc_score(pair["y"].to_numpy(dtype=int), pair["x"].to_numpy())
            if np.isfinite(auc):
                scored.append((abs(auc - 0.5), column))
        selected = [column for _, column in sorted(scored, reverse=True)[:20]]
        x_train = x_all.loc[train, selected].to_numpy(dtype=float)
        x_test = x_all.loc[test, selected].to_numpy(dtype=float)
        x_train, x_test = standardize_train_test(x_train, x_test)
        y_train = target[train].to_numpy(dtype=float)
        y_test = target[test].to_numpy(dtype=float)
        train_years = dates.loc[train].dt.year.to_numpy()
        inner_train = train_years < 2021
        inner_valid = train_years >= 2021
        best = (-np.inf, 100.0)
        for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0):
            _, prediction = ridge_fit_predict(
                x_train[inner_train], y_train[inner_train], x_train[inner_valid], alpha
            )
            score = auc_score(y_train[inner_valid].astype(int), prediction)
            strength = abs(score - 0.5) if np.isfinite(score) else -np.inf
            if strength > best[0]:
                best = (strength, alpha)
        _, prediction = ridge_fit_predict(x_train, y_train, x_test, best[1])
        low, high = np.quantile(prediction, [0.2, 0.8])
        rows.append(
            {
                "目的指標": name,
                "学習件数": int(train.sum()),
                "テスト件数": int(test.sum()),
                "特徴量数": len(selected),
                "alpha": best[1],
                "AUC": auc_score(y_test.astype(int), prediction),
                "全体実現率": y_test.mean(),
                "予測下位20%実現率": y_test[prediction <= low].mean(),
                "予測上位20%実現率": y_test[prediction >= high].mean(),
            }
        )
    result = pd.DataFrame(rows)
    result.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
