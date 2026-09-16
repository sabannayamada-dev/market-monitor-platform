from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "底検知データ 回帰最適化ツール"
DEFAULT_OUTPUT_DIR = Path("outputs") / "regularized_regression"


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            return pd.read_csv(path, encoding=enc, dtype=str, keep_default_na=False)
        except Exception as exc:  # pragma: no cover - surfaced to GUI
            last_error = exc
    raise RuntimeError(f"CSVを読み込めませんでした: {last_error}")


def parse_numeric_value(value: object) -> float:
    if value is None:
        return np.nan
    text = str(value).strip()
    if not text or text in {"-", "None", "nan", "NaN", "#DIV/0!", "#VALUE!", "#N/A"}:
        return np.nan

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    multiplier = 1.0
    if "%" in text:
        text = text.replace("%", "")
        multiplier *= 0.01
    if "兆" in text:
        multiplier *= 1_000_000_000_000
    if "億" in text:
        multiplier *= 100_000_000
    if "万" in text:
        multiplier *= 10_000

    text = (
        text.replace(",", "")
        .replace("円", "")
        .replace("歳", "")
        .replace("年", "")
        .replace("時間", "")
        .replace("h", "")
        .replace("H", "")
        .replace("点", "")
    )
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    if not match:
        return np.nan
    try:
        number = float(match.group(0)) * multiplier
    except ValueError:
        return np.nan
    return -number if negative else number


def numeric_series(series: pd.Series) -> pd.Series:
    return series.map(parse_numeric_value).astype(float)


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    n = numerator.astype(float)
    d = denominator.astype(float)
    out = n / d
    out = out.where(np.isfinite(out))
    out = out.where(d > 0)
    return out


def add_derived_features(data: pd.DataFrame) -> pd.DataFrame:
    df = data.copy()

    def get_numeric(column: str) -> pd.Series | None:
        if column not in df.columns:
            return None
        return numeric_series(df[column])

    avg_salary = get_numeric("底検知時_平均年間給与")
    avg_age = get_numeric("底検知時_平均年齢")
    avg_tenure = get_numeric("底検知時_平均勤続年数")
    employees = get_numeric("底検知時_従業員数")
    sales = get_numeric("底検知時_売上高")
    op_income = get_numeric("底検知時_営業利益")
    ordinary_income = get_numeric("底検知時_経常利益")
    net_assets = get_numeric("底検知時_純資産")
    cash = get_numeric("底検知時_現金等")
    rd = get_numeric("底検知時_研究開発費")
    capex = get_numeric("底検知時_設備投資額")

    if avg_salary is not None and employees is not None:
        labor_cost = avg_salary * employees
        labor_cost = labor_cost.where(np.isfinite(labor_cost))
        df["派生_推定人件費_平均年収×従業員数"] = labor_cost
        if sales is not None:
            df["派生_推定人件費売上比率"] = safe_divide(labor_cost, sales)

    if sales is not None and employees is not None:
        df["派生_一人当たり売上高"] = safe_divide(sales, employees)
    if op_income is not None and employees is not None:
        df["派生_一人当たり営業利益"] = safe_divide(op_income, employees)
    if ordinary_income is not None and employees is not None:
        df["派生_一人当たり経常利益"] = safe_divide(ordinary_income, employees)
    if cash is not None and employees is not None:
        df["派生_一人当たり現金等"] = safe_divide(cash, employees)
    if net_assets is not None and employees is not None:
        df["派生_一人当たり純資産"] = safe_divide(net_assets, employees)

    if avg_tenure is not None and avg_age is not None:
        denominator = (avg_age - 22).where((avg_age - 22) > 0)
        df["派生_勤続安定度_勤続年数÷社会人年数"] = safe_divide(avg_tenure, denominator)

    if rd is not None and sales is not None:
        df["派生_研究開発費売上比率"] = safe_divide(rd, sales)
    if capex is not None and sales is not None:
        df["派生_設備投資売上比率"] = safe_divide(capex, sales)

    return df


