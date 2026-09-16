from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parent / "outputs" / "market_cap_model_analysis"
SOURCE_PATH = BASE / "model_events.csv"
TICKER_PATH = BASE / "detected_ticker_buy_assessment.csv"
MODEL_PATH = BASE / "detected_model_buy_summary.csv"
SMALL_MODEL_PATH = BASE / "small_cap_model_buy_summary.csv"
COMBINATION_PATH = BASE / "detected_model_combination_summary.csv"
REPORT_PATH = BASE / "detected_ticker_buy_report.md"

MODEL_NAMES = {
    "JP-SMALL-BTM-v0": "小型モデル",
    "JP-MID-BTM-v1": "中型モデル",
    "JP-LARGE-BTM-v0": "大型モデル",
}
MODEL_ORDER = list(MODEL_NAMES)


def numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce")


def fmt_number(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:,.{digits}f}"


def fmt_pct(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def signal_history(frame: pd.DataFrame) -> str:
    work = frame.copy()
    work["_date"] = pd.to_datetime(work["シグナル日"], errors="coerce")
    work["_price"] = numeric(work, "シグナル価格")
    work = work.sort_values(["_date", "_price"])
    entries: list[str] = []
    for _, row in work.iterrows():
        date = row["_date"].strftime("%Y-%m-%d") if pd.notna(row["_date"]) else str(row["シグナル日"])
        price = f"{row['_price']:,.2f}円" if pd.notna(row["_price"]) else "価格不明"
        entry = f"{date} @ {price}"
        if entry not in entries:
            entries.append(entry)
    return " | ".join(entries)


def add_assessment_columns(events: pd.DataFrame) -> pd.DataFrame:
    result = events.copy()
    return_252 = numeric(result, "252日後リターン")
    acwi_252 = numeric(result, "比較_ACWI_252日リターン")
    result["252日ACWI超過"] = return_252 - acwi_252
    result["買い結果フラグ"] = return_252.gt(0) & result["252日ACWI超過"].gt(0)
    result["底タイミング合格フラグ"] = result["候補後判定"].isin(["成功", "許容"])
    result["厳格買い結果フラグ"] = result["買い結果フラグ"] & result["底タイミング合格フラグ"]
    return result


def summarize_models(events: pd.DataFrame, scope: str) -> pd.DataFrame:
    buy_union = set(events.loc[events["買い結果フラグ"], "銘柄"])
    strict_union = set(events.loc[events["厳格買い結果フラグ"], "銘柄"])
    rows: list[dict[str, object]] = []
    for model_id in MODEL_ORDER:
        part = events.loc[events["モデルID"] == model_id].copy()
        if part.empty:
            continue
        per_ticker = part.groupby("銘柄", sort=False).agg(
            イベント数=("銘柄", "size"),
            買い結果あり=("買い結果フラグ", "max"),
            厳格買い結果あり=("厳格買い結果フラグ", "max"),
            底タイミング合格あり=("底タイミング合格フラグ", "max"),
        )
        detected = len(per_ticker)
        detected_tickers = set(per_ticker.index)
        buy_label_detected = len(detected_tickers & buy_union)
        buy_count = int(per_ticker["買い結果あり"].sum())
        strict_count = int(per_ticker["厳格買い結果あり"].sum())
        rows.append({
            "集計範囲": scope,
            "モデルID": model_id,
            "モデル名": MODEL_NAMES[model_id],
            "底検知イベント数": len(part),
            "底検知銘柄数": detected,
            "買い判定銘柄検知数": buy_label_detected,
            "買い判定銘柄率": buy_label_detected / detected if detected else np.nan,
            "買い判定銘柄捕捉率": buy_label_detected / len(buy_union) if buy_union else np.nan,
            "買い結果あり銘柄数": buy_count,
            "買い結果なし銘柄数": detected - buy_count,
            "買い精度_銘柄単位": buy_count / detected if detected else np.nan,
            "買い銘柄捕捉率": buy_count / len(buy_union) if buy_union else np.nan,
            "厳格買い結果あり銘柄数": strict_count,
            "厳格買い精度_銘柄単位": strict_count / detected if detected else np.nan,
            "厳格買い銘柄捕捉率": strict_count / len(strict_union) if strict_union else np.nan,
            "底タイミング合格あり銘柄数": int(per_ticker["底タイミング合格あり"].sum()),
            "252日リターン中央値": numeric(part, "252日後リターン").median(),
            "252日ACWI超過中央値": numeric(part, "252日ACWI超過").median(),
        })
    return pd.DataFrame(rows)


def build_ticker_detail(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for ticker, group in events.groupby("銘柄", sort=True):
        group = group.copy()
        buy_count = int(group["買い結果フラグ"].sum())
        strict_count = int(group["厳格買い結果フラグ"].sum())
        timing_count = int(group["底タイミング合格フラグ"].sum())
        event_count = len(group)
        if strict_count > 0:
            decision = "買い候補（底タイミング合格あり）"
        elif buy_count > 0:
            decision = "買い候補（早すぎシグナルのみ）"
        else:
            decision = "見送り"

        row: dict[str, object] = {
            "銘柄": ticker,
            "企業名": group["企業名"].dropna().iloc[0] if group["企業名"].notna().any() else "",
            "底検知モデル": " / ".join(
                MODEL_NAMES[model_id] for model_id in MODEL_ORDER if model_id in set(group["モデルID"])
            ),
            "底検知イベント数": event_count,
            "底検知価格_全履歴": signal_history(group),
            "時価総額区分": " / ".join(sorted(group["時価総額区分"].dropna().astype(str).unique())),
            "買うべき判定_事後": decision,
            "買い結果イベント数": buy_count,
            "買い結果イベント率": buy_count / event_count,
            "厳格買い結果イベント数": strict_count,
            "底タイミング合格イベント数": timing_count,
            "252日リターン中央値": numeric(group, "252日後リターン").median(),
            "252日ACWI超過中央値": numeric(group, "252日ACWI超過").median(),
            "現在保有リターン中央値": numeric(group, "現在まで保有リターン").median(),
        }
        for model_id in MODEL_ORDER:
            model_name = MODEL_NAMES[model_id]
            part = group.loc[group["モデルID"] == model_id]
            row[f"{model_name}_底検知"] = "あり" if not part.empty else "なし"
            row[f"{model_name}_底検知回数"] = len(part)
            row[f"{model_name}_底検知日価格"] = signal_history(part) if not part.empty else ""
            row[f"{model_name}_買い結果あり"] = "あり" if part["買い結果フラグ"].any() else "なし"
            row[f"{model_name}_厳格買い結果あり"] = "あり" if part["厳格買い結果フラグ"].any() else "なし"
        rows.append(row)
    return pd.DataFrame(rows)


def summary_rows(summary: pd.DataFrame) -> list[list[object]]:
    rows: list[list[object]] = []
    for _, row in summary.iterrows():
        rows.append([
            row["モデル名"],
            int(row["底検知銘柄数"]),
            int(row["買い判定銘柄検知数"]),
            fmt_pct(row["買い判定銘柄率"]),
            fmt_pct(row["買い判定銘柄捕捉率"]),
            int(row["買い結果あり銘柄数"]),
            fmt_pct(row["買い精度_銘柄単位"]),
            int(row["厳格買い結果あり銘柄数"]),
            fmt_pct(row["厳格買い精度_銘柄単位"]),
            fmt_number(row["252日リターン中央値"]),
            fmt_number(row["252日ACWI超過中央値"]),
        ])
    return rows


def main() -> int:
    events = pd.read_csv(SOURCE_PATH, encoding="utf-8-sig", low_memory=False)
    events = add_assessment_columns(events)

    ticker_detail = build_ticker_detail(events)
    model_summary = summarize_models(events, "全時価総額帯")
    small_events = events.loc[events["時価総額区分"] == "small"].copy()
    small_summary = summarize_models(small_events, "小型帯")
    combination_summary = (
        ticker_detail.assign(
            _buy=ticker_detail["買うべき判定_事後"].ne("見送り"),
            _strict=ticker_detail["厳格買い結果イベント数"].gt(0),
        )
        .groupby("底検知モデル", as_index=False)
        .agg(
            銘柄数=("銘柄", "size"),
            買い判定銘柄数=("_buy", "sum"),
            厳格買い判定銘柄数=("_strict", "sum"),
        )
        .sort_values("銘柄数", ascending=False)
    )
    combination_summary["買い判定率"] = (
        combination_summary["買い判定銘柄数"] / combination_summary["銘柄数"]
    )

    ticker_detail.to_csv(TICKER_PATH, index=False, encoding="utf-8-sig")
    model_summary.to_csv(MODEL_PATH, index=False, encoding="utf-8-sig")
    small_summary.to_csv(SMALL_MODEL_PATH, index=False, encoding="utf-8-sig")
    combination_summary.to_csv(COMBINATION_PATH, index=False, encoding="utf-8-sig")

    best_capture = model_summary.sort_values(
        ["買い判定銘柄検知数", "買い判定銘柄率"], ascending=False
    ).iloc[0]
    best_small_precision = small_summary.sort_values(
        ["買い判定銘柄率", "買い判定銘柄検知数"], ascending=False
    ).iloc[0]
    perfect_small = small_summary.loc[small_summary["買い判定銘柄率"] == 1.0]
    buy_tickers = ticker_detail["買うべき判定_事後"].ne("見送り").sum()
    strict_tickers = ticker_detail["厳格買い結果イベント数"].gt(0).sum()

    headers = [
        "モデル", "検知銘柄", "買い判定銘柄を検知", "検知銘柄中の買い判定率", "買い判定銘柄捕捉率",
        "自身の良い買い場", "良い買い場精度", "厳格買い場", "厳格精度", "252日中央値", "ACWI超過中央値",
    ]
    lines = [
        "# 底検知銘柄・モデル別買い結果集計",
        "",
        "## 判定基準",
        "",
        "このレポートの『買うべき』は、将来を予測した推奨ではなく、バックテスト後の事後判定です。",
        "",
        "- **買い結果あり**: シグナル価格で買った場合の252日後リターンがプラス、かつ同期間のACWIを上回った。",
        "- **底タイミング合格**: 既存判定が `成功` または `許容` で、`早すぎ` ではない。",
        "- **厳格買い結果あり**: 上記二つを同時に満たした。",
        "- 銘柄単位では、同じモデルの複数シグナルのうち一度でも条件を満たせば『あり』とした。安定性は明細の `買い結果イベント率` も併せて確認する。",
        "",
        "## 全体集計",
        "",
        f"一度でも底検知された銘柄は **{len(ticker_detail):,}銘柄**。このうち買い結果ありが **{int(buy_tickers):,}銘柄**、底タイミングまで合格した厳格買い結果ありが **{int(strict_tickers):,}銘柄**です。",
        "",
        markdown_table(headers, summary_rows(model_summary)),
        "",
        f"買い判定銘柄を最も多く検知したのは **{best_capture['モデル名']}（{int(best_capture['買い判定銘柄検知数'])}銘柄、全買い判定銘柄の{fmt_pct(best_capture['買い判定銘柄捕捉率'])}）** です。ただし、単に同じ銘柄を検知しただけでなく、そのモデル自身のシグナル価格が良い買い場だったかは `自身の良い買い場` で分けて確認しています。",
        "",
        "### 検知モデルの組み合わせ",
        "",
        markdown_table(
            ["検知モデル", "銘柄数", "買い判定銘柄", "買い判定率", "厳格買い判定"],
            [
                [
                    row["底検知モデル"], int(row["銘柄数"]), int(row["買い判定銘柄数"]),
                    fmt_pct(row["買い判定率"]), int(row["厳格買い判定銘柄数"]),
                ]
                for _, row in combination_summary.iterrows()
            ],
        ),
        "",
        "## 小型時価総額帯",
        "",
        markdown_table(headers, summary_rows(small_summary)),
        "",
    ]
    if perfect_small.empty:
        lines.append("**小型帯で『買い結果あり銘柄だけ』を検知したモデルはありません。** どのモデルにも買い結果なし銘柄が含まれます。")
    else:
        names = "、".join(perfect_small["モデル名"].astype(str))
        lines.append(f"小型帯で買い結果あり銘柄だけを検知したモデルは **{names}** です。")
    lines.extend([
        "",
        f"小型帯で、検知銘柄に占める買い判定銘柄の割合が最も高いのは **{best_small_precision['モデル名']}（{fmt_pct(best_small_precision['買い判定銘柄率'])}）** です。",
        "",
        "## 読み方と注意",
        "",
        "- `detected_ticker_buy_assessment.csv` には、全銘柄についてモデル名、全シグナル日・価格、買い判定、モデル別結果を収録した。",
        "- 『一度でも成功』はモデルの試行回数が多いほど有利になる。買い精度だけでなくイベント率、固定期間中央値、厳格判定を見る必要がある。",
        "- 同じ銘柄の複数イベントは独立ではない。これは銘柄別の記述集計であり、統計的な将来収益保証ではない。",
        "- 現在の上場銘柄中心のため、生存者バイアスが残る。特に小型帯の精度は上場廃止銘柄追加後に再検証する必要がある。",
        "",
        "## 出力",
        "",
        f"- `{TICKER_PATH.name}`",
        f"- `{MODEL_PATH.name}`",
        f"- `{SMALL_MODEL_PATH.name}`",
        f"- `{COMBINATION_PATH.name}`",
    ])
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(REPORT_PATH)
    print(TICKER_PATH)
    print(MODEL_PATH)
    print(SMALL_MODEL_PATH)
    print(COMBINATION_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
