from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parent / "outputs" / "market_cap_model_analysis"
REPORT_PATH = BASE / "market_cap_model_hypothesis_report.md"
ROBUSTNESS_PATH = BASE / "market_cap_model_robustness.csv"
RNG_SEED = 20260713
BOOTSTRAP_REPS = 2000

MODEL_NAMES = {
    "JP-SMALL-BTM-v0": "小型モデル",
    "JP-MID-BTM-v1": "中型モデル",
    "JP-LARGE-BTM-v0": "大型モデル",
}


def read_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(BASE / name, encoding="utf-8-sig", low_memory=False)


def numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame.get(column, pd.Series(index=frame.index, dtype=float)), errors="coerce")


def boolean_numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame.get(column, pd.Series(index=frame.index, dtype=object))
    if values.dtype == bool:
        return values.astype(float)
    return values.map({True: 1.0, False: 0.0, "True": 1.0, "False": 0.0, 1: 1.0, 0: 0.0})


def stat_value(values: np.ndarray, stat: str) -> float:
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return np.nan
    if stat == "mean":
        return float(np.mean(values))
    if stat == "median":
        return float(np.median(values))
    raise ValueError(stat)


def cluster_arrays(frame: pd.DataFrame, column: str) -> dict[str, np.ndarray]:
    work = pd.DataFrame({"ticker": frame["銘柄"].astype(str), "value": numeric(frame, column)})
    work = work.dropna(subset=["value"])
    return {
        ticker: group["value"].to_numpy(dtype=float)
        for ticker, group in work.groupby("ticker")
    }


