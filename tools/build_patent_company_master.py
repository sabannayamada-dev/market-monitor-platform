from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "collections" / "collected_companies.csv"
MARKET_CAP_SOURCE = ROOT / "data" / "current_market_cap_snapshot.csv"
OUTPUT = ROOT / "patent_company_master.csv"
REPORT = ROOT / "patent_company_master_build_report.csv"
TARGET_COMPANY_COUNT = 450


# Detailed-industry labels are produced by the EDINET enrichment pipeline.  The
# scores intentionally favour businesses where one patent can alter products,
# manufacturing yield, licensing income, or competitive barriers.
PATENT_SENSITIVE_INDUSTRIES = {
    "半導体": (100, "半導体・製造装置"),
    "バイオ/創薬": (100, "バイオ・創薬"),
    "電子部品": (96, "電子部品"),
    "医薬品": (95, "バイオ・創薬"),
    "計測/制御": (94, "計測・制御"),
    "光学/精密機器": (94, "光学・量子・精密"),
    "重工/造船/航空": (92, "宇宙・防衛・重工"),
    "自動車部品": (90, "自動運転・次世代モビリティ"),
    "産業機器": (89, "ロボティクス・FA"),
    "機械": (87, "機械・製造技術"),
    "化学": (86, "化学・先端材料"),
    "素材/材料": (85, "電池・先端材料"),
    "鉄鋼/非鉄": (82, "金属・先端材料"),
    "自動車": (82, "自動運転・次世代モビリティ"),
    "プラント/エンジニアリング": (79, "プラント・脱炭素"),
    "鉄道/輸送機器": (78, "次世代モビリティ"),
    "ソフトウェア": (73, "AI・ソフトウェア"),
    "クラウド/SaaS": (69, "AI・ソフトウェア"),
    "SI/ITサービス": (66, "AI・ソフトウェア"),
    "電力": (62, "脱炭素・次世代エネルギー"),
    "石油/資源": (61, "脱炭素・次世代エネルギー"),
    "医療/介護": (58, "医療技術"),
}

TECHNOLOGY_KEYWORDS = {
    "半導体": "半導体・製造装置",
    "量子": "光学・量子・精密",
    "ロボット": "ロボティクス・FA",
    "人工知能": "AI・ソフトウェア",
    "生成AI": "AI・ソフトウェア",
    "サイバー": "サイバーセキュリティ",
    "セキュリティ": "サイバーセキュリティ",
    "電池": "電池・先端材料",
    "水素": "脱炭素・次世代エネルギー",
    "核融合": "脱炭素・次世代エネルギー",
    "宇宙": "宇宙・防衛",
    "衛星": "宇宙・防衛",
    "防衛": "宇宙・防衛",
    "創薬": "バイオ・創薬",
    "遺伝子": "バイオ・創薬",
    "医療機器": "医療機器",
    "自動運転": "自動運転・次世代モビリティ",
    "光学": "光学・量子・精密",
}


GROUPS = {
    "半導体・製造装置": "3436 4062 4063 4186 4369 4971 4975 4980 5384 6146 6228 6254 6264 6315 6323 6387 6521 6525 6526 6590 6613 6616 6627 6668 6677 6707 6723 6728 6758 6762 6779 6817 6834 6857 6871 6890 6920 6925 6941 6963 6965 6967 6971 6981 6988 7729 7735 8035",
    "AI・ソフトウェア": "2326 3655 3692 3774 3778 3905 3914 3993 4055 4180 4259 4268 4382 4413 4443 4475 4684 4812 5574 5582 5591 6701 6702",
    "サイバーセキュリティ": "2326 3040 3692 3857 4288 4493 4704",
    "ロボティクス・FA": "6103 6113 6134 6141 6232 6273 6324 6383 6407 6457 6479 6481 6501 6503 6504 6506 6594 6645 6841 6861 6954 7011 7012 7013 7224",
    "自動運転・次世代モビリティ": "3116 6201 6902 6995 7201 7202 7203 7211 7259 7261 7267 7269 7270 7272 7313",
    "電池・先端材料": "3401 3402 3405 3407 4004 4005 4021 4042 4043 4061 4080 4091 4107 4114 4183 4188 4203 4204 4272 4368 4401 4403 4626 4970 5019 5214 5333 5711 5713 5801 5802 5803 6674",
    "バイオ・創薬": "2160 219A 2395 4151 4502 4503 4506 4507 4519 4523 4527 4528 4530 4536 4543 4565 4568 4571 4578 4587 4592 4593 4594 4882 4883 4886 4887 4888 4892 4894 4974",
    "光学・量子・精密": "4901 4902 6613 6758 6861 6869 6925 6965 7701 7713 7731 7733 7741 7747 7751 7780 8086 9432",
    "宇宙・防衛": "186A 5595 6208 6501 6701 6702 6758 6946 7011 7012 7013 7224 7721 9348",
    "脱炭素・次世代エネルギー": "1407 1605 4107 5019 5020 6504 6674 7003 7011 9513 9517 9519",
    "次世代通信": "3774 5801 5802 5803 6701 6702 6754 6778 6800 6838 9432 9433 9434",
    "医療機器": "4543 6869 7701 7733 7741 7747 7780 8086",
}


