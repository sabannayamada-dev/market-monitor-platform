from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "outputs" / "overtime_source_comparison_with_reviews.csv"
EXTRA = Path(r"C:\Users\saban\Downloads\企業名,OpenWork,転職会議,キャリコネ.txt")
OUT = ROOT / "outputs" / "overtime_source_comparison_with_reviews_filled.csv"
AUDIT = ROOT / "outputs" / "overtime_review_fill_audit.csv"


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", "", text)
    return text.replace("＆", "&")


def core_name(value: object) -> str:
    text = normalize_name(value)
    for token in ["株式会社", "有限会社", "・", "-", "－", "‐", "ー", "･", "&"]:
        text = text.replace(token, "")
    return re.sub(r"HD$", "", text)


def main() -> None:
    df = pd.read_csv(BASE, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    source = pd.read_csv(EXTRA, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    source = source[source["企業名"] != "企業名"].copy()
    source["_norm"] = source["企業名"].map(normalize_name)
    source["_core"] = source["企業名"].map(core_name)

    norm_map = {row["_norm"]: row for _, row in source.iterrows()}
    core_map = {}
    for _, row in source.iterrows():
        key = row["_core"]
        if key and key not in core_map:
            core_map[key] = row

    all_cores = list(core_map.keys())
    target_mask = df[["OpenWork", "転職会議", "キャリコネ"]].eq("").all(axis=1)
    fills = []

    for idx, row in df[target_mask].iterrows():
        name = row["企業名"]
        norm = normalize_name(name)
        core = core_name(name)
        matched = None
        method = ""
        score = ""

        if norm in norm_map:
            matched = norm_map[norm]
            method = "exact_norm"
            score = "1.000"
        elif core in core_map:
            matched = core_map[core]
            method = "exact_core"
            score = "1.000"
        else:
            candidates = difflib.get_close_matches(core, all_cores, n=1, cutoff=0.90)
            if candidates:
                ratio = difflib.SequenceMatcher(None, core, candidates[0]).ratio()
                matched = core_map[candidates[0]]
                method = "fuzzy_core"
                score = f"{ratio:.3f}"

        if matched is None:
            continue

        for col in ["OpenWork", "転職会議", "キャリコネ"]:
            df.at[idx, col] = matched[col]
        fills.append(
            {
                "企業名": name,
                "補完元企業名": matched["企業名"],
                "方法": method,
                "類似度": score,
                "OpenWork": matched["OpenWork"],
                "転職会議": matched["転職会議"],
                "キャリコネ": matched["キャリコネ"],
            }
        )

    remaining = df[df[["OpenWork", "転職会議", "キャリコネ"]].eq("").all(axis=1)][
        ["企業番号", "企業名"]
    ].copy()
    remaining["補完状況"] = "未補完: 添付TXT内に高信頼一致なし。Web検索でも数値が直接確認困難"

    audit = pd.concat([pd.DataFrame(fills), remaining], ignore_index=True, sort=False)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    audit.to_csv(AUDIT, index=False, encoding="utf-8-sig")

    print(f"filled={len(fills)}")
    print(f"remaining={len(remaining)}")
    print(OUT)
    print(AUDIT)


if __name__ == "__main__":
    main()
