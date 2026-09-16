from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app_meta import APP_BUILD_DATE, APP_NAME, APP_VERSION


DEFAULT_COMPANY_FILE = Path("outputs/collections/collected_companies.csv")
DEFAULT_RANKING_FILE = Path("outputs/ranking.csv")
DEFAULT_OUTPUT_DIR = Path("outputs/shokuba_enrichment")
SHOKUBA_ENCODING = "cp932"

CORPORATE_NUMBER_ALIASES = ["corporate_number", "法人番号", "法人番号13桁", "hojin_bangho"]
COMPANY_NAME_ALIASES = ["company_name", "企業名", "会社名", "filer_name"]
SECURITY_CODE_ALIASES = ["sec_code", "security_code", "証券コード", "証券コード協議会コード"]
TOTAL_SCORE_ALIASES = ["total_score", "Total_Score", "総合スコア", "スコア"]

BASE_SHOKUBA_COLUMNS = [
    "法人番号",
    "企業名",
    "都道府県",
    "所在地",
    "企業規模",
    "企業規模詳細",
    "業種",
    "企業ホームページ",
    "採用ページ",
    "電話",
    "創業年",
    "証券コード",
    "市場区分",
    "ハローワークインターネットサービスの求人掲載-一般求人（フルタイム）",
    "ハローワークインターネットサービスの求人掲載-一般求人（パートタイム）",
    "ハローワークインターネットサービスの求人掲載-新卒・既卒求人",
]

KEYWORD_PATTERNS = [
    "有給",
    "休暇",
    "残業",
    "時間外",
    "所定外",
    "休日労働",
    "36協定",
    "育児",
    "介護",
    "賃金",
    "女性",
    "管理職",
    "役員",
    "テレワーク",
    "副業",
    "兼業",
    "正社員",
    "継続勤務",
    "継続雇用",
    "離職",
    "定着",
    "採用",
    "従業員",
    "平均年齢",
    "研修",
    "メンター",
    "自己啓発",
    "キャリア",
    "検定",
    "インターン",
    "職場見学",
    "職場体験",
    "社会保険",
    "定年",
    "くるみん",
    "えるぼし",
    "ユースエール",
    "健康経営",
    "雇用形態",
    "転換",
    "多様な正社員",
]

NUMERIC_HELPER_COLUMNS = [
    "正社員の有給休暇取得日数",
    "年次有給休暇取得率（全体）-取得率",
    "年次有給休暇取得率（雇用管理区分）-取得率（一覧）",
    "月平均所定外労働時間",
    "対象労働者全体の月平均の法定時間外労働時間と法定休日労働時間の合計-平均残業時間（詳細）",
    "平均の法定時間外労働60時間以上の労働者の数",
    "男女の賃金の差異-全労働者",
    "男女の賃金の差異-うち正規雇用労働者",
    "男女の賃金の差異-うち非正規雇用労働者",
    "正社員の平均継続勤務年数",
    "従業員の平均年齢",
    "管理職に占める女性の割合",
    "役員に占める女性の割合",
    "育児休業取得率（男性）-男性取得率（一覧）",
    "育児休業取得率（女性）-女性取得率（一覧）",
    "中途採用比率(前年度/2年度前/3年度前)",
]