ALIASES = {
    "4063": ["SHIN-ETSU CHEMICAL CO LTD"],
    "6146": ["DISCO CORPORATION"],
    "6501": ["HITACHI LTD"],
    "6503": ["MITSUBISHI ELECTRIC CORPORATION"],
    "6506": ["YASKAWA ELECTRIC CORPORATION"],
    "6594": ["NIDEC CORPORATION"],
    "6701": ["NEC CORPORATION", "NIPPON ELECTRIC CO LTD"],
    "6702": ["FUJITSU LIMITED"],
    "6723": ["RENESAS ELECTRONICS CORPORATION"],
    "6758": ["SONY GROUP CORPORATION", "SONY CORPORATION"],
    "6762": ["TDK CORPORATION"],
    "6857": ["ADVANTEST CORPORATION"],
    "6861": ["KEYENCE CORPORATION"],
    "6902": ["DENSO CORPORATION"],
    "6920": ["LASERTEC CORPORATION"],
    "6954": ["FANUC CORPORATION"],
    "6963": ["ROHM CO LTD"],
    "6965": ["HAMAMATSU PHOTONICS KK"],
    "6971": ["KYOCERA CORPORATION"],
    "6981": ["MURATA MANUFACTURING CO LTD"],
    "7011": ["MITSUBISHI HEAVY INDUSTRIES LTD"],
    "7012": ["KAWASAKI HEAVY INDUSTRIES LTD"],
    "7013": ["IHI CORPORATION"],
    "7201": ["NISSAN MOTOR CO LTD"],
    "7203": ["TOYOTA MOTOR CORPORATION"],
    "7259": ["AISIN CORPORATION"],
    "7261": ["MAZDA MOTOR CORPORATION"],
    "7267": ["HONDA MOTOR CO LTD"],
    "7269": ["SUZUKI MOTOR CORPORATION"],
    "7270": ["SUBARU CORPORATION"],
    "7731": ["NIKON CORPORATION"],
    "7733": ["OLYMPUS CORPORATION"],
    "7741": ["HOYA CORPORATION"],
    "7751": ["CANON INC"],
    "8035": ["TOKYO ELECTRON LIMITED"],
    "9432": ["NIPPON TELEGRAPH AND TELEPHONE CORPORATION", "NTT CORPORATION"],
    "9433": ["KDDI CORPORATION"],
    "9434": ["SOFTBANK CORP"],
    "3402": ["TORAY INDUSTRIES INC"],
    "3407": ["ASAHI KASEI CORPORATION"],
    "4005": ["SUMITOMO CHEMICAL CO LTD"],
    "4183": ["MITSUI CHEMICALS INC"],
    "4502": ["TAKEDA PHARMACEUTICAL COMPANY LIMITED"],
    "4503": ["ASTELLAS PHARMA INC"],
    "4507": ["SHIONOGI AND CO LTD"],
    "4519": ["CHUGAI PHARMACEUTICAL CO LTD"],
    "4523": ["EISAI CO LTD"],
    "4568": ["DAIICHI SANKYO COMPANY LIMITED"],
    "4587": ["PEPTIDREAM INC"],
    "4901": ["FUJIFILM HOLDINGS CORPORATION"],
}


SUBSIDIARIES = {
    "4004": ["株式会社レゾナック", "レゾナック株式会社"],
    "4188": ["三菱ケミカル株式会社"],
    "4578": ["大塚製薬株式会社"],
    "4901": ["富士フイルム株式会社"],
    "5020": ["ＥＮＥＯＳ株式会社", "ENEOS株式会社"],
    "6758": ["ソニー株式会社"],
    "6890": ["株式会社フェローテック"],
    "7735": ["株式会社ＳＣＲＥＥＮセミコンダクターソリューションズ"],
}


def normalize_code(value: str) -> str:
    code = str(value or "").strip().upper().replace(".T", "")
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]
    return code


def read_latest_companies() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = normalize_code(row.get("証券コード", ""))
            if not code or not row.get("企業名"):
                continue
            if code not in rows or row.get("提出日", "") > rows[code].get("提出日", ""):
                rows[code] = row
    return rows


def read_market_caps() -> dict[str, float]:
    result: dict[str, float] = {}
    if not MARKET_CAP_SOURCE.exists():
        return result
    with MARKET_CAP_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = normalize_code(row.get("銘柄", "") or row.get("入力値", ""))
            try:
                value = float(str(row.get("現在時価総額(億円)", "")).replace(",", "")) * 100_000_000
            except ValueError:
                continue
            if code and value > 0:
                result[code] = value
    return result


def numeric(value: str) -> float:
    text = re.sub(r"[^0-9.\-]", "", str(value or ""))
    try:
        return float(text)
    except ValueError:
        return 0.0


