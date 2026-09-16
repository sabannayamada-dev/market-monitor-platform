from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "outputs" / "governance_indicator_analysis" / "governance_indicator_joined.csv"

TARGETS = [
    "ヒューリック",
    "日本郵船",
    "中外製薬",
    "中部日本放送",
    "INPEX",
    "飯野海運",
    "SRAホールディングス",
    "三菱ケミカル",
    "電源開発",
    "コスモエネルギーホールディングス",
    "ENEOS",
    "レゾナック・ホールディングス",
    "アステラス製薬",
    "エーザイ",
]


ALIASES = {
    "INPEX": ["INPEX", "ＩＮＰＥＸ"],
    "ENEOS": ["ENEOS", "ＥＮＥＯＳ", "ＥＮＥＯＳホールディングス"],
    "レゾナック・ホールディングス": ["レゾナック", "れぞなっく", "RESONAC"],
    "SRAホールディングス": ["SRA", "ＳＲＡ"],
}


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", "", text)
    for token in ["株式会社", "ホールディングス", "・", "-", "－", "HD"]:
        text = text.replace(token, "")
    return text.upper()


def main() -> None:
    df = pd.read_csv(DATA, encoding="utf-8-sig")
    df["_n"] = df["企業名"].map(norm)
    rows = []
    for target in TARGETS:
        keys = [target] + ALIASES.get(target, [])
        norm_keys = [norm(k) for k in keys]
        mask = pd.Series(False, index=df.index)
        for key in norm_keys:
            mask |= df["_n"].str.contains(key, na=False)
            mask |= df["_n"].map(lambda x: x in key or key in x)
        matched = df[mask].copy()
        if matched.empty:
            rows.append({"検索名": target, "企業名": "NOT_FOUND"})
        else:
            row = matched.sort_values("ガバナンス指標").iloc[0].to_dict()
            row["検索名"] = target
            rows.append(row)
    out = pd.DataFrame(rows)
    cols = [
        "検索名",
        "企業名",
        "ガバナンス指標",
        "業種",
        "詳細業種",
        "平均年間給与",
        "平均年齢",
        "平均勤続年数",
        "提出残業平均",
        "口コミ残業平均",
        "口コミ平均との差",
    ]
    print(out[[c for c in cols if c in out.columns]].to_string(index=False))


if __name__ == "__main__":
    main()
