from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
GOV_PATH = Path(r"C:\Users\saban\Downloads\ガバナンス指標.csv")
COMPANY_PATH = Path(r"C:\Users\saban\Downloads\圧縮company_data.csv")
OVERTIME_PATH = ROOT / "outputs" / "overtime_source_comparison_with_reviews_manual_filled.csv"
OUT_DIR = ROOT / "outputs" / "governance_indicator_analysis"


def norm_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", "", text).replace("＆", "&")


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def strip_industry(value: object) -> str:
    text = str(value).strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text


def summarize_group(df: pd.DataFrame, group_col: str, min_count: int = 10) -> pd.DataFrame:
    grouped = (
        df.dropna(subset=[group_col])
        .groupby(group_col)
        .agg(
            件数=("ガバナンス指標", "size"),
            平均=("ガバナンス指標", "mean"),
            中央値=("ガバナンス指標", "median"),
            標準偏差=("ガバナンス指標", "std"),
            上位25pct=("ガバナンス指標", lambda s: s.quantile(0.75)),
            上位10pct=("ガバナンス指標", lambda s: s.quantile(0.90)),
            最大=("ガバナンス指標", "max"),
            高懸念率_10超=("ガバナンス指標", lambda s: float((s > 10).mean())),
            低懸念率_5未満=("ガバナンス指標", lambda s: float((s < -5).mean())),
            口コミ平均との差=("口コミ平均との差", "mean"),
            提出残業平均=("提出残業平均", "mean"),
            口コミ残業平均=("口コミ残業平均", "mean"),
        )
        .reset_index()
    )
    grouped = grouped[grouped["件数"] >= min_count].copy()
    return grouped.sort_values(["平均", "件数"], ascending=[False, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    gov = pd.read_csv(GOV_PATH, encoding="utf-8-sig", header=None, names=["企業名", "ガバナンス指標"], dtype=str, keep_default_na=False)
    gov["ガバナンス指標"] = to_num(gov["ガバナンス指標"])
    gov = gov[gov["企業名"].ne("企業名") & gov["ガバナンス指標"].notna()].copy()
    gov["_key"] = gov["企業名"].map(norm_name)

    company = pd.read_csv(COMPANY_PATH, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    company["_key"] = company["企業名"].map(norm_name)
    company["業種"] = company["しょくばらぼ_業種"].where(company["しょくばらぼ_業種"].ne(""), company["女性活躍DB_業種"]).map(strip_industry)
    company["詳細業種"] = company["詳細業種1"].astype(str).str.strip()
    company["企業規模"] = company["しょくばらぼ_企業規模"].where(company["しょくばらぼ_企業規模"].ne(""), company["女性活躍DB_企業規模"])

    overtime = pd.read_csv(OVERTIME_PATH, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    overtime["_key"] = overtime["企業名"].map(norm_name)
    for col in ["しょくばらぼでの残業", "女性進出ＤＢでの残業", "OpenWork", "転職会議", "キャリコネ"]:
        overtime[col] = to_num(overtime[col])
    overtime["提出残業平均"] = overtime[["しょくばらぼでの残業", "女性進出ＤＢでの残業"]].mean(axis=1)
    overtime["口コミ残業平均"] = overtime[["OpenWork", "転職会議", "キャリコネ"]].mean(axis=1)
    overtime["口コミ平均との差"] = overtime["口コミ残業平均"] - overtime["提出残業平均"]

    merged = gov.merge(
        company[["_key", "業種", "詳細業種", "企業規模", "平均年間給与", "平均年齢", "平均勤続年数"]],
        on="_key",
        how="left",
    ).merge(
        overtime[["_key", "提出残業平均", "口コミ残業平均", "口コミ平均との差"]],
        on="_key",
        how="left",
    )

    overall = pd.DataFrame(
        {
            "項目": [
                "件数",
                "業種結合件数",
                "提出/口コミ残業結合件数",
                "平均",
                "中央値",
                "標準偏差",
                "最小",
                "25pct",
                "75pct",
                "90pct",
                "最大",
                "10超件数",
                "-5未満件数",
                "指標と口コミ平均との差の相関",
            ],
            "値": [
                len(merged),
                int(merged["業種"].notna().sum()),
                int(merged["口コミ平均との差"].notna().sum()),
                merged["ガバナンス指標"].mean(),
                merged["ガバナンス指標"].median(),
                merged["ガバナンス指標"].std(),
                merged["ガバナンス指標"].min(),
                merged["ガバナンス指標"].quantile(0.25),
                merged["ガバナンス指標"].quantile(0.75),
                merged["ガバナンス指標"].quantile(0.90),
                merged["ガバナンス指標"].max(),
                int((merged["ガバナンス指標"] > 10).sum()),
                int((merged["ガバナンス指標"] < -5).sum()),
                merged[["ガバナンス指標", "口コミ平均との差"]].corr().iloc[0, 1],
            ],
        }
    )

    by_industry = summarize_group(merged, "業種", min_count=10)
    by_detail = summarize_group(merged, "詳細業種", min_count=8)
    top = merged.sort_values("ガバナンス指標", ascending=False).head(50)
    bottom = merged.sort_values("ガバナンス指標", ascending=True).head(30)

    merged.to_csv(OUT_DIR / "governance_indicator_joined.csv", index=False, encoding="utf-8-sig")
    overall.to_csv(OUT_DIR / "overall_summary.csv", index=False, encoding="utf-8-sig")
    by_industry.to_csv(OUT_DIR / "industry_summary.csv", index=False, encoding="utf-8-sig")
    by_detail.to_csv(OUT_DIR / "detail_industry_summary.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "top_concern_companies.csv", index=False, encoding="utf-8-sig")
    bottom.to_csv(OUT_DIR / "low_concern_companies.csv", index=False, encoding="utf-8-sig")

    print(overall.to_string(index=False))
    print("\n業種別上位")
    print(by_industry.head(15).to_string(index=False))
    print("\n詳細業種別上位")
    print(by_detail.head(20).to_string(index=False))
    print("\n企業上位")
    print(top[["企業名", "ガバナンス指標", "業種", "詳細業種", "提出残業平均", "口コミ残業平均", "口コミ平均との差"]].head(20).to_string(index=False))
    print(f"\noutput_dir={OUT_DIR}")


if __name__ == "__main__":
    main()