def candidate_score(row: dict[str, str], market_cap: float) -> tuple[float, set[str], list[str]]:
    industries = [row.get(f"詳細業種{index}", "") for index in range(1, 4)]
    matches = [PATENT_SENSITIVE_INDUSTRIES[item] for item in industries if item in PATENT_SENSITIVE_INDUSTRIES]
    if not matches:
        return 0.0, set(), []
    score, primary_tag = max(matches, key=lambda item: item[0])
    tags = {primary_tag}
    reasons = [f"詳細業種={industries[0] or primary_tag}"]

    research_text = (row.get("研究開発活動", "") or "").strip()
    business_text = " ".join((row.get("企業名", ""), row.get("事業内容", ""), research_text))
    if research_text:
        score += 8
        reasons.append("研究開発活動あり")
    research_cost = numeric(row.get("研究開発費", ""))
    sales = numeric(row.get("売上高・営業収益", ""))
    if research_cost > 0:
        score += 6
        reasons.append("研究開発費あり")
        if sales > 0 and research_cost / sales >= 0.03:
            score += 5
            reasons.append("研究開発比率3%以上")
    for keyword, tag in TECHNOLOGY_KEYWORDS.items():
        if keyword in business_text:
            tags.add(tag)
            score += 3
    if market_cap > 0:
        # Scale is useful for materiality and name matching, but capped so that
        # innovative small/mid caps are not displaced by large incumbents.
        score += min(10.0, max(0.0, math.log10(market_cap / 100_000_000 + 1) * 2.2))
    return score, tags, reasons


def main() -> None:
    tags: dict[str, set[str]] = defaultdict(set)
    for tag, codes in GROUPS.items():
        for code in codes.split():
            tags[code].add(tag)

    companies = read_latest_companies()
    market_caps = read_market_caps()
    selected: list[dict[str, object]] = []
    report: list[dict[str, str]] = []
    cutoff = "2025-01-01"
    for code in sorted(tags):
        source = companies.get(code)
        if not source:
            report.append({"ticker": code, "status": "not_found_in_edinet_master", "company_name": "", "latest_filing": ""})
            continue
        filing = source.get("提出日", "")
        if filing and filing < cutoff:
            report.append({"ticker": code, "status": "stale_filing_excluded", "company_name": source["企業名"], "latest_filing": filing})
            continue
        selected.append({
            "company_id": f"JP{code}",
            "company_name": source["企業名"],
            "ticker": code,
            "aliases": "|".join(ALIASES.get(code, [])),
            "subsidiaries": "|".join(SUBSIDIARIES.get(code, [])),
            "market_cap_jpy": int(market_caps.get(code, 0)),
            "target": 1,
            "technology_tags": "|".join(sorted(tags[code])),
        })
        report.append({"ticker": code, "status": "included_curated", "company_name": source["企業名"], "latest_filing": filing, "selection_score": "", "selection_reason": "既存監視対象"})

    selected_codes = {str(row["ticker"]) for row in selected}
    expansion_candidates: list[tuple[float, str, dict[str, str], set[str], list[str]]] = []
    for code, source in companies.items():
        if code in selected_codes:
            continue
        filing = source.get("提出日", "")
        if filing and filing < cutoff:
            continue
        score, inferred_tags, reasons = candidate_score(source, market_caps.get(code, 0))
        if score <= 0:
            continue
        expansion_candidates.append((score, code, source, inferred_tags, reasons))

    expansion_candidates.sort(key=lambda item: (-item[0], -market_caps.get(item[1], 0), item[1]))
    needed = max(0, TARGET_COMPANY_COUNT - len(selected))
    for score, code, source, inferred_tags, reasons in expansion_candidates[:needed]:
        selected.append({
            "company_id": f"JP{code}",
            "company_name": source["企業名"],
            "ticker": code,
            "aliases": "|".join(ALIASES.get(code, [])),
            "subsidiaries": "|".join(SUBSIDIARIES.get(code, [])),
            "market_cap_jpy": int(market_caps.get(code, 0)),
            "target": 1,
            "technology_tags": "|".join(sorted(inferred_tags)),
        })
        report.append({
            "ticker": code,
            "status": "included_scored_expansion",
            "company_name": source["企業名"],
            "latest_filing": source.get("提出日", ""),
            "selection_score": f"{score:.2f}",
            "selection_reason": "|".join(reasons),
        })

    selected.sort(key=lambda row: str(row["ticker"]))

    columns = ["company_id", "company_name", "ticker", "aliases", "subsidiaries", "market_cap_jpy", "target", "technology_tags"]
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(selected)
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        report_columns = ["ticker", "status", "company_name", "latest_filing", "selection_score", "selection_reason"]
        writer = csv.DictWriter(handle, fieldnames=report_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report)
    print(
        f"generated={len(selected)} curated={len(selected_codes)} "
        f"expanded={max(0, len(selected) - len(selected_codes))} report={REPORT}"
    )


if __name__ == "__main__":
    main()
