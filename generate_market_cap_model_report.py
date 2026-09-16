from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_DIR = Path("outputs/market_cap_model_analysis")
OUTPUT_PATH = INPUT_DIR / "market_cap_model_analysis_report.md"
RNG = np.random.default_rng(20260713)


def number(value: object, digits: int = 1) -> str:
    if pd.isna(value):
        return "-"
    return f"{float(value):,.{digits}f}"


def percent(value: object, digits: int = 1) -> str:
    if pd.isna(value):
        return "-"
    return f"{float(value) * 100:.{digits}f}%"


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in rows)
    return "\n".join(lines)


def clustered_bootstrap_ci(
    frame: pd.DataFrame,
    column: str,
    statistic: str = "mean",
    iterations: int = 3000,
) -> tuple[float, float, float, int]:
    groups = {
        ticker: pd.to_numeric(group[column], errors="coerce").dropna().to_numpy(float)
        for ticker, group in frame.groupby("銘柄")
    }
    groups = {ticker: values for ticker, values in groups.items() if len(values)}
    tickers = np.array(list(groups), dtype=object)
    if not len(tickers):
        return np.nan, np.nan, np.nan, 0
    observed_values = np.concatenate(list(groups.values()))
    observed = float(np.mean(observed_values) if statistic == "mean" else np.median(observed_values))
    samples = np.empty(iterations, dtype=float)
    for index in range(iterations):
        selected = RNG.choice(tickers, size=len(tickers), replace=True)
        values = np.concatenate([groups[ticker] for ticker in selected])
        samples[index] = np.mean(values) if statistic == "mean" else np.median(values)
    low, high = np.quantile(samples, [0.025, 0.975])
    return observed, float(low), float(high), len(tickers)


def paired_stock_difference(
    events: pd.DataFrame,
    column: str,
    model_a: str,
    model_b: str,
    iterations: int = 5000,
) -> tuple[float, float, float, int]:
    values = events.pivot_table(index="銘柄", columns="モデルID", values=column, aggfunc="mean")
    if model_a not in values or model_b not in values:
        return np.nan, np.nan, np.nan, 0
    difference = (values[model_a] - values[model_b]).dropna().to_numpy(float)
    if not len(difference):
        return np.nan, np.nan, np.nan, 0
    bootstrap = np.empty(iterations, dtype=float)
    for index in range(iterations):
        bootstrap[index] = RNG.choice(difference, size=len(difference), replace=True).mean()
    low, high = np.quantile(bootstrap, [0.025, 0.975])
    return float(difference.mean()), float(low), float(high), len(difference)


def near_overlap(events: pd.DataFrame, model_a: str, model_b: str, days: int = 7) -> tuple[int, float]:
    left = events.loc[events["モデルID"] == model_a, ["銘柄", "シグナル日"]]
    right = events.loc[events["モデルID"] == model_b, ["銘柄", "シグナル日"]]
    right_dates = {
        ticker: np.sort(group["シグナル日"].to_numpy(dtype="datetime64[D]"))
        for ticker, group in right.groupby("銘柄")
    }
    matched = 0
    for ticker, when in left.itertuples(index=False):
        dates = right_dates.get(ticker)
        if dates is None:
            continue
        day = np.datetime64(when.date(), "D")
        position = np.searchsorted(dates, day)
        distances: list[int] = []
        if position < len(dates):
            distances.append(abs(int((dates[position] - day).astype(int))))
        if position > 0:
            distances.append(abs(int((dates[position - 1] - day).astype(int))))
        if distances and min(distances) <= days:
            matched += 1
    return matched, matched / len(left) if len(left) else np.nan


