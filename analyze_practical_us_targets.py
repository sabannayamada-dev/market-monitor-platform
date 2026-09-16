from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DATA_PATH = Path("outputs/us_stock_research/us_bottom_events_with_sec.csv")
OUTPUT_DIR = Path("outputs/us_stock_research")
CUTOFF = pd.Timestamp("2023-01-01")


TARGETS = {
    "1年内深掘り20%": ("binary", "1年内最大下落率", lambda s: s <= -20),
    "1年内深掘り35%": ("binary", "1年内最大下落率", lambda s: s <= -35),
    "1年後プラス": ("binary", "1年後リターン", lambda s: s > 0),
    "1年内大化け50%": ("binary", "1年内最大上昇率", lambda s: s >= 50),
    "良質反発_1年後20%以上かつ深掘り20%未満": (
        "binary",
        "良質反発フラグ",
        lambda s: s >= 1,
    ),
    "大化け深傷回避_上昇50%以上かつ深掘り35%未満": (
        "binary",
        "大化け深傷回避フラグ",
        lambda s: s >= 1,
    ),
    "1年内最大下落率": ("continuous", "1年内最大下落率", None),
    "1年後リターン": ("continuous", "1年後リターン", None),
    "1年内最大上昇率": ("continuous", "1年内最大上昇率", None),
}

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
    "目的変数",
)


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)


def rankdata(values: np.ndarray) -> np.ndarray:
    return pd.Series(values).rank(method="average").to_numpy(dtype=float)