def pearson_corr(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 3:
        return float("nan")
    aa = a[mask]
    bb = b[mask]
    if np.std(aa) == 0 or np.std(bb) == 0:
        return float("nan")
    return float(np.corrcoef(aa, bb)[0, 1])


def r2_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if mask.sum() < 2:
        return float("nan")
    yy = y_true[mask]
    pp = y_pred[mask]
    denom = float(np.sum((yy - yy.mean()) ** 2))
    if denom == 0:
        return float("nan")
    return 1.0 - float(np.sum((yy - pp) ** 2)) / denom


def soft_threshold(value: float, threshold: float) -> float:
    if value > threshold:
        return value - threshold
    if value < -threshold:
        return value + threshold
    return 0.0


@dataclass
class StandardizedData:
    x: np.ndarray
    y: np.ndarray
    x_mean: np.ndarray
    x_std: np.ndarray
    y_mean: float
    y_std: float


def standardize(x: np.ndarray, y: np.ndarray) -> StandardizedData:
    x_mean = np.nanmean(x, axis=0)
    x_std = np.nanstd(x, axis=0)
    x_std[x_std == 0] = 1.0
    y_mean = float(np.nanmean(y))
    y_std = float(np.nanstd(y))
    if y_std == 0:
        y_std = 1.0
    return StandardizedData((x - x_mean) / x_std, (y - y_mean) / y_std, x_mean, x_std, y_mean, y_std)


def fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    n_features = x.shape[1]
    xtx = (x.T @ x) / max(len(y), 1)
    xty = (x.T @ y) / max(len(y), 1)
    return np.linalg.solve(xtx + alpha * np.eye(n_features), xty)


def fit_coordinate_descent(
    x: np.ndarray,
    y: np.ndarray,
    *,
    alpha: float,
    l1_ratio: float,
    max_iter: int = 5000,
    tol: float = 1e-7,
) -> np.ndarray:
    n_samples, n_features = x.shape
    beta = np.zeros(n_features, dtype=float)
    feature_norm = np.mean(x * x, axis=0)
    feature_norm[feature_norm == 0] = 1.0

    for _ in range(max_iter):
        old = beta.copy()
        prediction = x @ beta
        for j in range(n_features):
            residual = y - prediction + beta[j] * x[:, j]
            rho = float(np.dot(x[:, j], residual) / n_samples)
            new_beta = soft_threshold(rho, alpha * l1_ratio) / (
                feature_norm[j] + alpha * (1.0 - l1_ratio)
            )
            prediction += (new_beta - beta[j]) * x[:, j]
            beta[j] = new_beta
        if float(np.max(np.abs(beta - old))) < tol:
            break
    return beta


def fit_model(x: np.ndarray, y: np.ndarray, method: str, alpha: float, l1_ratio: float) -> np.ndarray:
    if method == "Ridge":
        return fit_ridge(x, y, alpha)
    if method == "Lasso":
        return fit_coordinate_descent(x, y, alpha=alpha, l1_ratio=1.0)
    if method == "ElasticNet":
        return fit_coordinate_descent(x, y, alpha=alpha, l1_ratio=l1_ratio)
    raise ValueError(f"未知の手法です: {method}")


def alpha_grid() -> list[float]:
    return [0.0, 0.0001, 0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0]


@dataclass
class ModelResult:
    method: str
    alpha: float
    l1_ratio: float
    coefficients: np.ndarray
    train_corr: float
    test_corr: float
    train_r2: float
    test_r2: float
    train_mae: float
    test_mae: float
    selected_by: float


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if mask.sum() == 0:
        return float("nan")
    return float(np.mean(np.abs(y_true[mask] - y_pred[mask])))


def mean_or_nan(series: pd.Series) -> float:
    values = numeric_series(series).replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return float("nan")
    return float(values.mean())


def median_or_nan(series: pd.Series) -> float:
    values = numeric_series(series).replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return float("nan")
    return float(values.median())


def quantile_bucket_labels(n_rows: int) -> list[str]:
    if n_rows <= 0:
        return []
    order = np.arange(n_rows)
    top_cut = max(1, int(math.ceil(n_rows * 0.2)))
    bottom_cut = max(1, int(math.floor(n_rows * 0.8)))
    labels = []
    for rank in order:
        if rank < top_cut:
            labels.append("EDINETスコア上位20%")
        elif rank >= bottom_cut:
            labels.append("EDINETスコア下位20%")
        else:
            labels.append("EDINETスコア中位60%")
    return labels


def build_bucket_validation(predictions: pd.DataFrame, methods: Iterable[str]) -> pd.DataFrame:
    metric_columns = {
        "候補後下落率": "候補後下落率",
        "高値→底下落率": "判定前最高値→候補後最安値下落率",
        "現在までリターン": "現在まで保有リターン",
        "年利": "年利換算",
    }
    verdict_column = "成否"
    split_order = ["all", "train", "test"]
    bucket_order = ["EDINETスコア上位20%", "EDINETスコア中位60%", "EDINETスコア下位20%"]
    rows: list[dict] = []

    for method in methods:
        pred_col = f"{method}_predicted_target"
        score_col = f"{method}_score"
        if pred_col not in predictions.columns:
            continue
        for split in split_order:
            if split == "all":
                base = predictions[predictions["_train_test"].isin(["train", "test"])].copy()
            else:
                base = predictions[predictions["_train_test"] == split].copy()
            base["_bucket_pred"] = numeric_series(base[pred_col])
            base = base.dropna(subset=["_bucket_pred"])
            if len(base) < 5:
                continue
            # 予測下落率は値が大きいほど「下落が浅い」ので、安全側を上位にする。
            base = base.sort_values("_bucket_pred", ascending=False).copy()
            base["_bucket"] = quantile_bucket_labels(len(base))
            for bucket in bucket_order:
                group = base[base["_bucket"] == bucket]
                if group.empty:
                    continue
                row: dict[str, object] = {
                    "method": method,
                    "split": split,
                    "bucket": bucket,
                    "count": int(len(group)),
                    "predicted_target_mean": mean_or_nan(group[pred_col]),
                    "predicted_target_median": median_or_nan(group[pred_col]),
                    "score_mean": mean_or_nan(group[score_col]) if score_col in group.columns else float("nan"),
                    "score_median": median_or_nan(group[score_col]) if score_col in group.columns else float("nan"),
                }
                for label, col in metric_columns.items():
                    if col in group.columns:
                        row[f"{label}_mean"] = mean_or_nan(group[col])
                        row[f"{label}_median"] = median_or_nan(group[col])
                    else:
                        row[f"{label}_mean"] = float("nan")
                        row[f"{label}_median"] = float("nan")
                if verdict_column in group.columns:
                    verdict = group[verdict_column].astype(str).str.strip()
                    denom = max(len(group), 1)
                    for label in ["成功", "許容", "早すぎ"]:
                        row[f"{label}_count"] = int((verdict == label).sum())
                        row[f"{label}_rate"] = float((verdict == label).sum() / denom * 100)
                rows.append(row)

    return pd.DataFrame(rows)


def train_test_indices(n_rows: int, test_ratio: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    idx = np.arange(n_rows)
    if test_ratio <= 0:
        return idx, np.array([], dtype=int)
    rng = np.random.default_rng(seed)
    shuffled = idx.copy()
    rng.shuffle(shuffled)
    test_size = max(1, int(round(n_rows * test_ratio)))
    test_idx = np.sort(shuffled[:test_size])
    train_idx = np.sort(shuffled[test_size:])
    if len(train_idx) < 3:
        return idx, np.array([], dtype=int)
    return train_idx, test_idx


def parse_date_series(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    text = text.replace({"": np.nan, "None": np.nan, "nan": np.nan, "NaN": np.nan})
    return pd.to_datetime(text, errors="coerce")


def split_indices(
    clean: pd.DataFrame,
    *,
    test_ratio: float,
    seed: int,
    split_mode: str,
    split_date_column: str | None,
    cutoff_year: int | None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    n_rows = len(clean)
    if split_mode == "random":
        train_idx, test_idx = train_test_indices(n_rows, test_ratio, seed)
        return train_idx, test_idx, {
            "split_mode": split_mode,
            "split_date_column": split_date_column or "",
            "cutoff_year": cutoff_year,
            "split_warning": "",
        }

    if not split_date_column or split_date_column not in clean.columns:
        raise ValueError("時系列分割には日付列を指定してください。")

    dates = parse_date_series(clean[split_date_column])
    valid_date_mask = dates.notna().to_numpy()
    if int(valid_date_mask.sum()) < 10:
        raise ValueError("時系列分割に使える日付が少なすぎます。日付列を確認してください。")

    valid_positions = np.where(valid_date_mask)[0]
    valid_dates = dates.iloc[valid_positions]

    if split_mode == "chronological":
        if test_ratio <= 0:
            return valid_positions, np.array([], dtype=int), {
                "split_mode": split_mode,
                "split_date_column": split_date_column,
                "cutoff_year": cutoff_year,
                "split_warning": "test_ratio <= 0 のため全件を学習に使いました。",
            }
        order = np.argsort(valid_dates.to_numpy())
        ordered_positions = valid_positions[order]
        test_size = max(1, int(round(len(ordered_positions) * test_ratio)))
        train_idx = np.sort(ordered_positions[:-test_size])
        test_idx = np.sort(ordered_positions[-test_size:])
        if len(train_idx) < 3:
            raise ValueError("時系列分割後の学習データが少なすぎます。検証比率を下げてください。")
        cutoff_date = dates.iloc[test_idx].min()
        return train_idx, test_idx, {
            "split_mode": split_mode,
            "split_date_column": split_date_column,
            "cutoff_year": cutoff_year,
            "chronological_test_start": str(cutoff_date.date()) if pd.notna(cutoff_date) else "",
            "split_warning": "",
        }

    if split_mode == "cutoff_year":
        if cutoff_year is None:
            raise ValueError("年指定分割にはカットオフ年を指定してください。")
        years = dates.dt.year
        train_idx = np.where((years <= cutoff_year).fillna(False).to_numpy())[0]
        test_idx = np.where((years > cutoff_year).fillna(False).to_numpy())[0]
        if len(train_idx) < 10 or len(test_idx) < 3:
            raise ValueError(
                f"年指定分割後の件数が不足しています。train={len(train_idx)} test={len(test_idx)} cutoff={cutoff_year}"
            )
        return np.sort(train_idx), np.sort(test_idx), {
            "split_mode": split_mode,
            "split_date_column": split_date_column,
            "cutoff_year": cutoff_year,
            "split_warning": "",
        }

    raise ValueError(f"未知の分割方法です: {split_mode}")


def evaluate_models(
    data: pd.DataFrame,
    *,
    target_column: str,
    feature_columns: list[str],
    methods: Iterable[str],
    test_ratio: float,
    seed: int,
    split_mode: str,
    split_date_column: str | None,
    cutoff_year: int | None,
    l1_ratio: float,
    require_positive_score: bool,
    impute_feature_median: bool,
    only_xbrl_parsed: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    source_data = data
    status_column = "底検知時_EDINET状態"
    if only_xbrl_parsed and status_column in source_data.columns:
        source_data = source_data[source_data[status_column].astype(str).str.strip() == "xbrl_parsed"].copy()

    raw = pd.DataFrame(index=data.index)
    raw = pd.DataFrame(index=source_data.index)
    raw[target_column] = numeric_series(source_data[target_column])
    for col in feature_columns:
        raw[col] = numeric_series(source_data[col])
    if split_date_column and split_date_column in source_data.columns:
        raw[split_date_column] = source_data[split_date_column]

    raw = raw.replace([np.inf, -np.inf], np.nan)
    clean = raw.dropna(subset=[target_column]).copy()
    dropped_feature_columns: list[str] = []
    imputed_feature_columns: list[str] = []
    feature_medians: dict[str, float] = {}
    if impute_feature_median:
        for col in list(feature_columns):
            median = clean[col].median(skipna=True)
            if not np.isfinite(median):
                clean = clean.drop(columns=[col])
                feature_columns.remove(col)
                dropped_feature_columns.append(col)
                continue
            missing_count = int(clean[col].isna().sum())
            if missing_count:
                clean[col] = clean[col].fillna(float(median))
                imputed_feature_columns.append(col)
                feature_medians[col] = float(median)
    else:
        clean = clean.dropna()
    if len(clean) < 10:
        raise ValueError(f"有効データが少なすぎます: {len(clean)}件")
    if not feature_columns:
        raise ValueError("有効な説明変数がありません。")

    y = clean[target_column].to_numpy(dtype=float)
    x = clean[feature_columns].to_numpy(dtype=float)
    train_idx, test_idx, split_metadata = split_indices(
        clean,
        test_ratio=test_ratio,
        seed=seed,
        split_mode=split_mode,
        split_date_column=split_date_column,
        cutoff_year=cutoff_year,
    )
    std = standardize(x[train_idx], y[train_idx])
    x_all_std = (x - std.x_mean) / std.x_std
    y_all_std = (y - std.y_mean) / std.y_std
    y_train = y[train_idx]
    y_test = y[test_idx] if len(test_idx) else np.array([], dtype=float)

    best_results: list[ModelResult] = []
    prediction_cols: dict[str, np.ndarray] = {}

    for method in methods:
        candidates: list[ModelResult] = []
        for alpha in alpha_grid():
            if method in {"Lasso", "ElasticNet"} and alpha == 0.0:
                continue
            try:
                beta = fit_model(std.x, std.y, method, alpha, l1_ratio)
            except np.linalg.LinAlgError:
                continue
            pred_std = x_all_std @ beta
            pred = pred_std * std.y_std + std.y_mean
            if require_positive_score:
                corr_train = pearson_corr(pred[train_idx], y_train)
            else:
                corr_train = abs(pearson_corr(pred[train_idx], y_train))
            if len(test_idx):
                corr_eval_raw = pearson_corr(pred[test_idx], y_test)
                corr_eval = corr_eval_raw if require_positive_score else abs(corr_eval_raw)
            else:
                corr_eval_raw = pearson_corr(pred[train_idx], y_train)
                corr_eval = corr_train
            if not np.isfinite(corr_eval):
                corr_eval = -math.inf
            candidates.append(
                ModelResult(
                    method=method,
                    alpha=alpha,
                    l1_ratio=l1_ratio if method == "ElasticNet" else (1.0 if method == "Lasso" else 0.0),
                    coefficients=beta,
                    train_corr=pearson_corr(pred[train_idx], y_train),
                    test_corr=corr_eval_raw if len(test_idx) else float("nan"),
                    train_r2=r2_score(y_train, pred[train_idx]),
                    test_r2=r2_score(y_test, pred[test_idx]) if len(test_idx) else float("nan"),
                    train_mae=mae(y_train, pred[train_idx]),
                    test_mae=mae(y_test, pred[test_idx]) if len(test_idx) else float("nan"),
                    selected_by=float(corr_eval),
                )
            )
        if not candidates:
            continue
        best = max(candidates, key=lambda item: item.selected_by)
        best_results.append(best)
        prediction_cols[f"{method}_score"] = x_all_std @ best.coefficients
        prediction_cols[f"{method}_predicted_target"] = prediction_cols[f"{method}_score"] * std.y_std + std.y_mean

    if not best_results:
        raise ValueError("モデルを作成できませんでした。説明変数や目的変数を確認してください。")

    summary_rows = []
    coef_rows = []
    for result in best_results:
        summary_rows.append(
            {
                "method": result.method,
                "alpha": result.alpha,
                "l1_ratio": result.l1_ratio,
                "train_corr": result.train_corr,
                "test_corr": result.test_corr,
                "train_r2": result.train_r2,
                "test_r2": result.test_r2,
                "train_mae": result.train_mae,
                "test_mae": result.test_mae,
                "nonzero_coefficients": int(np.sum(np.abs(result.coefficients) > 1e-10)),
            }
        )
        for col, coef in zip(feature_columns, result.coefficients):
            coef_rows.append(
                {
                    "method": result.method,
                    "feature": col,
                    "standardized_weight": coef,
                    "abs_weight": abs(coef),
                }
            )

    predictions = source_data.loc[clean.index].copy()
    predictions["_target_numeric"] = y
    predictions["_train_test"] = "unused"
    predictions.iloc[train_idx, predictions.columns.get_loc("_train_test")] = "train"
    if len(test_idx):
        predictions.iloc[test_idx, predictions.columns.get_loc("_train_test")] = "test"
    for col, values in prediction_cols.items():
        predictions[col] = values

    metadata = {
        "input_rows": int(len(data)),
        "rows_after_status_filter": int(len(source_data)),
        "valid_rows": int(len(clean)),
        "train_rows": int(len(train_idx)),
        "test_rows": int(len(test_idx)),
        "unused_valid_rows": int(len(clean) - len(train_idx) - len(test_idx)),
        "dropped_rows": int(len(data) - len(clean)),
        "only_xbrl_parsed": only_xbrl_parsed,
        "target_column": target_column,
        "feature_columns": feature_columns,
        "impute_feature_median": impute_feature_median,
        "imputed_feature_columns": imputed_feature_columns,
        "dropped_feature_columns": dropped_feature_columns,
        "feature_medians": feature_medians,
        "test_ratio": test_ratio,
        "random_seed": seed,
        **split_metadata,
        "l1_ratio": l1_ratio,
        "require_positive_score": require_positive_score,
    }
    return pd.DataFrame(summary_rows), pd.DataFrame(coef_rows), {"predictions": predictions, "metadata": metadata}


class RegressionApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.df: pd.DataFrame | None = None
        self.csv_path = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR.resolve()))
        self.target_var = tk.StringVar()
        self.test_ratio_var = tk.DoubleVar(value=0.25)
        self.seed_var = tk.IntVar(value=42)
        self.split_mode_var = tk.StringVar(value="random")
        self.split_date_column_var = tk.StringVar()
        self.cutoff_year_var = tk.IntVar(value=2020)
        self.l1_ratio_var = tk.DoubleVar(value=0.5)
        self.positive_corr_var = tk.BooleanVar(value=True)
        self.impute_feature_median_var = tk.BooleanVar(value=True)
        self.add_derived_features_var = tk.BooleanVar(value=True)
        self.only_xbrl_parsed_var = tk.BooleanVar(value=True)
        self.method_vars = {
            "Ridge": tk.BooleanVar(value=True),
            "Lasso": tk.BooleanVar(value=True),
            "ElasticNet": tk.BooleanVar(value=True),
        }
        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        file_frame = ttk.LabelFrame(root, text="入力")
        file_frame.pack(fill="x")
        ttk.Label(file_frame, text="CSV").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(file_frame, textvariable=self.csv_path).grid(row=0, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(file_frame, text="選択", command=self.select_csv).grid(row=0, column=2, padx=6, pady=5)
        ttk.Button(file_frame, text="読み込み", command=self.load_csv).grid(row=0, column=3, padx=6, pady=5)
        ttk.Label(file_frame, text="出力フォルダ").grid(row=1, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(file_frame, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(file_frame, text="選択", command=self.select_output_dir).grid(row=1, column=2, padx=6, pady=5)
        file_frame.columnconfigure(1, weight=1)

        config = ttk.Frame(root)
        config.pack(fill="both", expand=True, pady=10)

        left = ttk.LabelFrame(config, text="列の選択")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(left, text="目的変数（例: 下落率）").pack(anchor="w", padx=8, pady=(8, 2))
        self.target_combo = ttk.Combobox(left, textvariable=self.target_var, state="readonly")
        self.target_combo.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(left, text="説明変数（Ctrl/Shiftで複数選択）").pack(anchor="w", padx=8)
        self.feature_list = tk.Listbox(left, selectmode="extended", exportselection=False, height=20)
        self.feature_list.pack(fill="both", expand=True, padx=8, pady=5)
        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=8, pady=5)
        ttk.Button(btns, text="全選択", command=self.select_all_features).pack(side="left", padx=(0, 5))
        ttk.Button(btns, text="クリア", command=self.clear_features).pack(side="left", padx=5)
        ttk.Button(btns, text="EDINET財務っぽい列を選択", command=self.select_edinet_features).pack(side="left", padx=5)

        right = ttk.LabelFrame(config, text="モデル設定")
        right.pack(side="left", fill="both", expand=True)
        methods = ttk.Frame(right)
        methods.pack(fill="x", padx=8, pady=8)
        for name, var in self.method_vars.items():
            ttk.Checkbutton(methods, text=name, variable=var).pack(side="left", padx=8)

        grid = ttk.Frame(right)
        grid.pack(fill="x", padx=8, pady=8)
        ttk.Label(grid, text="検証データ比率").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=0.0, to=0.8, increment=0.05, textvariable=self.test_ratio_var, width=8).grid(
            row=0, column=1, sticky="w", pady=4
        )
        ttk.Label(grid, text="分割方法").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            grid,
            textvariable=self.split_mode_var,
            values=["random", "chronological", "cutoff_year"],
            state="readonly",
            width=16,
        ).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="日付列").grid(row=2, column=0, sticky="w", pady=4)
        self.split_date_combo = ttk.Combobox(grid, textvariable=self.split_date_column_var, state="readonly", width=28)
        self.split_date_combo.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="カットオフ年").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=1990, to=2035, increment=1, textvariable=self.cutoff_year_var, width=8).grid(
            row=3, column=1, sticky="w", pady=4
        )
        ttk.Label(grid, text="乱数seed").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=0, to=999999, increment=1, textvariable=self.seed_var, width=8).grid(
            row=4, column=1, sticky="w", pady=4
        )
        ttk.Label(grid, text="ElasticNet L1比率").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=0.05, to=0.95, increment=0.05, textvariable=self.l1_ratio_var, width=8).grid(
            row=5, column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(
            right,
            text="正の相関が最大になるように選ぶ（下落率がマイナス保存なら通常ON）",
            variable=self.positive_corr_var,
        ).pack(anchor="w", padx=8, pady=8)
        ttk.Checkbutton(
            right,
            text="説明変数の欠損は中央値で補完する（行を捨てすぎないため通常ON）",
            variable=self.impute_feature_median_var,
        ).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(
            right,
            text="人件費・人件費/売上・一人当たり指標などの派生特徴量を使う",
            variable=self.add_derived_features_var,
        ).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(
            right,
            text="EDINET財務取得成功行だけ使う（底検知時_EDINET状態=xbrl_parsed）",
            variable=self.only_xbrl_parsed_var,
        ).pack(anchor="w", padx=8, pady=2)
        ttk.Button(right, text="Ridge / Lasso / Elastic Net 実行", command=self.run_analysis).pack(
            anchor="w", padx=8, pady=8
        )
        ttk.Button(right, text="出力フォルダを開く", command=self.open_output_dir).pack(anchor="w", padx=8, pady=2)

        self.log = tk.Text(root, height=12, wrap="word")
        self.log.pack(fill="both", expand=False, pady=(8, 0))
        self.write_log("CSVを選択して読み込んでください。")

    def write_log(self, message: str) -> None:
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.update_idletasks()

    def select_csv(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.csv_path.set(path)

    def select_output_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.output_dir.set(path)

    def load_csv(self) -> None:
        path = Path(self.csv_path.get())
        if not path.exists():
            messagebox.showerror(APP_TITLE, "CSVファイルが見つかりません。")
            return
        try:
            self.df = read_csv_flexible(path)
            if self.add_derived_features_var.get():
                before = len(self.df.columns)
                self.df = add_derived_features(self.df)
                added = len(self.df.columns) - before
            else:
                added = 0
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        columns = list(self.df.columns)
        self.target_combo["values"] = columns
        date_candidates = [
            c
            for c in columns
            if ("日" in c or "date" in c.lower()) and parse_date_series(self.df[c]).notna().sum() >= 10
        ]
        self.split_date_combo["values"] = date_candidates
        preferred_date = next((c for c in date_candidates if "底打ち候補日" in c), None)
        if not preferred_date:
            preferred_date = next((c for c in date_candidates if "底検知" in c and "日" in c), None)
        if not preferred_date and date_candidates:
            preferred_date = date_candidates[0]
        self.split_date_column_var.set(preferred_date or "")
        likely_targets = [c for c in columns if "下落率" in c or "下げ幅" in c or "騰落" in c]
        self.target_var.set(likely_targets[0] if likely_targets else columns[0])
        self.feature_list.delete(0, "end")
        for col in columns:
            self.feature_list.insert("end", col)
        self.write_log(f"読み込み完了: {path}")
        self.write_log(f"行数={len(self.df):,} 列数={len(self.df.columns):,}")
        if added:
            self.write_log(f"派生特徴量を {added} 列追加しました。空欄は0ではなく欠損として扱います。")
        if likely_targets:
            self.write_log(f"目的変数候補として {likely_targets[0]} を選びました。")
        self.select_edinet_features()

    def select_all_features(self) -> None:
        self.feature_list.select_set(0, "end")

    def clear_features(self) -> None:
        self.feature_list.selection_clear(0, "end")

    def select_edinet_features(self) -> None:
        keywords = [
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
            "底検知時_ROE",
            "底検知時_現金等",
            "底検知時_研究開発費",
            "底検知時_設備投資額",
            "派生_推定人件費_平均年収×従業員数",
            "派生_推定人件費売上比率",
            "派生_一人当たり売上高",
            "派生_一人当たり営業利益",
            "派生_一人当たり経常利益",
            "派生_一人当たり現金等",
            "派生_一人当たり純資産",
            "派生_勤続安定度_勤続年数÷社会人年数",
            "派生_研究開発費売上比率",
            "派生_設備投資売上比率",
        ]
        self.clear_features()
        values = self.feature_list.get(0, "end")
        selected = 0
        for i, col in enumerate(values):
            if col in keywords:
                self.feature_list.select_set(i)
                selected += 1
        self.write_log(f"EDINET財務っぽい説明変数を {selected} 列選択しました。")

    def selected_features(self) -> list[str]:
        return [self.feature_list.get(i) for i in self.feature_list.curselection()]

    def run_analysis(self) -> None:
        if self.df is None:
            messagebox.showerror(APP_TITLE, "先にCSVを読み込んでください。")
            return
        target = self.target_var.get()
        features = [col for col in self.selected_features() if col != target]
        methods = [name for name, var in self.method_vars.items() if var.get()]
        if not target or target not in self.df.columns:
            messagebox.showerror(APP_TITLE, "目的変数を選んでください。")
            return
        if not features:
            messagebox.showerror(APP_TITLE, "説明変数を1列以上選んでください。")
            return
        if not methods:
            messagebox.showerror(APP_TITLE, "手法を1つ以上選んでください。")
            return
        out_dir = Path(self.output_dir.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = time.strftime("%Y%m%d_%H%M%S")
        run_dir = out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        self.write_log("分析開始")
        self.write_log(f"目的変数: {target}")
        self.write_log(f"説明変数: {len(features)}列")
        self.write_log(
            f"分割方法: {self.split_mode_var.get()} / 日付列: {self.split_date_column_var.get() or '-'} / "
            f"カットオフ年: {self.cutoff_year_var.get()} / 検証比率: {self.test_ratio_var.get()}"
        )
        try:
            summary, coefs, extra = evaluate_models(
                self.df,
                target_column=target,
                feature_columns=features,
                methods=methods,
                test_ratio=float(self.test_ratio_var.get()),
                seed=int(self.seed_var.get()),
                split_mode=self.split_mode_var.get(),
                split_date_column=self.split_date_column_var.get() or None,
                cutoff_year=int(self.cutoff_year_var.get()),
                l1_ratio=float(self.l1_ratio_var.get()),
                require_positive_score=bool(self.positive_corr_var.get()),
                impute_feature_median=bool(self.impute_feature_median_var.get()),
                only_xbrl_parsed=bool(self.only_xbrl_parsed_var.get()),
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self.write_log(f"エラー: {exc}")
            return

        summary_path = run_dir / "model_summary.csv"
        coef_path = run_dir / "model_coefficients.csv"
        pred_path = run_dir / "predictions.csv"
        bucket_path = run_dir / "bucket_validation.csv"
        meta_path = run_dir / "run_metadata.json"
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        coefs.sort_values(["method", "abs_weight"], ascending=[True, False]).to_csv(
            coef_path, index=False, encoding="utf-8-sig"
        )
        extra["predictions"].to_csv(pred_path, index=False, encoding="utf-8-sig")
        bucket_validation = build_bucket_validation(extra["predictions"], methods)
        bucket_validation.to_csv(bucket_path, index=False, encoding="utf-8-sig")
        meta_path.write_text(json.dumps(extra["metadata"], ensure_ascii=False, indent=2), encoding="utf-8")

        self.write_log("分析完了")
        self.write_log(summary.to_string(index=False))
        self.write_log(f"summary: {summary_path}")
        self.write_log(f"coefficients: {coef_path}")
        self.write_log(f"predictions: {pred_path}")
        self.write_log(f"bucket_validation: {bucket_path}")
        if not bucket_validation.empty:
            preview_cols = [
                col
                for col in [
                    "method",
                    "split",
                    "bucket",
                    "count",
                    "候補後下落率_mean",
                    "高値→底下落率_mean",
                    "現在までリターン_mean",
                    "年利_mean",
                    "成功_rate",
                    "許容_rate",
                    "早すぎ_rate",
                ]
                if col in bucket_validation.columns
            ]
            self.write_log(bucket_validation[preview_cols].to_string(index=False))
        messagebox.showinfo(APP_TITLE, f"分析が完了しました。\n{run_dir}")

    def open_output_dir(self) -> None:
        path = Path(self.output_dir.get())
        path.mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))


def main() -> None:
    app = RegressionApp()
    app.mainloop()


if __name__ == "__main__":
    main()