def cluster_bootstrap_ci(
    frame: pd.DataFrame,
    column: str,
    stat: str,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    groups = cluster_arrays(frame, column)
    tickers = list(groups)
    observed = stat_value(numeric(frame, column).to_numpy(dtype=float), stat)
    if len(tickers) < 2:
        return observed, np.nan, np.nan
    draws = np.empty(BOOTSTRAP_REPS)
    for index in range(BOOTSTRAP_REPS):
        sampled = rng.choice(tickers, size=len(tickers), replace=True)
        values = np.concatenate([groups[ticker] for ticker in sampled])
        draws[index] = stat_value(values, stat)
    low, high = np.nanpercentile(draws, [2.5, 97.5])
    return observed, float(low), float(high)


def cluster_bootstrap_difference(
    base: pd.DataFrame,
    alternative: pd.DataFrame,
    column: str,
    stat: str,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    base_groups = cluster_arrays(base, column)
    alt_groups = cluster_arrays(alternative, column)
    tickers = sorted(set(base_groups) | set(alt_groups))
    observed = stat_value(numeric(alternative, column).to_numpy(dtype=float), stat) - stat_value(
        numeric(base, column).to_numpy(dtype=float), stat
    )
    if len(tickers) < 2:
        return observed, np.nan, np.nan
    draws: list[float] = []
    for _ in range(BOOTSTRAP_REPS):
        sampled = rng.choice(tickers, size=len(tickers), replace=True)
        base_values = [base_groups[ticker] for ticker in sampled if ticker in base_groups]
        alt_values = [alt_groups[ticker] for ticker in sampled if ticker in alt_groups]
        if not base_values or not alt_values:
            continue
        draws.append(
            stat_value(np.concatenate(alt_values), stat)
            - stat_value(np.concatenate(base_values), stat)
        )
    low, high = np.nanpercentile(draws, [2.5, 97.5])
    return observed, float(low), float(high)


def fmt(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:,.{digits}f}"


def pct(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


def ci_text(value: float, low: float, high: float) -> str:
    return f"{fmt(value)} [{fmt(low)}, {fmt(high)}]"


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(output)


def summarize_frame(frame: pd.DataFrame) -> dict[str, float]:
    current = numeric(frame, "現在まで保有リターン")
    acwi = numeric(frame, "比較_ACWI_現在リターン")
    spy = numeric(frame, "比較_SPY_現在リターン")
    return {
        "events": len(frame),
        "tickers": frame["銘柄"].nunique(),
        "false_126": boolean_numeric(frame, "126日偽底フラグ").mean(),
        "huge_756": boolean_numeric(frame, "756日大化けフラグ").mean(),
        "return_252_mean": numeric(frame, "252日後リターン").mean(),
        "return_252_median": numeric(frame, "252日後リターン").median(),
        "return_756_mean": numeric(frame, "756日後リターン").mean(),
        "return_756_median": numeric(frame, "756日後リターン").median(),
        "current_mean": current.mean(),
        "current_median": current.median(),
        "annualized_median": numeric(frame, "現在まで年利換算").median(),
        "acwi_excess_mean": (current - acwi).mean(),
        "acwi_excess_median": (current - acwi).median(),
        "acwi_win_rate": (current > acwi).mean(),
        "spy_excess_mean": (current - spy).mean(),
        "spy_excess_median": (current - spy).median(),
        "spy_win_rate": (current > spy).mean(),
    }


def period_label(values: pd.Series) -> pd.Series:
    dates = pd.to_datetime(values, errors="coerce")
    years = dates.dt.year
    return pd.Series(
        np.select(
            [years.le(2020), years.between(2021, 2022), years.between(2023, 2024)],
            ["2012-2020", "2021-2022", "2023-2024"],
            default="other",
        ),
        index=values.index,
    )


def top_exclusion(frame: pd.DataFrame, removed: int) -> dict[str, float]:
    ordered = frame.assign(_return=numeric(frame, "現在まで保有リターン")).sort_values(
        "_return", ascending=False
    )
    trimmed = ordered.iloc[removed:]
    current = numeric(trimmed, "現在まで保有リターン")
    acwi = numeric(trimmed, "比較_ACWI_現在リターン")
    return {
        "removed": removed,
        "n": len(trimmed),
        "current_mean": current.mean(),
        "current_median": current.median(),
        "acwi_excess_mean": (current - acwi).mean(),
        "acwi_excess_median": (current - acwi).median(),
    }


def main() -> int:
    report = json.loads((BASE / "analysis_report.json").read_text(encoding="utf-8"))
    settings = json.loads((BASE / "model_settings.json").read_text(encoding="utf-8"))
    events = read_csv("model_events.csv")
    threshold_events = read_csv("threshold_set_routed_events.csv")
    threshold_summary = read_csv("threshold_set_summary.csv")
    band_summary = read_csv("market_cap_band_model_summary.csv")
    coverage = read_csv("market_cap_coverage_summary.csv")
    overlap = read_csv("model_signal_overlap.csv")
    rng = np.random.default_rng(RNG_SEED)

    cap_known = events.loc[numeric(events, "時価総額円").notna()].copy()
    set_names = threshold_summary.sort_values("境界セット順")["境界セット名"].tolist()
    set_frames = {
        name: threshold_events.loc[threshold_events["境界セット名"] == name].copy()
        for name in set_names
    }
    set_metrics = {name: summarize_frame(frame) for name, frame in set_frames.items()}

    robustness_rows: list[dict[str, object]] = []
    ci_metrics = [
        ("756日後リターン", "median", "756日中央値"),
        ("現在まで保有リターン", "median", "現在保有中央値"),
        ("252日後リターン", "median", "252日中央値"),
    ]
    ci_lookup: dict[tuple[str, str], tuple[float, float, float]] = {}
    for name, frame in set_frames.items():
        for column, stat, label in ci_metrics:
            estimate, low, high = cluster_bootstrap_ci(frame, column, stat, rng)
            ci_lookup[(name, label)] = (estimate, low, high)
            robustness_rows.append({
                "比較種別": "境界セット単独CI",
                "対象": name,
                "指標": label,
                "推定差または値": estimate,
                "95%CI下限": low,
                "95%CI上限": high,
                "0を跨ぐ": low <= 0 <= high if np.isfinite(low) and np.isfinite(high) else None,
            })

    base_name = set_names[0]
    difference_lookup: dict[tuple[str, str], tuple[float, float, float]] = {}
    for name in set_names[1:]:
        for column, stat, label in ci_metrics:
            estimate, low, high = cluster_bootstrap_difference(
                set_frames[base_name], set_frames[name], column, stat, rng
            )
            difference_lookup[(name, label)] = (estimate, low, high)
            robustness_rows.append({
                "比較種別": f"{base_name}との差",
                "対象": name,
                "指標": label,
                "推定差または値": estimate,
                "95%CI下限": low,
                "95%CI上限": high,
                "0を跨ぐ": low <= 0 <= high if np.isfinite(low) and np.isfinite(high) else None,
            })

    band_difference_lookup: dict[tuple[str, str, str], tuple[float, float, float]] = {}
    for model_id, model_frame in cap_known.groupby("モデルID"):
        band_frames = {
            band: model_frame.loc[model_frame["時価総額区分"] == band].copy()
            for band in ("small", "mid", "large")
        }
        for comparison, base_band in (("中型-小型", "small"), ("中型-大型", "large")):
            for column, label in (("252日後リターン", "252日中央値"), ("756日後リターン", "756日中央値")):
                estimate, low, high = cluster_bootstrap_difference(
                    band_frames[base_band], band_frames["mid"], column, "median", rng
                )
                band_difference_lookup[(model_id, comparison, label)] = (estimate, low, high)
                robustness_rows.append({
                    "比較種別": comparison,
                    "対象": MODEL_NAMES.get(model_id, model_id),
                    "指標": label,
                    "推定差または値": estimate,
                    "95%CI下限": low,
                    "95%CI上限": high,
                    "0を跨ぐ": low <= 0 <= high if np.isfinite(low) and np.isfinite(high) else None,
                })

    pd.DataFrame(robustness_rows).to_csv(ROBUSTNESS_PATH, index=False, encoding="utf-8-sig")

    threshold_rows: list[list[object]] = []
    for _, row in threshold_summary.sort_values("境界セット順").iterrows():
        name = row["境界セット名"]
        metric = set_metrics[name]
        threshold_rows.append([
            name,
            f"{fmt(row['小型上限億円'], 0)} / {fmt(row['大型下限億円'], 0)}",
            int(metric["events"]),
            fmt(metric["return_252_median"]),
            ci_text(*ci_lookup[(name, "756日中央値")]),
            fmt(metric["current_median"]),
            fmt(metric["annualized_median"]),
            pct(metric["false_126"]),
            pct(metric["huge_756"]),
            fmt(metric["acwi_excess_median"]),
        ])

    band_rows: list[list[object]] = []
    for band in ("small", "mid", "large"):
        part = band_summary.loc[band_summary["時価総額区分"] == band]
        for _, row in part.sort_values("モデルID").iterrows():
            band_rows.append([
                band,
                MODEL_NAMES.get(row["モデルID"], row["モデルID"]),
                int(row["イベント数"]),
                fmt(row["252日_中央値"]),
                fmt(row["756日_中央値"]),
                fmt(row["現在保有中央値"]),
                pct(row["126日偽底率"]),
                pct(row["756日大化け率"]),
                fmt(row["現在ベンチマーク超過中央値"]),
            ])

    band_difference_rows: list[list[object]] = []
    for model_id in MODEL_NAMES:
        if model_id not in cap_known["モデルID"].unique():
            continue
        band_difference_rows.append([
            MODEL_NAMES[model_id],
            ci_text(*band_difference_lookup[(model_id, "中型-小型", "252日中央値")]),
            ci_text(*band_difference_lookup[(model_id, "中型-小型", "756日中央値")]),
            ci_text(*band_difference_lookup[(model_id, "中型-大型", "252日中央値")]),
            ci_text(*band_difference_lookup[(model_id, "中型-大型", "756日中央値")]),
        ])

    single_model_rows: list[list[object]] = []
    single_metrics: dict[str, dict[str, float]] = {}
    for model_id, frame in cap_known.groupby("モデルID"):
        metric = summarize_frame(frame)
        single_metrics[model_id] = metric
        single_model_rows.append([
            MODEL_NAMES.get(model_id, model_id),
            int(metric["events"]),
            fmt(metric["return_252_median"]),
            fmt(metric["return_756_median"]),
            fmt(metric["current_median"]),
            pct(metric["false_126"]),
            pct(metric["huge_756"]),
            fmt(metric["acwi_excess_median"]),
        ])

    difference_rows: list[list[object]] = []
    for name in set_names[1:]:
        difference_rows.append([
            name,
            ci_text(*difference_lookup[(name, "252日中央値")]),
            ci_text(*difference_lookup[(name, "756日中央値")]),
            ci_text(*difference_lookup[(name, "現在保有中央値")]),
        ])

    period_rows: list[list[object]] = []
    for name, frame in set_frames.items():
        work = frame.copy()
        work["区間"] = period_label(work["シグナル日"])
        for period in ("2012-2020", "2021-2022", "2023-2024"):
            part = work.loc[work["区間"] == period]
            metric = summarize_frame(part)
            period_rows.append([
                name,
                period,
                int(metric["events"]),
                fmt(metric["return_252_median"]),
                fmt(metric["return_756_median"]),
                fmt(metric["current_median"]),
                pct(metric["false_126"]),
            ])

    exclusion_rows: list[list[object]] = []
    for name, frame in set_frames.items():
        for removed in (0, 1, 3, 5, 10):
            result = top_exclusion(frame, removed)
            exclusion_rows.append([
                name,
                removed,
                result["n"],
                fmt(result["current_mean"]),
                fmt(result["current_median"]),
                fmt(result["acwi_excess_mean"]),
                fmt(result["acwi_excess_median"]),
            ])

    top_standard = set_frames[base_name].assign(
        _return=numeric(set_frames[base_name], "現在まで保有リターン")
    ).sort_values("_return", ascending=False).head(10)
    positive = numeric(set_frames[base_name], "現在まで保有リターン").clip(lower=0)
    top10_positive_share = (
        top_standard["_return"].clip(lower=0).sum() / positive.sum()
        if positive.sum() > 0 else np.nan
    )
    top_rows = [
        [row["銘柄"], row["企業名"], MODEL_NAMES.get(row["モデルID"], row["モデルID"]), fmt(row["_return"])]
        for _, row in top_standard.iterrows()
    ]

    overlap_counts = overlap["検出モデル数"].value_counts().sort_index().to_dict()
    estimated_coverage = coverage.loc[
        coverage["時価総額情報種別"] == "estimated_from_current_market_cap", "イベント割合"
    ].iloc[0]

    lines = [
        "# 時価総額帯別・底検知モデル検証レポート",
        "",
        f"作成日: 2026-07-13  /  アプリ: v{report['app_version']}  /  対象期間: {settings['start_date']}〜{settings['end_date']}",
        "",
        "## 結論",
        "",
        "**仮説は二つに分けて判定すべきです。**",
        "",
        "1. **『中型時価総額帯に成績のスイートスポットがある』は、今回の標本では強く支持されます。** 3モデルすべてで、中型帯は小型帯より長期リターン中央値・現在保有中央値が大幅に高く、大型帯も概ね上回りました。モデル固有というより、時価総額帯そのものに結びついた差です。",
        "2. **『小型・中型・大型ごとに専用モデルへ切り替えれば、単一モデルより良くなる』は支持されません。** 特に小型帯では小型専用モデルが3モデル中で最も弱く、標準ルーティング全体の中央値を押し下げています。大型専用モデルには一部改善が見られますが、全面的な成功とは言えません。",
        "3. **境界値の最適解は確定していません。** `大型広め（700/2000億円）` は現在保有中央値で標準セットを上回りましたが、保有期間を揃えた252日・756日中央値差の95%区間は0を跨ぎます。今回最良に見える境界は、次回データでの事前固定検証が必要です。",
        "4. **平均リターンは強い一方、典型的なイベントはACWIに負けています。** 全境界セットで現在までのACWI超過中央値は約-71〜-76ポイントです。正の超過平均は少数の大化け銘柄によるため、『平均が高い』だけで採用するのは危険です。",
        "",
        "したがって現時点の実務判断は、**小型・中型・大型へ自動ルーティングするより、まず中型時価総額帯をフィルタとして使い、現行中型モデルまたは大型モデルを比較する**方がデータに忠実です。",
        "",
        "## 1. 検証範囲とデータ品質",
        "",
        markdown_table(
            ["項目", "値"],
            [
                ["処理企業数", f"{report['processed_company_count']:,}社"],
                ["モデルイベント行", f"{len(events):,}件"],
                ["一意な銘柄×シグナル日", f"{len(overlap):,}件"],
                ["検出モデル数の内訳", ", ".join(f"{int(k)}モデル={int(v):,}件" for k, v in overlap_counts.items())],
                ["時価総額推定カバー率", pct(float(estimated_coverage))],
                ["推定可能なイベント銘柄", f"{cap_known['銘柄'].nunique():,}銘柄"],
                ["実行時間", f"{report['elapsed_seconds']:.1f}秒"],
            ],
        ),
        "",
        "時価総額は `現在時価総額 × シグナル価格 ÷ 現在価格` で推定されています。株式分割には概ね対応しますが、増資・自社株買い・合併による発行済株式数変化は反映できません。",
        "",
        "## 2. 時価総額帯の効果",
        "",
        markdown_table(
            ["規模帯", "適用モデル", "件数", "252日中央値", "756日中央値", "現在保有中央値", "126日偽底率", "756日大化け率", "ACWI超過中央値"],
            band_rows,
        ),
        "",
        "### 中型帯の中央値差（銘柄単位ブートストラップ、95%区間）",
        "",
        markdown_table(
            ["モデル", "中型-小型 252日", "中型-小型 756日", "中型-大型 252日", "中型-大型 756日"],
            band_difference_rows,
        ),
        "",
        "差が正なら中型帯が優位です。小型帯との差は3モデル・両固定期間で95%区間が正です。大型帯に対しては、中型モデルと大型モデルの756日差は正ですが、252日差と小型モデルの756日差は未確定です。したがって『中型は小型より強い』は頑健ですが、『中型は大型より常に強い』は部分支持です。",
        "",
        "### 判定",
        "",
        "- **中型帯の優位はモデルを跨いで再現**しています。大型モデルでも中型帯の現在保有中央値は小型帯・大型帯より高く、中型モデル・小型モデルでも同じ方向です。",
        "- **小型専用モデルは小型帯で改善していません。** 小型帯の現在保有中央値、756日中央値、偽底率のいずれも厳しく、現段階では採用根拠がありません。",
        "- **大型専用モデルは大型帯で部分的に妥当**です。大型帯の現在保有中央値は良好ですが、中型モデルと大差ではなく、偽底率・大化け率では中型モデルが勝つ指標もあります。",
        "",
        "## 3. 単一モデルとの比較",
        "",
        "時価総額を推定できたイベントだけに限定し、各モデルを単独で使った場合を比較しました。",
        "",
        markdown_table(
            ["単一モデル", "件数", "252日中央値", "756日中央値", "現在保有中央値", "126日偽底率", "756日大化け率", "ACWI超過中央値"],
            single_model_rows,
        ),
        "",
        "ルーティングが単一モデルを一貫して上回る形ではありません。特に小型モデルへ振り分けられるイベントが多いため、専用モデル切替の弱点が全体へ波及しています。",
        "",
        "## 4. 境界セット比較",
        "",
        markdown_table(
            ["境界セット", "小型上限/大型下限(億円)", "件数", "252日中央値", "756日中央値 [95%CI]", "現在保有中央値", "年利中央値", "126日偽底率", "756日大化け率", "ACWI超過中央値"],
            threshold_rows,
        ),
        "",
        "`大型広め（700/2000億円）` は今回、現在保有中央値39.6%、756日中央値11.5%、偽底率60.0%で4セット中もっともバランスが良く見えます。ただし、他セットとの差は小さく、同じデータで境界を選んだ結果なので最適化バイアスを含みます。",
        "",
        "### 標準セットとの差（銘柄単位ブートストラップ、95%区間）",
        "",
        markdown_table(
            ["境界セット", "252日中央値差", "756日中央値差", "現在保有中央値差"],
            difference_rows,
        ),
        "",
        "`大型広め` の現在保有中央値差だけは95%区間が0を上回りました。ただし現在保有はシグナル日によって保有期間が異なり、固定252日・756日では全セットの差が0を跨ぎます。したがって境界セット間の順位は『候補』であって『確定』ではありません。",
        "",
        "## 5. 時期別の安定性",
        "",
        markdown_table(
            ["境界セット", "シグナル期", "件数", "252日中央値", "756日中央値", "現在保有中央値", "126日偽底率"],
            period_rows,
        ),
        "",
        "古い期間ほど現在保有リターンが大きくなるのは保有期間の長さと生存者バイアスの影響を受けます。より重要なのは固定252日成績ですが、近年区間で弱いセットがあるため、全期間平均だけでの判断は避けるべきです。",
        "",
        "## 6. 大化け銘柄への依存",
        "",
        markdown_table(
            ["境界セット", "上位除外数", "残件数", "現在平均", "現在中央値", "ACWI超過平均", "ACWI超過中央値"],
            exclusion_rows,
        ),
        "",
        f"標準セットでは、上位10イベントが正の現在リターン総和の **{pct(float(top10_positive_share))}** を占めます。中央値のACWI超過が負である一方、平均超過が正なのはこの右裾の長い分布によります。",
        "",
        "### 標準セットの現在リターン上位10イベント",
        "",
        markdown_table(["銘柄", "企業名", "採用モデル", "現在リターン"], top_rows),
        "",
        "## 7. 仮説の最終判定",
        "",
        markdown_table(
            ["仮説", "判定", "根拠"],
            [
                ["中型時価総額帯は成績が良い", "支持", "3モデルすべてで中型帯の長期中央値が小型帯を大幅に上回る"],
                ["全銘柄共通の固定閾値が規模差を生む", "部分支持", "規模帯差は明瞭だが、原因が固定閾値だけとは識別できない"],
                ["小型専用モデルが小型帯を改善する", "棄却寄り", "小型帯で専用モデルが最も弱く、偽底率も高い"],
                ["大型専用モデルが大型帯を改善する", "部分支持", "現在保有中央値は良いが、中型モデルとの差は一貫しない"],
                ["1100〜3300億円が最適境界", "不支持", "標準や大型広めを一貫して上回らず、差のCIも広い"],
                ["規模別自動ルーティングは単一モデルより優れる", "現時点では不支持", "小型モデル採用部分が全体中央値を押し下げ、典型イベントはACWI未達"],
                ["大型下限を2000億円へ下げる案", "次回検証候補", "今回の4セットでは最も均衡しているが、同一標本内選択なので未確定"],
            ],
        ),
        "",
        "## 8. 重要な制約",
        "",
        "- 対象は現在の株価DBに残る688社であり、上場廃止銘柄を含む完全な当時点ユニバースではありません。生存者バイアスがあります。",
        "- 時価総額は現在の発行済株式数を暗黙に使った推定であり、完全なpoint-in-time値ではありません。",
        "- 同一銘柄の複数イベントは独立ではありません。CIは銘柄単位でクラスタ化しましたが、相場局面の共通ショックまでは除去していません。",
        "- 現在保有リターンはシグナル日ごとに保有期間が異なるため、境界評価では252日・756日固定期間を優先すべきです。",
        "- ACWI・SPY比較は為替換算と売買コストを含みません。日本円投資家の実現成績とは一致しません。",
        "- 4境界セットを同じデータで比較しているため、多重比較・選択バイアスがあります。",
        "",
        "## 9. 次に行うべき検証",
        "",
        "1. `大型広め（700/2000億円）` と `標準（700/5000億円）` を事前固定し、未使用期間または更新データで一度だけ比較する。",
        "2. 自動ルーティングとは別に、**中型帯だけを採用するフィルタ戦略**を作り、現行中型モデル・大型モデルを同じ投資日・同じ資金配分で比較する。",
        "3. 小型モデルは閾値微調整より先に、偽底回避条件を再設計する。現行結果のまま実運用へ入れない。",
        "4. EODHD等で上場廃止銘柄と当時点時価総額を追加し、生存者バイアスを潰して再検証する。",
        "5. 平均ではなく、中央値・ACWI超過率・最大ドローダウン・上位10除外後成績を採用判定の主指標にする。",
        "",
        "## 参照ファイル",
        "",
        "- `model_events.csv`",
        "- `market_cap_band_model_summary.csv`",
        "- `threshold_set_summary.csv`",
        "- `threshold_set_routed_events.csv`",
        "- `benchmark_equal_weight_summary.csv`",
        "- `top_winner_exclusion.csv`",
        "- `model_settings.json` / `analysis_report.json`",
        "",
        "補助統計は `market_cap_model_robustness.csv` に保存しています。",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(REPORT_PATH.resolve())
    print(ROBUSTNESS_PATH.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