def auc_score(y: np.ndarray, score: np.ndarray) -> float:
    y = np.asarray(y, dtype=int)
    score = np.asarray(score, dtype=float)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return np.nan
    ranks = rankdata(score)
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def standardize_train_test(
    x_train: np.ndarray, x_test: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    med = np.nanmedian(x_train, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    x_train = np.where(np.isfinite(x_train), x_train, med)
    x_test = np.where(np.isfinite(x_test), x_test, med)
    mean = x_train.mean(axis=0)
    std = x_train.std(axis=0)
    std = np.where(std > 1e-9, std, 1.0)
    return (x_train - mean) / std, (x_test - mean) / std


def ridge_fit_predict(
    x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray, alpha: float
) -> tuple[np.ndarray, np.ndarray]:
    x_train_i = np.column_stack([np.ones(len(x_train)), x_train])
    x_test_i = np.column_stack([np.ones(len(x_test)), x_test])
    penalty = np.eye(x_train_i.shape[1]) * alpha
    penalty[0, 0] = 0.0
    beta = np.linalg.pinv(x_train_i.T @ x_train_i + penalty) @ x_train_i.T @ y_train
    return x_train_i @ beta, x_test_i @ beta


def correlation(y: np.ndarray, pred: np.ndarray) -> float:
    if len(y) < 3 or np.nanstd(y) == 0 or np.nanstd(pred) == 0:
        return np.nan
    return float(np.corrcoef(y, pred)[0, 1])


def feature_columns(df: pd.DataFrame) -> list[str]:
    manifest_path = OUTPUT_DIR / "model_feature_manifest.csv"
    if manifest_path.exists():
        manifest = pd.read_csv(manifest_path, encoding="utf-8-sig")
        feature_name_col = "特徴量" if "特徴量" in manifest.columns else "feature"
        candidates = manifest[feature_name_col].dropna().astype(str).tolist()
    else:
        candidates = [c for c in df.columns if "SEC_" in c or "同業比較" in c]
    return [
        c
        for c in candidates
        if c in df.columns and not any(token in c for token in EXCLUDE_TOKENS)
    ]


def main() -> None:
    df = pd.read_csv(DATA_PATH, encoding="utf-8-sig", low_memory=False)
    one_year_return = numeric(df["1年後リターン"])
    one_year_upside = numeric(df["1年内最大上昇率"])
    one_year_drawdown = numeric(df["1年内最大下落率"])
    df["良質反発フラグ"] = np.where(
        one_year_return.notna() & one_year_drawdown.notna(),
        ((one_year_return >= 20) & (one_year_drawdown > -20)).astype(float),
        np.nan,
    )
    df["大化け深傷回避フラグ"] = np.where(
        one_year_upside.notna() & one_year_drawdown.notna(),
        ((one_year_upside >= 50) & (one_year_drawdown > -35)).astype(float),
        np.nan,
    )
    dates = pd.to_datetime(df["底打ち候補日"], errors="coerce")
    feature_cols = feature_columns(df)
    x_all = df[feature_cols].apply(numeric)

    definition_rows: list[dict] = []
    result_rows: list[dict] = []
    top_feature_rows: list[dict] = []
    quantile_rows: list[dict] = []
    prediction_rows: list[dict] = []

    for target_name, (target_type, source_col, transform) in TARGETS.items():
        raw = numeric(df[source_col])
        valid = raw.notna() & dates.notna()
        y = transform(raw).astype(float) if transform else raw
        train_mask = valid & (dates < CUTOFF)
        test_mask = valid & (dates >= CUTOFF)

        definition_rows.append(
            {
                "目的指標": target_name,
                "種類": target_type,
                "元列": source_col,
                "有効件数": int(valid.sum()),
                "充足率_pct": round(valid.mean() * 100, 2),
                "学習件数": int(train_mask.sum()),
                "検証件数": int(test_mask.sum()),
                "全体陽性率_pct": round(y[valid].mean() * 100, 2)
                if target_type == "binary"
                else np.nan,
                "学習陽性率_pct": round(y[train_mask].mean() * 100, 2)
                if target_type == "binary"
                else np.nan,
                "検証陽性率_pct": round(y[test_mask].mean() * 100, 2)
                if target_type == "binary"
                else np.nan,
                "中央値": round(raw[valid].median(), 3),
                "10pct点": round(raw[valid].quantile(0.1), 3),
                "90pct点": round(raw[valid].quantile(0.9), 3),
            }
        )
        if train_mask.sum() < 100 or test_mask.sum() < 50:
            continue

        # Feature screening is done on the training period only.
        screening: list[tuple[str, float, int]] = []
        for col in feature_cols:
            pair = pd.DataFrame({"x": x_all.loc[train_mask, col], "y": y[train_mask]}).dropna()
            if len(pair) < 150 or pair["x"].nunique() < 5:
                continue
            if target_type == "binary":
                score = auc_score(pair["y"].to_numpy(), pair["x"].to_numpy())
                strength = abs(score - 0.5) if np.isfinite(score) else 0.0
            else:
                score = correlation(
                    rankdata(pair["x"].to_numpy(dtype=float)),
                    rankdata(pair["y"].to_numpy(dtype=float)),
                )
                strength = abs(score) if np.isfinite(score) else 0.0
            screening.append((col, float(score), len(pair)))
        screening.sort(
            key=lambda row: abs(row[1] - 0.5) if target_type == "binary" else abs(row[1]),
            reverse=True,
        )
        selected = [row[0] for row in screening[:30]]
        for rank, (col, score, count) in enumerate(screening[:30], start=1):
            top_feature_rows.append(
                {
                    "目的指標": target_name,
                    "順位": rank,
                    "特徴量": col,
                    "学習期間単変量_AUCまたはSpearman": score,
                    "学習期間有効件数": count,
                }
            )

        x_train = x_all.loc[train_mask, selected].to_numpy(dtype=float)
        x_test = x_all.loc[test_mask, selected].to_numpy(dtype=float)
        x_train, x_test = standardize_train_test(x_train, x_test)
        y_train = y[train_mask].to_numpy(dtype=float)
        y_test = y[test_mask].to_numpy(dtype=float)

        best = None
        inner_cutoff = pd.Timestamp("2021-01-01")
        train_dates = dates[train_mask]
        inner_train = (train_dates < inner_cutoff).to_numpy()
        inner_valid = ~inner_train
        if inner_train.sum() < 100 or inner_valid.sum() < 50:
            inner_train = np.arange(len(y_train)) % 4 != 0
            inner_valid = ~inner_train
        for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0):
            _, pred_inner = ridge_fit_predict(
                x_train[inner_train], y_train[inner_train], x_train[inner_valid], alpha
            )
            metric = (
                abs(auc_score(y_train[inner_valid], pred_inner) - 0.5)
                if target_type == "binary"
                else abs(correlation(y_train[inner_valid], pred_inner))
            )
            if best is None or metric > best[0]:
                best = (metric, alpha)
        alpha = float(best[1])
        pred_train, pred_test = ridge_fit_predict(x_train, y_train, x_test, alpha)
        test_indices = df.index[test_mask]
        for row_index, actual, prediction in zip(test_indices, y_test, pred_test):
            prediction_rows.append(
                {
                    "目的指標": target_name,
                    "row_index": int(row_index),
                    "銘柄": df.at[row_index, "銘柄"],
                    "底打ち候補日": df.at[row_index, "底打ち候補日"],
                    "実績": float(actual),
                    "予測値": float(prediction),
                }
            )

        if target_type == "binary":
            train_metric = auc_score(y_train, pred_train)
            test_metric = auc_score(y_test, pred_test)
            metric_name = "AUC"
            baseline = max(y_test.mean(), 1 - y_test.mean())
            threshold = np.quantile(pred_train, 0.8)
            predicted_high = pred_test >= threshold
            precision = y_test[predicted_high].mean() if predicted_high.any() else np.nan
            result_rows.append(
                {
                    "目的指標": target_name,
                    "種類": target_type,
                    "選択特徴量数": len(selected),
                    "alpha": alpha,
                    "学習指標名": metric_name,
                    "学習指標": train_metric,
                    "検証指標": test_metric,
                    "検証基準正解率": baseline,
                    "検証上位20pct陽性率": precision,
                    "検証全体陽性率": y_test.mean(),
                }
            )
        else:
            train_metric = correlation(y_train, pred_train)
            test_metric = correlation(y_test, pred_test)
            metric_name = "Pearson相関"
            result_rows.append(
                {
                    "目的指標": target_name,
                    "種類": target_type,
                    "選択特徴量数": len(selected),
                    "alpha": alpha,
                    "学習指標名": metric_name,
                    "学習指標": train_metric,
                    "検証指標": test_metric,
                    "検証MAE": float(np.mean(np.abs(y_test - pred_test))),
                    "平均予測基準MAE": float(np.mean(np.abs(y_test - y_train.mean()))),
                }
            )

        q = pd.qcut(pd.Series(pred_test), 5, labels=False, duplicates="drop")
        for bucket in sorted(q.dropna().unique()):
            take = q.to_numpy() == bucket
            quantile_rows.append(
                {
                    "目的指標": target_name,
                    "予測分位": int(bucket) + 1,
                    "件数": int(take.sum()),
                    "実績平均": float(y_test[take].mean()),
                    "実績中央値": float(np.median(y_test[take])),
                    "予測平均": float(pred_test[take].mean()),
                }
            )

    pd.DataFrame(definition_rows).to_csv(
        OUTPUT_DIR / "practical_target_definition_summary.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(result_rows).to_csv(
        OUTPUT_DIR / "practical_target_time_split_results.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(top_feature_rows).to_csv(
        OUTPUT_DIR / "practical_target_top_features.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(quantile_rows).to_csv(
        OUTPUT_DIR / "practical_target_quantile_comparison.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(prediction_rows).to_csv(
        OUTPUT_DIR / "practical_target_test_predictions.csv", index=False, encoding="utf-8-sig"
    )
    print(pd.DataFrame(definition_rows).to_string(index=False))
    print("\nTIME SPLIT RESULTS")
    print(pd.DataFrame(result_rows).to_string(index=False))


if __name__ == "__main__":
    main()