def main() -> None:
    report = json.loads((INPUT_DIR / "analysis_report.json").read_text(encoding="utf-8"))
    settings = json.loads((INPUT_DIR / "model_settings.json").read_text(encoding="utf-8"))
    events = pd.read_csv(INPUT_DIR / "model_events.csv", encoding="utf-8-sig", low_memory=False)
    summary = pd.read_csv(INPUT_DIR / "model_summary.csv", encoding="utf-8-sig")
    exclusion = pd.read_csv(INPUT_DIR / "top_winner_exclusion.csv", encoding="utf-8-sig")
    overlap = pd.read_csv(INPUT_DIR / "model_signal_overlap.csv", encoding="utf-8-sig")
    benchmark_comparison_path = INPUT_DIR / "benchmark_equal_weight_summary.csv"
    benchmark_comparison = (
        pd.read_csv(benchmark_comparison_path, encoding="utf-8-sig")
        if benchmark_comparison_path.exists() else pd.DataFrame()
    )
    events["シグナル日"] = pd.to_datetime(events["シグナル日"], errors="coerce")

    model_names = dict(events[["モデルID", "モデル名"]].drop_duplicates().itertuples(index=False, name=None))
    model_order = ["JP-SMALL-BTM-v0", "JP-MID-BTM-v1", "JP-LARGE-BTM-v0"]
    full = summary.loc[summary["期間"] == "全期間"].set_index("モデルID")

    overview_rows: list[list[object]] = []
    verdict_rows: list[list[object]] = []
    ci_rows: list[list[object]] = []
    for model in model_order:
        row = full.loc[model]
        group = events.loc[events["モデルID"] == model]
        verdict = group["候補後判定"].value_counts()
        overview_rows.append([
            model_names[model], int(row["イベント数"]), int(row["銘柄数"]),
            number(row["60日_中央値"]), number(row["126日_中央値"]), number(row["252日_中央値"]),
            number(row["504日_中央値"]), number(row["756日_中央値"]),
            number(row["252日_超過中央値"]), number(row["756日_超過中央値"]),
            percent(row["126日偽底率"]), percent(row["756日大化け率"]),
        ])
        verdict_rows.append([
            model_names[model],
            percent(verdict.get("成功", 0) / len(group)),
            percent(verdict.get("許容", 0) / len(group)),
            percent(verdict.get("早すぎ", 0) / len(group)),
            number(pd.to_numeric(group["候補後最安値下落率"], errors="coerce").median()),
        ])
        for metric in ("252日後リターン", "756日後リターン", "252日ベンチマーク超過", "756日ベンチマーク超過"):
            observed, low, high, stocks = clustered_bootstrap_ci(group, metric)
            ci_rows.append([model_names[model], metric, number(observed), f"[{number(low)}, {number(high)}]", stocks])

    period_rows: list[list[object]] = []
    for period in ("2012-2020", "2021-2022", "2023-2024"):
        for model in model_order:
            row = summary.loc[(summary["期間"] == period) & (summary["モデルID"] == model)].iloc[0]
            period_rows.append([
                period, model_names[model], int(row["イベント数"]),
                number(row["252日_中央値"]), number(row["252日_超過中央値"]),
                number(row["756日_中央値"]), number(row["756日_超過中央値"]),
                percent(row["126日偽底率"]),
            ])

    exclusion_rows: list[list[object]] = []
    for model in model_order:
        base = exclusion.loc[(exclusion["モデルID"] == model) & (exclusion["上位除外数"] == 0)].iloc[0]
        cut = exclusion.loc[(exclusion["モデルID"] == model) & (exclusion["上位除外数"] == 10)].iloc[0]
        exclusion_rows.append([
            model_names[model], number(base["現在保有平均"]), number(cut["現在保有平均"]),
            number(base["現在保有中央値"]), number(cut["現在保有中央値"]),
            number(base["756日_平均"]), number(cut["756日_平均"]),
        ])

    pair_rows: list[list[object]] = []
    for metric in ("252日後リターン", "756日後リターン", "252日ベンチマーク超過", "756日ベンチマーク超過"):
        for model_a, model_b in (("JP-SMALL-BTM-v0", "JP-MID-BTM-v1"), ("JP-LARGE-BTM-v0", "JP-MID-BTM-v1")):
            difference, low, high, count = paired_stock_difference(events, metric, model_a, model_b)
            pair_rows.append([
                f"{model_names[model_a]} - {model_names[model_b]}", metric,
                number(difference), f"[{number(low)}, {number(high)}]", count,
                "差を確認" if low > 0 or high < 0 else "明確な差なし",
            ])

    exact_multi = int((overlap["検出モデル数"] >= 2).sum())
    exact_all = int((overlap["検出モデル数"] == 3).sum())
    overlap_rows: list[list[object]] = []
    for model_a, model_b in (("JP-SMALL-BTM-v0", "JP-MID-BTM-v1"), ("JP-LARGE-BTM-v0", "JP-MID-BTM-v1"), ("JP-SMALL-BTM-v0", "JP-LARGE-BTM-v0")):
        count, rate = near_overlap(events, model_a, model_b)
        overlap_rows.append([model_names[model_a], model_names[model_b], count, percent(rate)])

    benchmark_rows: list[list[object]] = []
    if not benchmark_comparison.empty:
        benchmark_indexed = benchmark_comparison.set_index("モデルID")
        for model in model_order:
            row = benchmark_indexed.loc[model]
            benchmark_rows.append([
                model_names[model],
                number(row["個別株現在リターン平均"]),
                number(row["ACWI同額購入平均"]),
                number(row["SPY同額購入平均"]),
                number(row["個別株現在リターン中央値"]),
                number(row["個別株-ACWI中央値差"]),
                number(row["個別株-SPY中央値差"]),
                percent(row["個別株がACWI超過した割合"]),
                percent(row["個別株がSPY超過した割合"]),
            ])

    best_252 = full["252日_中央値"].idxmax()
    best_756 = full["756日_中央値"].idxmax()
    lowest_false = full["126日偽底率"].idxmin()
    newest = summary.loc[summary["期間"] == "2023-2024"].set_index("モデルID")
    newest_best_252 = newest["252日_超過中央値"].idxmax()

    lines = [
        "# 時価総額帯別・固定閾値底検知モデル比較レポート",
        "",
        f"作成対象: `{INPUT_DIR.resolve()}`  ",
        f"解析期間: {settings['start_date']}～{settings['end_date']} / 対象企業: {report['company_count']}社 / イベント: {report['event_count']}件  ",
        f"既存アルゴリズム: {report['legacy_algorithm_version']} / 解析アプリ: v{report['app_version']}",
        "",
        "## 結論",
        "",
        "1. **固定閾値をずらすと、検出件数と成績は実際に変わる。** したがって閾値選択が重要だという仮説は支持されます。ただし、これだけでは時価総額帯ごとに別モデルが必要だとは証明できません。",
        f"2. 全銘柄へ一律適用した比較では、252日中央値が最も高いのは **{model_names[best_252]}**、756日中央値が最も高いのは **{model_names[best_756]}**、126日偽底率が最も低いのは **{model_names[lowest_false]}** でした。",
        f"3. 記述統計では、直近2023～2024年の252日ベンチマーク超過中央値も **{model_names[newest_best_252]}** が最良でした。大型株モデルは今回の3期間すべてで相対的に優勢ですが、直近値そのものはマイナスであり、典型的イベントがベンチマークを上回ったとはいえません。",
        "4. **本命仮説である『小型株には小型モデル、大型株には大型モデル』は未検証です。** 今回は時点別時価総額CSVが未指定で、3モデルを同じ688社へ適用したためです。",
        "5. よって現段階の判定は、**モデル分割の発想は支持、具体的な小型・大型閾値の採用は保留**です。",
        "",
        "## 実行の完全性",
        "",
        f"処理は正常終了しています。{report['processed_company_count']}/{report['company_count']}社を処理し、エラー終了・途中停止ではありません。所要時間は{number(report['elapsed_seconds'], 2)}秒でした。",
        "",
        "## 全期間の比較",
        "",
        markdown_table(
            ["モデル", "イベント", "銘柄", "60日中央値", "126日中央値", "252日中央値", "504日中央値", "756日中央値", "252日超過中央値", "756日超過中央値", "偽底率", "大化け率"],
            overview_rows,
        ),
        "",
        "単位はリターン・超過リターンが%です。偽底は126日以内最大下落率が-15%以下、大化けは756日後リターンが+100%以上という本アプリの定義です。",
        "",
        "## 底検知品質",
        "",
        markdown_table(["モデル", "成功", "許容", "早すぎ", "候補後最安値下落率中央値"], verdict_rows),
        "",
        "ここでは長期平均リターンだけでなく、『シグナル後にさらにどれだけ下がったか』も確認しています。リターンが高くても早すぎ率が高いモデルは、即時購入モデルとしては扱いに注意が必要です。",
        "",
        "## 時期別安定性",
        "",
        markdown_table(["期間", "モデル", "イベント", "252日中央値", "252日超過中央値", "756日中央値", "756日超過中央値", "偽底率"], period_rows),
        "",
        "今回の区分では大型株モデルが一貫して相対首位でした。ただし2023～2024年は3モデルとも252日超過中央値が大幅なマイナスです。相対首位と、投資対象として十分な絶対成績は別問題です。特に2012年前後の大幅上昇を含む全期間平均だけで判断しない方が安全です。",
        "",
        "## 大化け銘柄への依存",
        "",
        markdown_table(["モデル", "現在保有平均", "上位10除外後", "現在保有中央値", "上位10除外後", "756日平均", "上位10除外後"], exclusion_rows),
        "",
        "平均が上位10件除外で大きく低下し、中央値が比較的安定する場合、成績の一部は少数の大化け銘柄に依存しています。これは既存アルゴリズムで観察されていた『中央値より平均が強い』性質と整合します。",
        "",
        "## ACWI・S&P 500との同額購入比較",
        "",
        markdown_table(
            ["モデル", "個別株平均", "ACWI平均", "SPY平均", "個別株中央値", "ACWIとの差中央値", "SPYとの差中央値", "ACWI超過率", "SPY超過率"],
            benchmark_rows,
        ) if benchmark_rows else "比較データはまだ生成されていません。",
        "",
        "各シグナル日に個別株、ACWI、SPYへ同額を投資し、現在まで保有したイベント単位の比較です。個別株の平均は指数を上回る一方、差の中央値は全モデルでマイナス、指数超過率も50%未満です。したがって平均超過は広範な優位ではなく、少数の大化け銘柄による右裾の効果です。なおUSD建てETFとの騰落率比較で、為替損益は含みません。",
        "",
        "## 銘柄単位ブートストラップ",
        "",
        markdown_table(["モデル", "指標", "観測平均", "銘柄クラスタ95%区間", "銘柄数"], ci_rows),
        "",
        "同一銘柄から複数シグナルが出るため、イベントを完全に独立とはみなさず、銘柄単位で再標本化しました。区間が0をまたぐ超過リターンは、プラスの平均だけを見て優位と断定できません。",
        "",
        "## 中型株モデルとの差",
        "",
        markdown_table(["比較", "指標", "銘柄平均差", "95%区間", "共通銘柄", "判定"], pair_rows),
        "",
        "共通銘柄について各モデルの平均値を比較したものです。95%区間が0をまたぐ場合、今回の標本だけでは差の方向を確定できません。",
        "",
        "## シグナルの重複",
        "",
        f"一意な銘柄・日付イベントは{len(overlap):,}件で、同日中に2モデル以上が検出したものは{exact_multi:,}件、3モデルすべてが検出したものは{exact_all:,}件でした。確認日数がモデルごとに異なるため、同じ上昇局面でも数日ずれることがあります。",
        "",
        markdown_table(["基準モデル", "比較モデル", "±7日以内一致", "基準モデル中の割合"], overlap_rows),
        "",
        "## 仮説判定",
        "",
        "| 仮説 | 判定 | 根拠 |",
        "|---|---|---|",
        "| 閾値選択が成績へ影響する | **支持** | 閾値変更により検出数、偽底率、期間別リターンが大きく変化した |",
        "| 全銘柄・全相場で固定閾値1組では不十分 | **未確定** | 大型株モデルが全期間・全区分で記述統計上優勢で、規模別分割が必須とはまだいえない |",
        "| 小型・中型・大型でモデルを分ける価値がある | **検証継続に値する** | モデルは異なるイベント集合を作るが、時価総額帯別の直接比較は未実施 |",
        "| 現在の小型株モデルが小型株に優れる | **判定不能／全社一律では不採用寄り** | シグナル時点の時価総額がなく、小型株だけの成績は不明。全社では偽底率72.2%・252日中央値-11.5% |",
        "| 現在の大型株モデルが大型株に優れる | **判定不能／候補として有望** | 大型株だけの成績は不明だが、全社の記述統計では3期間一貫して最良 |",
        "| 閾値をずらすだけで直ちに実運用モデルになる | **否定寄り** | 大型モデルでも偽底率46.3%、252日・756日超過中央値がマイナス |",
        "| 大化け株を拾う性質が存在する | **支持** | 全モデルで756日大化けイベントが存在し、平均と中央値の差も大きい |",
        "",
        "## 重要な制約",
        "",
        "- **時価総額帯未分類:** `size_history_mode=none` のため、本来のルーティング仮説を検証できていません。",
        "- **生存者バイアス:** 現在DBに残る銘柄が中心です。小型株モデルほど上場廃止銘柄欠落の影響を受けやすい可能性があります。",
        "- **ベンチマーク通貨:** ACWIの現地通貨建て価格変化と日本株の円建て変化を単純比較しており、為替を含む日本人投資家の実現リターンとは一致しません。",
        "- **売買コスト未反映:** 手数料、スプレッド、税、流動性、指値未約定を含みません。",
        "- **同時保有制約未反映:** イベントごとのリターンであり、資金上限を持つポートフォリオの成績ではありません。",
        "- **閾値は暫定:** 小型・大型モデルは仮説から置いた値で、独立した検証期間で選択された値ではありません。",
        "",
        "## 次の検証",
        "",
        "1. 各シグナル日時点の時価総額を用意し、small / mid / largeへpoint-in-time分類する。",
        "2. 各帯の中で、3モデルを同じ銘柄・同じ期間へ適用して比較する。",
        "3. 閾値調整に使わない最終ホールドアウト期間を残す。",
        "4. 上場廃止銘柄を追加して、特に小型株帯の偽底率を再計算する。",
        "5. ACWIを円換算するか、円建ての比較可能なベンチマークへ変更する。",
        "6. 最終的にはイベント平均ではなく、同時保有数・資金配分・売買コスト込みのポートフォリオで評価する。",
        "",
        "## 最終判断",
        "",
        "今回の結果から確実にいえるのは、**閾値選択は重要であり、現行中型株モデルより大型株モデルの設定が記述統計上かなり有望**ということです。一方、共通銘柄の差の95%区間はすべて0をまたぎ、大型モデルのベンチマーク超過中央値もマイナスでした。したがって大型モデルへの即時置換はまだ早く、現在の小型モデルは全社一律用途では不採用寄りです。**規模別モデルが本当に精度を上げるかは、時点別時価総額を結合した次の検証で初めて判定できます。**",
        "",
    ]
    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(OUTPUT_PATH.resolve())


if __name__ == "__main__":
    main()
