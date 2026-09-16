from __future__ import annotations

from pathlib import Path
import json
import math
import re
from typing import Iterable

import numpy as np
import pandas as pd


INPUT_CSV = Path(
    r"C:/Users/saban/Documents/Codex/2026-07-03/gem/outputs/stock_bottom_financial_enrichment/stock_bottom_with_edinet_financials_with_price_metrics.csv"
)
OUTPUT_DIR = Path(
    r"C:/Users/saban/Documents/Codex/2026-07-03/gem/outputs/big_winner_classifier"
)


def to_num(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    text = text.replace({"": np.nan, "nan": np.nan, "None": np.nan, "-": np.nan})
    text = text.str.replace(",", "", regex=False)
    text = text.str.replace("%", "", regex=False)
    text = text.str.replace("％", "", regex=False)
    text = text.str.replace("円", "", regex=False)
    text = text.str.replace("倍", "", regex=False)
    text = text.str.extract(r"([-+]?\d+(?:\.\d+)?)", expand=False)
    return pd.to_numeric(text, errors="coerce")


def add_numeric(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col + "_num"] = to_num(out[col])
    return out


def safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    b = b.replace(0, np.nan)
    return a / b


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    # Scale-heavy values are more model-friendly on log1p.
    for col in [
        "底検知時_平均年間給与_num",
        "底検知時_従業員数_num",
        "底検知時_売上高_num",
        "底検知時_営業利益_num",
        "底検知時_経常利益_num",
        "底検知時_総資産_num",
        "底検知時_純資産_num",
        "底検知時_負債_num",
        "底検知時_現金等_num",
        "底検知時_研究開発費_num",
        "底検知時_設備投資額_num",
        "判定前最高値_num",
        "底打ち候補日価格_num",
    ]:
        if col in out.columns:
            out["log_" + col.replace("_num", "")] = np.sign(out[col]) * np.log1p(out[col].abs())

    if {"底検知時_営業利益_num", "底検知時_売上高_num"} <= set(out.columns):
        out["派生_営業利益率"] = safe_div(out["底検知時_営業利益_num"], out["底検知時_売上高_num"])
    if {"底検知時_経常利益_num", "底検知時_売上高_num"} <= set(out.columns):
        out["派生_経常利益率"] = safe_div(out["底検知時_経常利益_num"], out["底検知時_売上高_num"])
    if {"底検知時_現金等_num", "底検知時_総資産_num"} <= set(out.columns):
        out["派生_現金総資産比率"] = safe_div(out["底検知時_現金等_num"], out["底検知時_総資産_num"])
    if {"底検知時_負債_num", "底検知時_総資産_num"} <= set(out.columns):
        out["派生_負債総資産比率"] = safe_div(out["底検知時_負債_num"], out["底検知時_総資産_num"])
    if {"底検知時_研究開発費_num", "底検知時_売上高_num"} <= set(out.columns):
        out["派生_研究開発費売上比率"] = safe_div(out["底検知時_研究開発費_num"], out["底検知時_売上高_num"])
    if {"底検知時_設備投資額_num", "底検知時_売上高_num"} <= set(out.columns):
        out["派生_設備投資売上比率"] = safe_div(out["底検知時_設備投資額_num"], out["底検知時_売上高_num"])
    if {"底検知時_平均勤続年数_num", "底検知時_平均年齢_num"} <= set(out.columns):
        out["派生_勤続安定度"] = safe_div(out["底検知時_平均勤続年数_num"], out["底検知時_平均年齢_num"] - 22)
    if {"底検知時_平均年間給与_num", "底検知時_従業員数_num"} <= set(out.columns):
        out["派生_推定人件費"] = out["底検知時_平均年間給与_num"] * out["底検知時_従業員数_num"]
        if "底検知時_売上高_num" in out.columns:
            out["派生_推定人件費売上比率"] = safe_div(out["派生_推定人件費"], out["底検知時_売上高_num"])
    return out


def auc_score(y_true: np.ndarray, score: np.ndarray) -> float:
    y_true = np.asarray(y_true).astype(int)
    score = np.asarray(score).astype(float)
    pos = y_true == 1
    neg = y_true == 0
    if pos.sum() == 0 or neg.sum() == 0:
        return np.nan
    order = np.argsort(score)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(score) + 1)
    # Tie handling by average rank.
    sorted_score = score[order]
    i = 0
    while i < len(score):
        j = i + 1
        while j < len(score) and sorted_score[j] == sorted_score[i]:
            j += 1
        if j - i > 1:
            ranks[order[i:j]] = (i + 1 + j) / 2
        i = j
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * neg.sum()))


def average_precision(y_true: np.ndarray, score: np.ndarray) -> float:
    y_true = np.asarray(y_true).astype(int)
    order = np.argsort(-np.asarray(score).astype(float))
    y_sorted = y_true[order]
    positives = y_sorted.sum()
    if positives == 0:
        return np.nan
    cum_pos = np.cumsum(y_sorted)
    precision = cum_pos / (np.arange(len(y_sorted)) + 1)
    return float((precision * y_sorted).sum() / positives)


