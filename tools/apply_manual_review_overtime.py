from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "outputs" / "overtime_source_comparison_with_reviews_filled.csv"
if not BASE.exists():
    BASE = ROOT / "outputs" / "overtime_source_comparison_with_reviews.csv"
ADDITIONS = ROOT / "inputs" / "manual_review_overtime_additions.csv"
OUT = ROOT / "outputs" / "overtime_source_comparison_with_reviews_manual_filled.csv"
AUDIT = ROOT / "outputs" / "overtime_manual_review_apply_audit.csv"


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", "", text)
    return text.replace("＆", "&")


def core_name(value: object) -> str:
    text = normalize_name(value)
    for token in ["株式会社", "有限会社", "ホールディングス", "・", "-", "－", "‐", "ー", "･", "&"]:
        text = text.replace(token, "")
    return re.sub(r"HD$", "", text)


def main() -> None:
    df = pd.read_csv(BASE, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    additions = pd.read_csv(ADDITIONS, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    additions["_norm"] = additions["企業名"].map(normalize_name)
    additions["_core"] = additions["企業名"].map(core_name)

    norm_map = {row["_norm"]: row for _, row in additions.iterrows()}
    core_map = {}
    for _, row in additions.iterrows():
        key = row["_core"]
        if key and key not in core_map:
            core_map[key] = row

    fills = []
    for idx, row in df.iterrows():
        if not all(str(row[col]).strip() == "" for col in ["OpenWork", "転職会議", "キャリコネ"]):
            continue
        name = row["企業名"]
        matched = None
        method = ""
        norm = normalize_name(name)
        core = core_name(name)
        if norm in norm_map:
            matched = norm_map[norm]
            method = "manual_exact_norm"
        elif core in core_map:
            matched = core_map[core]
            method = "manual_exact_core"
        if matched is None:
            continue
        for col in ["OpenWork", "転職会議", "キャリコネ"]:
            df.at[idx, col] = matched[col]
        fills.append(
            {
                "企業番号": row["企業番号"],
                "企業名": name,
                "補完元企業名": matched["企業名"],
                "方法": method,
                "OpenWork": matched["OpenWork"],
                "転職会議": matched["転職会議"],
                "キャリコネ": matched["キャリコネ"],
            }
        )

    remaining = df[df[["OpenWork", "転職会議", "キャリコネ"]].eq("").all(axis=1)][
        ["企業番号", "企業名"]
    ].copy()
    remaining["補完状況"] = "未補完"
    audit = pd.concat([pd.DataFrame(fills), remaining], ignore_index=True, sort=False)

    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    audit.to_csv(AUDIT, index=False, encoding="utf-8-sig")
    print(f"manual_rows={len(additions)}")
    print(f"filled={len(fills)}")
    print(f"remaining={len(remaining)}")
    print(OUT)
    print(AUDIT)


if __name__ == "__main__":
    main()