SUMMARY_PREFERRED_COLUMNS = [
    "corporate_number",
    "法人番号",
    "company_name",
    "企業名",
    "total_score",
    "総合スコア",
    "average_annual_salary",
    "平均年間給与",
    "average_length_of_service",
    "平均勤続年数",
    "number_of_employees",
    "従業員数",
    "detailed_industry_1",
    "詳細業種1",
    "detailed_industry_1_score",
    "詳細業種1信頼度",
    "detailed_industry_2",
    "詳細業種2",
    "detailed_industry_2_score",
    "詳細業種2信頼度",
    "detailed_industry_3",
    "詳細業種3",
    "detailed_industry_3_score",
    "詳細業種3信頼度",
    "detailed_industry_candidates",
    "詳細業種候補",
    "detailed_industry_evidence_keywords",
    "詳細業種根拠キーワード",
    "しょくばらぼ_JOIN",
    "しょくばらぼ_結合方法",
    "しょくばらぼ_取得項目数",
    "しょくばらぼ_企業名",
    "しょくばらぼ_都道府県",
    "しょくばらぼ_所在地",
    "しょくばらぼ_企業規模",
    "しょくばらぼ_企業規模詳細",
    "しょくばらぼ_業種",
    "しょくばらぼ_市場区分",
    "しょくばらぼ_証券コード",
    "しょくばらぼ_企業ホームページ",
    "しょくばらぼ_採用ページ",
    "しょくばらぼ_正社員の有給休暇取得日数",
    "しょくばらぼ_正社員の有給休暇取得日数_数値",
    "しょくばらぼ_年次有給休暇取得率（全体）-取得率",
    "しょくばらぼ_年次有給休暇取得率（全体）-取得率_数値",
    "しょくばらぼ_年次有給休暇取得率（雇用管理区分）-取得率（一覧）",
    "しょくばらぼ_月平均所定外労働時間",
    "しょくばらぼ_月平均所定外労働時間_数値",
    "しょくばらぼ_対象労働者全体の月平均の法定時間外労働時間と法定休日労働時間の合計-平均残業時間（詳細）",
    "しょくばらぼ_対象労働者全体の月平均の法定時間外労働時間と法定休日労働時間の合計-平均残業時間（詳細）_数値",
    "しょくばらぼ_平均の法定時間外労働60時間以上の労働者の数",
    "しょくばらぼ_男女の賃金の差異-全労働者",
    "しょくばらぼ_男女の賃金の差異-全労働者_数値",
    "しょくばらぼ_男女の賃金の差異-うち正規雇用労働者",
    "しょくばらぼ_男女の賃金の差異-うち非正規雇用労働者",
    "しょくばらぼ_管理職に占める女性の割合",
    "しょくばらぼ_管理職に占める女性の割合_数値",
    "しょくばらぼ_役員に占める女性の割合",
    "しょくばらぼ_役員に占める女性の割合_数値",
    "しょくばらぼ_育児休業取得率（男性）-男性取得率（一覧）",
    "しょくばらぼ_育児休業取得率（女性）-女性取得率（一覧）",
    "しょくばらぼ_テレワーク制度-可否",
    "しょくばらぼ_テレワーク制度-制度内容",
    "しょくばらぼ_副業・兼業-可否",
    "しょくばらぼ_副業・兼業-副業・兼業にあたっての留意事項等",
    "しょくばらぼ_多様な正社員制度-制度-職務限定正社員",
    "しょくばらぼ_多様な正社員制度-制度-勤務地限定正社員",
    "しょくばらぼ_多様な正社員制度-制度-短時間正社員",
    "しょくばらぼ_正社員の平均継続勤務年数",
    "しょくばらぼ_正社員の平均継続勤務年数_数値",
    "しょくばらぼ_従業員の平均年齢",
    "しょくばらぼ_従業員の平均年齢_数値",
    "しょくばらぼ_中途採用比率(前年度/2年度前/3年度前)",
    "しょくばらぼ_中途採用比率(前年度/2年度前/3年度前)_数値",
    "しょくばらぼ_研修制度-有無",
    "しょくばらぼ_研修制度-内容",
    "しょくばらぼ_メンター制度-有無",
    "しょくばらぼ_自己啓発支援制度-有無",
    "しょくばらぼ_自己啓発支援制度-内容",
    "しょくばらぼ_キャリアコンサルティング制度-有無",
    "しょくばらぼ_キャリアコンサルティング制度-内容",
    "しょくばらぼ_社内検定制度-有無",
    "しょくばらぼ_社内検定制度-内容",
    "しょくばらぼ_インターンシップの受入-可否",
    "しょくばらぼ_職場見学・職場体験の受入-可否",
    "しょくばらぼ_えるぼし認定-認定有無",
    "しょくばらぼ_えるぼし認定-認定段階",
    "しょくばらぼ_くるみん-認定有無",
    "しょくばらぼ_くるみん-認定状況",
    "しょくばらぼ_健康経営銘柄-認定有無",
]