def prepare_matrix(X_train: pd.DataFrame, X_test: pd.DataFrame):
    med = X_train.median(numeric_only=True).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    xtr = X_train.replace([np.inf, -np.inf], np.nan).fillna(med).to_numpy(float)
    xte = X_test.replace([np.inf, -np.inf], np.nan).fillna(med).to_numpy(float)
    mean = xtr.mean(axis=0)
    std = xtr.std(axis=0)
    std[std == 0] = 1.0
    return (xtr - mean) / std, (xte - mean) / std


def sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -40, 40)
    return 1.0 / (1.0 + np.exp(-z))


def fit_logistic_l2(X: np.ndarray, y: np.ndarray, l2: float = 1.0, lr: float = 0.05, steps: int = 5000):
    Xb = np.column_stack([np.ones(len(X)), X])
    beta = np.zeros(Xb.shape[1])
    y = y.astype(float)
    pos = max(y.sum(), 1.0)
    neg = max(len(y) - y.sum(), 1.0)
    weights = np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))
    for _ in range(steps):
        p = sigmoid(Xb @ beta)
        grad = Xb.T @ ((p - y) * weights) / len(y)
        grad[1:] += l2 * beta[1:] / len(y)
        beta -= lr * grad
    return beta


def fit_ridge_score(X: np.ndarray, y: np.ndarray, alpha: float = 1.0):
    Xb = np.column_stack([np.ones(len(X)), X])
    y_centered = y.astype(float)
    eye = np.eye(Xb.shape[1])
    eye[0, 0] = 0.0
    return np.linalg.pinv(Xb.T @ Xb + alpha * eye) @ Xb.T @ y_centered


def evaluate_scores(name: str, score: np.ndarray, y_test: np.ndarray):
    auc = auc_score(y_test, score)
    ap = average_precision(y_test, score)
    baseline = float(np.mean(y_test))
    order = np.argsort(-score)
    metrics = {
        "model": name,
        "test_count": int(len(y_test)),
        "test_positive": int(np.sum(y_test)),
        "baseline_positive_rate": baseline,
        "auc": float(auc),
        "average_precision": float(ap),
    }
    for frac in [0.1, 0.2, 0.3]:
        k = max(1, int(math.ceil(len(y_test) * frac)))
        precision = float(np.mean(y_test[order[:k]]))
        metrics[f"precision_top_{int(frac*100)}pct"] = precision
        metrics[f"lift_top_{int(frac*100)}pct"] = precision / baseline if baseline else np.nan

    return metrics


def run_experiment(df: pd.DataFrame, name: str, target_col: str, feature_cols: list[str]):
    work = df.copy()
    work = work[work[target_col].notna()].copy()
    work["target"] = work[target_col].astype(int)
    # Time split: train on older candidates, test on newer candidates.
    train_mask = work["bottom_date"] <= pd.Timestamp("2022-12-31")
    test_mask = work["bottom_date"] >= pd.Timestamp("2023-01-01")
    work = work[train_mask | test_mask].copy()
    train_mask = work["bottom_date"] <= pd.Timestamp("2022-12-31")
    test_mask = work["bottom_date"] >= pd.Timestamp("2023-01-01")
    if train_mask.sum() < 50 or test_mask.sum() < 30 or work.loc[test_mask, "target"].nunique() < 2:
        return [], {}, pd.DataFrame()

    X_train = work.loc[train_mask, feature_cols].replace([np.inf, -np.inf], np.nan)
    X_test = work.loc[test_mask, feature_cols].replace([np.inf, -np.inf], np.nan)
    y_train = work.loc[train_mask, "target"].to_numpy()
    y_test = work.loc[test_mask, "target"].to_numpy()

    metrics = []
    importances = {}
    scored_rows = work.loc[test_mask, ["銘柄", "名称", "底打ち候補日", "現在まで保有リターン", "年利換算", "target"]].copy()
    xtr, xte = prepare_matrix(X_train, X_test)

    beta = fit_logistic_l2(xtr, y_train, l2=2.0, lr=0.05, steps=4000)
    logistic_score = sigmoid(np.column_stack([np.ones(len(xte)), xte]) @ beta)
    metrics.append(evaluate_scores(f"{name}:logistic_l2_numpy", logistic_score, y_test))
    imp = pd.DataFrame({"feature": feature_cols, "importance": beta[1:]})
    imp["abs_importance"] = imp["importance"].abs()
    importances["logistic_l2_numpy"] = imp.sort_values("abs_importance", ascending=False)
    scored_rows["logistic_l2_score"] = logistic_score

    rb = fit_ridge_score(xtr, y_train, alpha=5.0)
    ridge_score = np.column_stack([np.ones(len(xte)), xte]) @ rb
    metrics.append(evaluate_scores(f"{name}:ridge_linear_score", ridge_score, y_test))
    imp = pd.DataFrame({"feature": feature_cols, "importance": rb[1:]})
    imp["abs_importance"] = imp["importance"].abs()
    importances["ridge_linear_score"] = imp.sort_values("abs_importance", ascending=False)
    scored_rows["ridge_linear_score"] = ridge_score
    return metrics, importances, scored_rows.sort_values("logistic_l2_score", ascending=False)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(INPUT_CSV, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    numeric_cols = [
        "現在まで保有リターン",
        "年利換算",
        "高値からの下落率",
        "底位置指数",
        "経過日数",
        "判定前最高値",
        "底打ち候補日価格",
        "底検知時_平均年間給与",
        "底検知時_平均年齢",
        "底検知時_平均勤続年数",
        "底検知時_従業員数",
        "底検知時_売上高",
        "底検知時_営業利益",
        "底検知時_経常利益",
        "底検知時_当期利益",
        "底検知時_総資産",
        "底検知時_純資産",
        "底検知時_負債",
        "底検知時_自己資本比率",
        "底検知時_ROE",
        "底検知時_営業CF",
        "底検知時_投資CF",
        "底検知時_財務CF",
        "底検知時_現金等",
        "底検知時_研究開発費",
        "底検知時_設備投資額",
    ]
    df = add_numeric(df, numeric_cols)
    df = add_derived_features(df)
    df["bottom_date"] = pd.to_datetime(df["底打ち候補日"], errors="coerce")
    df = df[df["bottom_date"] >= pd.Timestamp("2016-01-01")].copy()
    df = df[df["現在まで保有リターン_num"].notna() & df["年利換算_num"].notna()].copy()

    # Define winners inside the 2016+ population. Two definitions are tested.
    df["target_return_top10"] = (
        df["現在まで保有リターン_num"] >= df["現在まで保有リターン_num"].quantile(0.90)
    ).astype(int)
    df["target_annualized_top10"] = (
        df["年利換算_num"] >= df["年利換算_num"].quantile(0.90)
    ).astype(int)

    technical_features = [
        "高値からの下落率_num",
        "底位置指数_num",
        "経過日数_num",
        "log_判定前最高値",
        "log_底打ち候補日価格",
    ]
    edinet_features = [
        c
        for c in df.columns
        if c.startswith("底検知時_") and c.endswith("_num")
    ] + [
        c
        for c in df.columns
        if c.startswith("log_底検知時_") or c.startswith("派生_")
    ]
    edinet_features = [c for c in edinet_features if c in df.columns]

    experiments = []
    all_importances = []
    for target in ["target_return_top10", "target_annualized_top10"]:
        metrics, importances, scored = run_experiment(
            df, f"technical_only:{target}", target, [c for c in technical_features if c in df.columns]
        )
        experiments.extend(metrics)
        for model_name, imp in importances.items():
            if not imp.empty:
                imp.insert(0, "experiment", f"technical_only:{target}:{model_name}")
                all_importances.append(imp.head(30))
        scored.to_csv(OUTPUT_DIR / f"scored_technical_only_{target}.csv", index=False, encoding="utf-8-sig")

        edinet_df = df[df["底検知時_EDINET状態"].astype(str).eq("xbrl_parsed")].copy()
        metrics, importances, scored = run_experiment(
            edinet_df,
            f"technical_plus_edinet:{target}",
            target,
            [c for c in technical_features + edinet_features if c in edinet_df.columns],
        )
        experiments.extend(metrics)
        for model_name, imp in importances.items():
            if not imp.empty:
                imp.insert(0, "experiment", f"technical_plus_edinet:{target}:{model_name}")
                all_importances.append(imp.head(30))
        scored.to_csv(
            OUTPUT_DIR / f"scored_technical_plus_edinet_{target}.csv",
            index=False,
            encoding="utf-8-sig",
        )

    summary = pd.DataFrame(experiments)
    summary.to_csv(OUTPUT_DIR / "big_winner_classifier_summary.csv", index=False, encoding="utf-8-sig")
    if all_importances:
        pd.concat(all_importances, ignore_index=True).to_csv(
            OUTPUT_DIR / "big_winner_feature_importance.csv",
            index=False,
            encoding="utf-8-sig",
        )
    metadata = {
        "input_csv": str(INPUT_CSV),
        "output_dir": str(OUTPUT_DIR),
        "population": "bottom_date >= 2016-01-01 and return/annualized return available",
        "split": "train <= 2022-12-31, test >= 2023-01-01",
        "leakage_policy": "future/current outcome columns excluded from features; only pre-candidate technical fields and EDINET-at-bottom fields used",
        "rows_2016_plus": int(len(df)),
        "technical_features": technical_features,
        "edinet_feature_count": len(edinet_features),
    }
    (OUTPUT_DIR / "big_winner_classifier_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print("output_dir:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