def normalize_corporate_number(value: object) -> str:
    text = "" if value is None else str(value)
    if "e+" in text.lower() or "e-" in text.lower():
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 13:
        return digits
    return ""


def normalize_security_code(value: object) -> str:
    text = "" if value is None else str(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 5 and digits.endswith("0"):
        return digits[:4]
    if len(digits) == 4:
        return digits
    return ""


def normalize_company_name_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).upper()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[()（）・,，.．/／\\\-ー－_＿]", "", text)
    for token in ["株式会社", "有限会社", "合同会社", "合名会社", "合資会社", "㈱", "（株）", "(株)", "K.K.", "KK"]:
        text = text.replace(token.upper(), "")
    return text


def detect_encoding(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            pd.read_csv(path, encoding=encoding, nrows=1, dtype=str)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def find_column(columns: list[str], aliases: list[str]) -> str:
    normalized = {str(column).strip(): column for column in columns}
    lower = {str(column).strip().lower(): column for column in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
        if alias.lower() in lower:
            return lower[alias.lower()]
    return ""


def read_csv_flexible(path: Path) -> pd.DataFrame:
    encoding = detect_encoding(path)
    return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)


def shokuba_columns(path: Path) -> list[str]:
    frame = pd.read_csv(path, encoding=SHOKUBA_ENCODING, nrows=0, dtype=str)
    return [str(column) for column in frame.columns]


def select_shokuba_columns(columns: list[str]) -> list[str]:
    selected: list[str] = []
    for column in columns:
        if column in BASE_SHOKUBA_COLUMNS or any(pattern in column for pattern in KEYWORD_PATTERNS):
            selected.append(column)
    if "法人番号" not in selected:
        selected.insert(0, "法人番号")
    return selected


def nonempty_count(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    if not columns:
        return pd.Series([0] * len(frame), index=frame.index)
    return frame[columns].fillna("").astype(str).apply(lambda row: sum(bool(value.strip()) for value in row), axis=1)


def parse_first_number(value: object) -> float | None:
    text = "" if value is None else str(value)
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def add_numeric_helpers(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in NUMERIC_HELPER_COLUMNS:
        prefixed = f"しょくばらぼ_{column}"
        if prefixed in output.columns:
            output[f"{prefixed}_数値"] = output[prefixed].map(parse_first_number)
    return output


def build_shokuba_lookup(
    shokuba_file: Path,
    selected_columns: list[str],
    target_corporate_numbers: set[str] | None = None,
    target_security_codes: set[str] | None = None,
    target_company_names: set[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    usecols = [column for column in selected_columns if column]
    target_corporate_numbers = target_corporate_numbers or set()
    target_security_codes = target_security_codes or set()
    target_company_names = target_company_names or set()
    if target_corporate_numbers or target_security_codes or target_company_names:
        key_columns = [column for column in ["法人番号", "証券コード", "企業名"] if column in usecols]
        key_frame = pd.read_csv(shokuba_file, encoding=SHOKUBA_ENCODING, dtype=str, usecols=key_columns, keep_default_na=False)
        key_frame["法人番号_norm"] = key_frame["法人番号"].map(normalize_corporate_number) if "法人番号" in key_frame.columns else ""
        key_frame["証券コード_norm"] = key_frame["証券コード"].map(normalize_security_code) if "証券コード" in key_frame.columns else ""
        key_frame["企業名_norm"] = key_frame["企業名"].map(normalize_company_name_key) if "企業名" in key_frame.columns else ""
        candidate_mask = (
            key_frame["法人番号_norm"].isin(target_corporate_numbers)
            | key_frame["証券コード_norm"].isin(target_security_codes)
            | key_frame["企業名_norm"].isin(target_company_names)
        )
        keep_row_numbers = set((key_frame.index[candidate_mask] + 1).tolist())
        if keep_row_numbers:
            frame = pd.read_csv(
                shokuba_file,
                encoding=SHOKUBA_ENCODING,
                dtype=str,
                usecols=usecols,
                keep_default_na=False,
                skiprows=lambda row_number: row_number > 0 and row_number not in keep_row_numbers,
            )
        else:
            frame = pd.DataFrame(columns=usecols)
    else:
        frame = pd.read_csv(shokuba_file, encoding=SHOKUBA_ENCODING, dtype=str, usecols=usecols, keep_default_na=False)
    frame["法人番号_norm"] = frame["法人番号"].map(normalize_corporate_number)
    if "証券コード" in frame.columns:
        frame["証券コード_norm"] = frame["証券コード"].map(normalize_security_code)
    else:
        frame["証券コード_norm"] = ""
    if "企業名" in frame.columns:
        frame["企業名_norm"] = frame["企業名"].map(normalize_company_name_key)
    else:
        frame["企業名_norm"] = ""
    frame = frame[frame["法人番号_norm"].ne("")].copy()
    metric_columns = [column for column in frame.columns if column not in {"法人番号", "法人番号_norm", "証券コード_norm", "企業名_norm"}]
    frame["_shokuba_nonempty_metric_count"] = nonempty_count(frame, metric_columns)
    duplicate_count = int(frame.duplicated("法人番号_norm", keep=False).sum())
    frame = frame.sort_values(["法人番号_norm", "_shokuba_nonempty_metric_count"], ascending=[True, False])
    frame = frame.drop_duplicates("法人番号_norm", keep="first").copy()
    report = {
        "shokuba_unique_corporate_number_count": int(len(frame)),
        "shokuba_security_code_count": int(frame["証券コード_norm"].ne("").sum()),
        "shokuba_company_name_key_count": int(frame["企業名_norm"].ne("").sum()),
        "shokuba_duplicate_row_count": duplicate_count,
        "selected_shokuba_column_count": int(len(selected_columns)),
    }
    return frame, report


def write_matched_raw_shokuba(
    shokuba_file: Path,
    target_numbers: set[str],
    output_path: Path,
    chunksize: int = 20000,
) -> int:
    if not target_numbers:
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        return 0
    key_frame = pd.read_csv(shokuba_file, encoding=SHOKUBA_ENCODING, dtype=str, usecols=["法人番号"], keep_default_na=False)
    key_frame["法人番号_norm"] = key_frame["法人番号"].map(normalize_corporate_number)
    keep_row_numbers = set((key_frame.index[key_frame["法人番号_norm"].isin(target_numbers)] + 1).tolist())
    if not keep_row_numbers:
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        return 0
    matched = pd.read_csv(
        shokuba_file,
        encoding=SHOKUBA_ENCODING,
        dtype=str,
        keep_default_na=False,
        skiprows=lambda row_number: row_number > 0 and row_number not in keep_row_numbers,
    )
    matched.to_csv(output_path, index=False, encoding="utf-8-sig")
    return int(len(matched))


INTERNAL_SHOKUBA_COLUMNS = {"法人番号_norm", "証券コード_norm", "企業名_norm", "_shokuba_nonempty_metric_count"}


def fill_unmatched_from_lookup(
    output: pd.DataFrame,
    prefixed_lookup: pd.DataFrame,
    key_column: str,
    data_columns: list[str],
    method: str,
    unique_only: bool = False,
) -> pd.DataFrame:
    if key_column not in output.columns or key_column not in prefixed_lookup.columns:
        return output
    unmatched = output["_shokuba_nonempty_metric_count"].isna() & output[key_column].fillna("").astype(str).ne("")
    if not unmatched.any():
        return output
    lookup = prefixed_lookup[prefixed_lookup[key_column].fillna("").astype(str).ne("")].copy()
    if lookup.empty:
        return output
    if unique_only:
        duplicated = lookup.duplicated(key_column, keep=False)
        lookup = lookup.loc[~duplicated].copy()
    else:
        lookup = lookup.sort_values([key_column, "_shokuba_nonempty_metric_count"], ascending=[True, False])
        lookup = lookup.drop_duplicates(key_column, keep="first")
    if lookup.empty:
        return output

    lookup_index_by_key = pd.Series(lookup.index, index=lookup[key_column].astype(str)).to_dict()
    target_keys = output.loc[unmatched, key_column].astype(str)
    matched_lookup_indexes = target_keys.map(lookup_index_by_key)
    matched_mask = matched_lookup_indexes.notna()
    if not bool(matched_mask.any()):
        return output
    target_indexes = matched_lookup_indexes.index[matched_mask]
    source_indexes = matched_lookup_indexes.loc[matched_mask].astype(int).to_list()
    matched_values = lookup.loc[source_indexes, ["_shokuba_nonempty_metric_count", *data_columns]].reset_index(drop=True)
    for column in ["_shokuba_nonempty_metric_count", *data_columns]:
        output.loc[target_indexes, column] = matched_values[column].to_numpy()
    output.loc[target_indexes, "しょくばらぼ_結合方法"] = method
    return output


def enrich_frame(
    base: pd.DataFrame,
    shokuba_lookup: pd.DataFrame,
    base_corporate_column: str,
    base_security_column: str = "",
    base_company_column: str = "",
) -> pd.DataFrame:
    work = base.copy()
    work["法人番号_norm"] = work[base_corporate_column].map(normalize_corporate_number)
    work["証券コード_norm"] = work[base_security_column].map(normalize_security_code) if base_security_column else ""
    work["企業名_norm"] = work[base_company_column].map(normalize_company_name_key) if base_company_column else ""
    prefixed = shokuba_lookup.copy()
    rename_map = {
        column: f"しょくばらぼ_{column}"
        for column in prefixed.columns
        if column not in INTERNAL_SHOKUBA_COLUMNS
    }
    prefixed = prefixed.rename(columns=rename_map)
    data_columns = [column for column in prefixed.columns if column not in INTERNAL_SHOKUBA_COLUMNS]
    corporate_lookup = prefixed[["法人番号_norm", "_shokuba_nonempty_metric_count", *data_columns]].copy()
    output = work.merge(corporate_lookup, on="法人番号_norm", how="left")
    output["しょくばらぼ_結合方法"] = output["_shokuba_nonempty_metric_count"].notna().map(lambda value: "corporate_number" if value else "")
    output = fill_unmatched_from_lookup(output, prefixed, "証券コード_norm", data_columns, "security_code")
    output = fill_unmatched_from_lookup(output, prefixed, "企業名_norm", data_columns, "company_name", unique_only=True)
    output["しょくばらぼ_JOIN"] = output["_shokuba_nonempty_metric_count"].notna().map(lambda value: "matched" if value else "not_found")
    output["しょくばらぼ_取得項目数"] = output["_shokuba_nonempty_metric_count"].fillna(0).astype(int)
    output = add_numeric_helpers(output)
    return output.drop(columns=["法人番号_norm", "証券コード_norm", "企業名_norm", "_shokuba_nonempty_metric_count"], errors="ignore")


def metric_coverage(enriched: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    target_columns = [
        column
        for column in enriched.columns
        if column.startswith("しょくばらぼ_") and column not in {"しょくばらぼ_JOIN", "しょくばらぼ_結合方法", "しょくばらぼ_取得項目数"}
    ]
    total = len(enriched)
    for column in target_columns:
        present = int(enriched[column].fillna("").astype(str).map(lambda value: bool(value.strip())).sum())
        rows.append(
            {
                "項目名": column,
                "取得件数": present,
                "対象件数": total,
                "取得率": round(present / total, 4) if total else 0,
            }
        )
    return pd.DataFrame(rows).sort_values(["取得件数", "項目名"], ascending=[False, True])


def maybe_build_top_n(enriched: pd.DataFrame, top_n: int) -> pd.DataFrame:
    score_column = find_column(list(enriched.columns), TOTAL_SCORE_ALIASES)
    if not score_column:
        return enriched.head(top_n).copy()
    work = enriched.copy()
    work["_score_sort"] = pd.to_numeric(work[score_column], errors="coerce")
    work = work.sort_values("_score_sort", ascending=False, na_position="last")
    return work.drop(columns=["_score_sort"]).head(top_n).copy()


def summary_metrics_frame(enriched: pd.DataFrame) -> pd.DataFrame:
    columns = [column for column in SUMMARY_PREFERRED_COLUMNS if column in enriched.columns]
    seen: set[str] = set()
    unique_columns = []
    for column in columns:
        if column not in seen:
            unique_columns.append(column)
            seen.add(column)
    return enriched[unique_columns].copy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EDINET収集済み企業CSVに、しょくばらぼ職場情報を法人番号で結合します")
    parser.add_argument("--shokuba-file", required=True, help="しょくばらぼCSV一括ダウンロードファイル")
    parser.add_argument("--company-file", default=str(DEFAULT_COMPANY_FILE), help="強化対象の企業CSV")
    parser.add_argument("--ranking-file", default=str(DEFAULT_RANKING_FILE), help="任意のランキングCSV。存在すれば上位N社版を作ります")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="出力フォルダ")
    parser.add_argument("--top-n", type=int, default=50, help="ランキング上位版の件数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shokuba_file = Path(args.shokuba_file)
    company_file = Path(args.company_file)
    ranking_file = Path(args.ranking_file) if str(args.ranking_file).strip() else None
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    companies = read_csv_flexible(company_file)
    company_corporate_column = find_column(list(companies.columns), CORPORATE_NUMBER_ALIASES)
    if not company_corporate_column:
        raise ValueError(f"企業CSVに法人番号列が見つかりません: {company_file}")
    company_security_column = find_column(list(companies.columns), SECURITY_CODE_ALIASES)
    company_name_column = find_column(list(companies.columns), COMPANY_NAME_ALIASES)
    target_corporate_numbers = set(companies[company_corporate_column].map(normalize_corporate_number))
    target_corporate_numbers.discard("")
    target_security_codes = set(companies[company_security_column].map(normalize_security_code)) if company_security_column else set()
    target_security_codes.discard("")
    target_company_names = set(companies[company_name_column].map(normalize_company_name_key)) if company_name_column else set()
    target_company_names.discard("")

    all_shokuba_columns = shokuba_columns(shokuba_file)
    selected_columns = select_shokuba_columns(all_shokuba_columns)
    shokuba_lookup, shokuba_report = build_shokuba_lookup(
        shokuba_file,
        selected_columns,
        target_corporate_numbers=target_corporate_numbers,
        target_security_codes=target_security_codes,
        target_company_names=target_company_names,
    )
    enriched = enrich_frame(companies, shokuba_lookup, company_corporate_column, company_security_column, company_name_column)

    company_output = output_dir / "companies_with_shokuba_key_metrics.csv"
    summary_output = output_dir / "companies_with_shokuba_summary_metrics.csv"
    coverage_output = output_dir / "shokuba_metric_coverage.csv"
    raw_matched_output = output_dir / "shokuba_matched_all_columns.csv"
    report_output = output_dir / "shokuba_enrichment_report.json"
    top_output = output_dir / f"top{args.top_n}_with_shokuba_key_metrics.csv"

    enriched.to_csv(company_output, index=False, encoding="utf-8-sig")
    summary_metrics_frame(enriched).to_csv(summary_output, index=False, encoding="utf-8-sig")
    coverage = metric_coverage(enriched)
    coverage.to_csv(coverage_output, index=False, encoding="utf-8-sig")

    target_numbers = set(enriched[company_corporate_column].map(normalize_corporate_number))
    if "しょくばらぼ_法人番号" in enriched.columns:
        target_numbers.update(enriched["しょくばらぼ_法人番号"].map(normalize_corporate_number))
    target_numbers.discard("")
    raw_matched_count = write_matched_raw_shokuba(shokuba_file, target_numbers, raw_matched_output)

    top_source = None
    if ranking_file and ranking_file.exists() and ranking_file.is_file():
        ranking = read_csv_flexible(ranking_file)
        ranking_corporate_column = find_column(list(ranking.columns), CORPORATE_NUMBER_ALIASES)
        if ranking_corporate_column:
            ranking_security_column = find_column(list(ranking.columns), SECURITY_CODE_ALIASES)
            ranking_company_column = find_column(list(ranking.columns), COMPANY_NAME_ALIASES)
            top_source = str(ranking_file)
            top_enriched = enrich_frame(ranking, shokuba_lookup, ranking_corporate_column, ranking_security_column, ranking_company_column)
            top_enriched = maybe_build_top_n(top_enriched, args.top_n)
            top_enriched.to_csv(top_output, index=False, encoding="utf-8-sig")
    if top_source is None:
        top_enriched = maybe_build_top_n(enriched, args.top_n)
        top_enriched.to_csv(top_output, index=False, encoding="utf-8-sig")
        top_source = str(company_file)

    matched_count = int(enriched["しょくばらぼ_JOIN"].eq("matched").sum())
    report = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "company_file": str(company_file),
        "ranking_file_used_for_top": top_source,
        "shokuba_file": str(shokuba_file),
        "company_input_count": int(len(enriched)),
        "company_with_corporate_number_count": int(len(target_numbers)),
        "company_with_original_corporate_number_count": int(enriched[company_corporate_column].map(normalize_corporate_number).ne("").sum()),
        "company_with_security_code_count": int(enriched[company_security_column].map(normalize_security_code).ne("").sum()) if company_security_column else 0,
        "company_with_company_name_key_count": int(enriched[company_name_column].map(normalize_company_name_key).ne("").sum()) if company_name_column else 0,
        "shokuba_matched_company_count": matched_count,
        "shokuba_match_by_corporate_number_count": int(enriched["しょくばらぼ_結合方法"].eq("corporate_number").sum()) if "しょくばらぼ_結合方法" in enriched.columns else 0,
        "shokuba_match_by_security_code_count": int(enriched["しょくばらぼ_結合方法"].eq("security_code").sum()) if "しょくばらぼ_結合方法" in enriched.columns else 0,
        "shokuba_match_by_company_name_count": int(enriched["しょくばらぼ_結合方法"].eq("company_name").sum()) if "しょくばらぼ_結合方法" in enriched.columns else 0,
        "shokuba_match_rate": round(matched_count / len(enriched), 4) if len(enriched) else 0,
        "raw_matched_shokuba_row_count": int(raw_matched_count),
        "top_n": int(args.top_n),
        "outputs": {
            "companies_with_shokuba_key_metrics_csv": str(company_output),
            "companies_with_shokuba_summary_metrics_csv": str(summary_output),
            "top_with_shokuba_key_metrics_csv": str(top_output),
            "shokuba_metric_coverage_csv": str(coverage_output),
            "shokuba_matched_all_columns_csv": str(raw_matched_output),
            "report_json": str(report_output),
        },
        **shokuba_report,
    }
    with report_output.open("w", encoding="utf-8-sig") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
