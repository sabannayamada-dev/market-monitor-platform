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
DEFAULT_OUTPUT_DIR = Path("outputs/women_activity_enrichment")
WOMEN_ACTIVITY_ENCODING = "utf-8-sig"

CORPORATE_NUMBER_ALIASES = [
    "corporate_number",
    "法人番号",
    "法人番号13桁",
    "hojin_bangho",
    "しょくばらぼ_法人番号",
]
COMPANY_NAME_ALIASES = ["company_name", "企業名", "会社名", "filer_name", "提出者名"]
SECURITY_CODE_ALIASES = ["sec_code", "security_code", "証券コード", "証券コード協議会コード"]
TOTAL_SCORE_ALIASES = ["total_score", "Total_Score", "総合スコア", "スコア"]

BASE_COLUMNS = [
    "企業名",
    "法人番号",
    "女性活躍推進法に基づく一般事業主行動計画",
    "業種",
    "業種(詳細分類)",
    "都道府県",
    "企業規模",
    "市場区分",
    "証券コード",
    "データの最終更新日",
]

CERTIFICATION_COLUMNS = [
    "企業認定等-くるみん認定",
    "企業認定等-くるみんプラス認定",
    "企業認定等-トライくるみん認定",
    "企業認定等-トライくるみんプラス認定",
    "企業認定等-プラチナくるみん認定",
    "企業認定等-プラチナくるみんプラス認定",
    "企業認定等-プラチナえるぼし、または、プラチナえるぼしプラス認定",
    "企業認定等-えるぼし、または、えるぼしプラス認定",
    "企業認定等-イクメン企業アワード",
    "企業認定等-ユースエール認定",
    "企業認定等-令和7年度なでしこ銘柄",
    "企業認定等-ダイバーシティ経営企業100選 / 新・ダイバーシティ経営企業100選",
    "企業認定等-100選プライム",
    "企業認定等-女性が輝く先進企業表彰",
]

KEY_METRIC_PATTERNS = [
    "女性労働者の割合",
    "競争倍率",
    "平均継続勤務年数",
    "継続雇用割合",
    "育児休業取得率",
    "平均残業時間",
    "年次有給休暇の取得率",
    "係長級",
    "管理職",
    "役員",
    "男女の賃金の差異",
    "対象期間",
    "データの対象",
    "データ集計時点",
]

SYSTEM_COLUMNS = [
    "フレックスタイム制度",
    "在宅勤務・テレワーク",
    "短時間勤務制度",
    "病気・不妊治療休暇",
    "年次有給休暇時間単位取得制度",
    "職種・雇用形態転換制度",
    "正社員再雇用・中途採用制度",
    "教育訓練・研修制度",
    "キャリアコンサルティング制度",
]

NUMERIC_PATTERNS = [
    "(%)",
    "(倍)",
    "(年)",
    "(時間)",
    "(人)",
    "割合",
    "差異",
    "平均残業時間",
    "有給休暇取得率",
]

IMPORTANT_NUMERIC_HELPER_COLUMNS = {
    "5.男女別の育児休業取得率-男性(%)",
    "5.男女別の育児休業取得率-女性(%)",
    "6.一月当たりの労働者の平均残業時間-平均残業時間(時間)",
    "7.雇用管理区分ごとの一月当たりの労働者の平均残業時間-平均残業時間(時間)",
    "8.(1)年次有給休暇の取得率-対象労働者(%)",
    "8.(2)年次有給休暇の取得率(区)-有給休暇取得率(%)",
    "10.管理職に占める女性労働者の割合-割合(%)",
    "11.役員に占める女性の割合-割合(%)",
    "14.男女の賃金の差異1-全労働者(%)",
    "14.男女の賃金の差異2-うち正規雇用労働者(%)",
    "14.男女の賃金の差異3-うち非正規雇用労働者(%)",
}

RANKING_READY_WOMEN_COLUMNS = [
    "女性活躍DB_JOIN",
    "女性活躍DB_結合方法",
    "女性活躍DB_取得項目数",
    "女性活躍DB_企業名",
    "女性活躍DB_法人番号",
    "女性活躍DB_証券コード",
    "女性活躍DB_業種",
    "女性活躍DB_業種(詳細分類)",
    "女性活躍DB_都道府県",
    "女性活躍DB_企業規模",
    "女性活躍DB_市場区分",
    "女性活躍DB_5.男女別の育児休業取得率-男性(%)_数値",
    "女性活躍DB_5.男女別の育児休業取得率-女性(%)_数値",
    "女性活躍DB_6.一月当たりの労働者の平均残業時間-平均残業時間(時間)_数値",
    "女性活躍DB_8.(1)年次有給休暇の取得率-対象労働者(%)_数値",
    "女性活躍DB_8.(2)年次有給休暇の取得率(区)-有給休暇取得率(%)_数値",
    "女性活躍DB_10.管理職に占める女性労働者の割合-割合(%)_数値",
    "女性活躍DB_11.役員に占める女性の割合-割合(%)_数値",
    "女性活躍DB_14.男女の賃金の差異1-全労働者(%)_数値",
    "女性活躍DB_14.男女の賃金の差異2-うち正規雇用労働者(%)_数値",
    "女性活躍DB_14.男女の賃金の差異3-うち非正規雇用労働者(%)_数値",
    "女性活躍DB_フレックスタイム制度",
    "女性活躍DB_在宅勤務・テレワーク",
    "女性活躍DB_短時間勤務制度",
    "女性活躍DB_病気・不妊治療休暇",
    "女性活躍DB_年次有給休暇時間単位取得制度",
    "女性活躍DB_データの最終更新日",
]

RANKING_READY_BASE_COLUMNS = [
    "企業キー",
    "法人番号",
    "企業名",
    "EDINETコード",
    "証券コード",
    "提出日",
    "対象期間終了日",
    "最新収集アプリver",
    "データ整合性フラグ",
    "データ整合性フラグ数",
    "分析候補",
    "企業分類推定",
    "総資産",
    "純資産",
    "負債",
    "自己資本比率",
    "営業利益",
    "経常利益",
    "当期利益",
    "営業キャッシュフロー",
    "投資キャッシュフロー",
    "財務キャッシュフロー",
    "現金及び現金同等物",
    "ROE",
    "平均年齢",
    "平均年間給与",
    "平均勤続年数",
    "研究開発費",
    "売上高・営業収益",
    "従業員数",
    "女性管理職比率",
    "男性育休取得率",
    "男女賃金差異_全労働者",
    "男女賃金差異_正規",
    "男女賃金差異_非正規",
    "詳細業種1",
    "詳細業種1信頼度",
    "詳細業種2",
    "詳細業種2信頼度",
    "詳細業種3",
    "詳細業種3信頼度",
    "詳細業種候補",
    "詳細業種根拠キーワード",
    "しょくばらぼ_JOIN",
    "しょくばらぼ_結合方法",
    "しょくばらぼ_取得項目数",
    "しょくばらぼ_企業名",
    "しょくばらぼ_法人番号",
    "しょくばらぼ_証券コード",
    "しょくばらぼ_業種",
    "しょくばらぼ_都道府県",
    "しょくばらぼ_企業規模",
    "しょくばらぼ_市場区分",
    "しょくばらぼ_正社員の有給休暇取得日数_数値",
    "しょくばらぼ_年次有給休暇取得率（全体）-取得率_数値",
    "しょくばらぼ_年次有給休暇取得率（雇用管理区分）-取得率（一覧）_数値",
    "しょくばらぼ_月平均所定外労働時間_数値",
    "しょくばらぼ_対象労働者全体の月平均の法定時間外労働時間と法定休日労働時間の合計-平均残業時間（詳細）_数値",
    "しょくばらぼ_平均の法定時間外労働60時間以上の労働者の数_数値",
    "しょくばらぼ_男女の賃金の差異-全労働者_数値",
    "しょくばらぼ_男女の賃金の差異-うち正規雇用労働者_数値",
    "しょくばらぼ_男女の賃金の差異-うち非正規雇用労働者_数値",
    "しょくばらぼ_正社員の平均継続勤務年数_数値",
    "しょくばらぼ_従業員の平均年齢_数値",
    "しょくばらぼ_管理職に占める女性の割合_数値",
    "しょくばらぼ_役員に占める女性の割合_数値",
    "しょくばらぼ_育児休業取得率（男性）-男性取得率（一覧）_数値",
    "しょくばらぼ_育児休業取得率（女性）-女性取得率（一覧）_数値",
    "しょくばらぼ_テレワーク制度-可否",
    "しょくばらぼ_副業・兼業-可否",
    "しょくばらぼ_多様な正社員制度-制度-職務限定正社員",
    "しょくばらぼ_多様な正社員制度-制度-勤務地限定正社員",
    "しょくばらぼ_多様な正社員制度-制度-短時間正社員",
    "しょくばらぼ_研修制度-有無",
    "しょくばらぼ_メンター制度-有無",
    "しょくばらぼ_自己啓発支援制度-有無",
    "しょくばらぼ_キャリアコンサルティング制度-有無",
    "しょくばらぼ_社内検定制度-有無",
    "しょくばらぼ_インターンシップの受入-可否",
    "しょくばらぼ_職場見学・職場体験の受入-可否",
    "しょくばらぼ_えるぼし認定-認定有無",
    "しょくばらぼ_プラチナえるぼし認定-認定有無",
    "しょくばらぼ_くるみん-認定有無",
    "しょくばらぼ_プラチナくるみん-認定有無",
    "しょくばらぼ_健康経営銘柄-認定有無",
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
    "industry",
    "業種",
    "detailed_industry_1",
    "詳細業種1",
    "女性活躍DB_JOIN",
    "女性活躍DB_結合方法",
    "女性活躍DB_取得項目数",
    "女性活躍DB_企業名",
    "女性活躍DB_法人番号",
    "女性活躍DB_証券コード",
    "女性活躍DB_業種",
    "女性活躍DB_業種(詳細分類)",
    "女性活躍DB_都道府県",
    "女性活躍DB_企業規模",
    "女性活躍DB_市場区分",
    "女性活躍DB_6.一月当たりの労働者の平均残業時間-平均残業時間(時間)",
    "女性活躍DB_6.一月当たりの労働者の平均残業時間-平均残業時間(時間)_数値",
    "女性活躍DB_8.(1)年次有給休暇の取得率-対象労働者(%)",
    "女性活躍DB_8.(1)年次有給休暇の取得率-対象労働者(%)_数値",
    "女性活躍DB_8.(2)年次有給休暇の取得率(区)-有給休暇取得率(%)",
    "女性活躍DB_8.(2)年次有給休暇の取得率(区)-有給休暇取得率(%)_数値",
    "女性活躍DB_5.男女別の育児休業取得率-男性(%)",
    "女性活躍DB_5.男女別の育児休業取得率-男性(%)_数値",
    "女性活躍DB_5.男女別の育児休業取得率-女性(%)",
    "女性活躍DB_5.男女別の育児休業取得率-女性(%)_数値",
    "女性活躍DB_4.(1)男女の平均継続勤務年数の差異-男性(年)",
    "女性活躍DB_4.(1)男女の平均継続勤務年数の差異-男性(年)_数値",
    "女性活躍DB_4.(1)男女の平均継続勤務年数の差異-女性(年)",
    "女性活躍DB_4.(1)男女の平均継続勤務年数の差異-女性(年)_数値",
    "女性活躍DB_10.管理職に占める女性労働者の割合-割合(%)",
    "女性活躍DB_10.管理職に占める女性労働者の割合-割合(%)_数値",
    "女性活躍DB_14.男女の賃金の差異1-全労働者(%)",
    "女性活躍DB_14.男女の賃金の差異1-全労働者(%)_数値",
    "女性活躍DB_フレックスタイム制度",
    "女性活躍DB_在宅勤務・テレワーク",
    "女性活躍DB_病気・不妊治療休暇",
    "女性活躍DB_年次有給休暇時間単位取得制度",
    "女性活躍DB_データの最終更新日",
]


def normalize_corporate_number(value: object) -> str:
    text = "" if value is None else str(value)
    if "e+" in text.lower() or "e-" in text.lower():
        return ""
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 13 else ""


def normalize_security_code(value: object) -> str:
    text = "" if value is None else str(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 5 and digits.endswith("0"):
        return digits[:4]
    return digits if len(digits) == 4 else ""


def normalize_company_name_key(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    replacements = [
        "株式会社",
        "(株)",
        "（株）",
        "有限会社",
        "合同会社",
        "ホールディングス",
        "holdings",
        "inc.",
        "inc",
        "co.,ltd.",
        "co.ltd.",
        "co ltd",
        "ltd.",
        "ltd",
    ]
    for item in replacements:
        text = text.replace(item, "")
    text = re.sub(r"[\s　・･,，.．\-ー―_/／\\()（）\[\]【】]", "", text)
    return text


def normalize_column_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().lower()


def find_column(columns: list[str], aliases: list[str]) -> str:
    by_normalized = {normalize_column_name(column): column for column in columns}
    for alias in aliases:
        found = by_normalized.get(normalize_column_name(alias))
        if found:
            return found
    for column in columns:
        normalized = normalize_column_name(column)
        if any(normalize_column_name(alias) in normalized for alias in aliases):
            return column
    return ""


def read_csv_flexible(path: Path, nrows: int | None = None, usecols: list[str] | None = None) -> pd.DataFrame:
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False, nrows=nrows, usecols=usecols)
        except Exception as exc:
            last_error = exc
    raise ValueError(f"CSVを読み込めません: {path}: {last_error}")


def source_columns(path: Path) -> list[str]:
    return list(pd.read_csv(path, encoding=WOMEN_ACTIVITY_ENCODING, dtype=str, nrows=0).columns)


def select_source_columns(columns: list[str]) -> list[str]:
    selected: list[str] = []
    for column in columns:
        if column in BASE_COLUMNS or column in CERTIFICATION_COLUMNS or column in SYSTEM_COLUMNS:
            selected.append(column)
            continue
        if any(pattern in column for pattern in KEY_METRIC_PATTERNS):
            selected.append(column)
    for required in ["法人番号", "証券コード", "企業名"]:
        if required in columns and required not in selected:
            selected.insert(0, required)
    return [column for column in columns if column in set(selected)]


def parse_number(value: object) -> float | None:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).strip()
    if not text or text in {"-", "－", "―", "nan", "None"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def should_add_numeric_helper(column: str) -> bool:
    source_column = column.removeprefix("女性活躍DB_")
    return source_column in IMPORTANT_NUMERIC_HELPER_COLUMNS


def add_numeric_helpers(frame: pd.DataFrame) -> pd.DataFrame:
    helper_columns: dict[str, pd.Series] = {}
    for column in list(frame.columns):
        if not column.startswith("女性活躍DB_"):
            continue
        if column.endswith("_数値") or not should_add_numeric_helper(column):
            continue
        values = frame[column].map(parse_number)
        if values.notna().any():
            helper_columns[f"{column}_数値"] = values
    if helper_columns:
        frame = pd.concat([frame, pd.DataFrame(helper_columns, index=frame.index)], axis=1)
    return frame


def drop_existing_women_activity_columns(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    removable_prefixes = ("女性活躍DB_",)
    removable_exact = {"女性活躍DB_JOIN"}
    removable = [
        column
        for column in frame.columns
        if column in removable_exact or any(str(column).startswith(prefix) for prefix in removable_prefixes)
    ]
    if not removable:
        return frame, 0
    return frame.drop(columns=removable, errors="ignore"), len(removable)


def ranking_ready_frame(enriched: pd.DataFrame) -> pd.DataFrame:
    base_columns = [column for column in RANKING_READY_BASE_COLUMNS if column in enriched.columns]
    women_columns = [column for column in RANKING_READY_WOMEN_COLUMNS if column in enriched.columns]
    columns = []
    seen: set[str] = set()
    for column in [*base_columns, *women_columns]:
        if column not in seen:
            columns.append(column)
            seen.add(column)
    return enriched[columns].copy()


def nonempty_count(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    if not columns:
        return pd.Series([0] * len(frame), index=frame.index)
    work = frame[columns].fillna("").astype(str)
    return work.apply(lambda row: sum(bool(value.strip()) for value in row), axis=1)


def build_source_lookup(
    source_file: Path,
    selected_columns: list[str],
    target_corporate_numbers: set[str],
    target_security_codes: set[str],
    target_company_names: set[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    key_columns = [column for column in ["法人番号", "証券コード", "企業名"] if column in selected_columns]
    key_frame = pd.read_csv(source_file, encoding=WOMEN_ACTIVITY_ENCODING, dtype=str, usecols=key_columns, keep_default_na=False)
    key_frame["法人番号_norm"] = key_frame["法人番号"].map(normalize_corporate_number) if "法人番号" in key_frame.columns else ""
    key_frame["証券コード_norm"] = key_frame["証券コード"].map(normalize_security_code) if "証券コード" in key_frame.columns else ""
    key_frame["企業名_norm"] = key_frame["企業名"].map(normalize_company_name_key) if "企業名" in key_frame.columns else ""

    target_mask = pd.Series(False, index=key_frame.index)
    if target_corporate_numbers:
        target_mask = target_mask | key_frame["法人番号_norm"].isin(target_corporate_numbers)
    if target_security_codes:
        target_mask = target_mask | key_frame["証券コード_norm"].isin(target_security_codes)
    if target_company_names:
        target_mask = target_mask | key_frame["企業名_norm"].isin(target_company_names)

    if bool(target_mask.any()):
        keep_row_numbers = set((key_frame.index[target_mask] + 1).tolist())
        frame = pd.read_csv(
            source_file,
            encoding=WOMEN_ACTIVITY_ENCODING,
            dtype=str,
            usecols=selected_columns,
            keep_default_na=False,
            skiprows=lambda row_number: row_number > 0 and row_number not in keep_row_numbers,
        )
    else:
        frame = pd.DataFrame(columns=selected_columns)

    frame["法人番号_norm"] = frame["法人番号"].map(normalize_corporate_number) if "法人番号" in frame.columns else ""
    frame["証券コード_norm"] = frame["証券コード"].map(normalize_security_code) if "証券コード" in frame.columns else ""
    frame["企業名_norm"] = frame["企業名"].map(normalize_company_name_key) if "企業名" in frame.columns else ""

    metric_columns = [column for column in selected_columns if column not in {"法人番号", "証券コード", "企業名"}]
    frame["_women_nonempty_metric_count"] = nonempty_count(frame, metric_columns)
    duplicate_count = int(frame["法人番号_norm"].ne("").sum() - frame.loc[frame["法人番号_norm"].ne("")]["法人番号_norm"].nunique())
    frame = frame.sort_values(["法人番号_norm", "_women_nonempty_metric_count"], ascending=[True, False])

    report = {
        "women_activity_source_row_count": int(len(key_frame)),
        "women_activity_lookup_row_count": int(len(frame)),
        "women_activity_unique_corporate_number_count": int(frame["法人番号_norm"].replace("", pd.NA).dropna().nunique()),
        "women_activity_security_code_count": int(frame["証券コード_norm"].ne("").sum()),
        "women_activity_company_name_key_count": int(frame["企業名_norm"].ne("").sum()),
        "women_activity_duplicate_row_count": duplicate_count,
        "selected_women_activity_column_count": int(len(selected_columns)),
    }
    return frame, report


def write_matched_raw_source(source_file: Path, target_numbers: set[str], output_path: Path) -> int:
    if not target_numbers:
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        return 0
    key_frame = pd.read_csv(source_file, encoding=WOMEN_ACTIVITY_ENCODING, dtype=str, usecols=["法人番号"], keep_default_na=False)
    key_frame["法人番号_norm"] = key_frame["法人番号"].map(normalize_corporate_number)
    keep_row_numbers = set((key_frame.index[key_frame["法人番号_norm"].isin(target_numbers)] + 1).tolist())
    if not keep_row_numbers:
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        return 0
    matched = pd.read_csv(
        source_file,
        encoding=WOMEN_ACTIVITY_ENCODING,
        dtype=str,
        keep_default_na=False,
        skiprows=lambda row_number: row_number > 0 and row_number not in keep_row_numbers,
    )
    matched.to_csv(output_path, index=False, encoding="utf-8-sig")
    return int(len(matched))


INTERNAL_SOURCE_COLUMNS = {"法人番号_norm", "証券コード_norm", "企業名_norm", "_women_nonempty_metric_count"}


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
    unmatched = output["_women_nonempty_metric_count"].isna() & output[key_column].fillna("").astype(str).ne("")
    if not unmatched.any():
        return output
    lookup = prefixed_lookup[prefixed_lookup[key_column].fillna("").astype(str).ne("")].copy()
    if lookup.empty:
        return output
    if unique_only:
        lookup = lookup.loc[~lookup.duplicated(key_column, keep=False)].copy()
    else:
        lookup = lookup.sort_values([key_column, "_women_nonempty_metric_count"], ascending=[True, False])
        lookup = lookup.drop_duplicates(key_column, keep="first")
    if lookup.empty:
        return output
    lookup_index_by_key = pd.Series(lookup.index, index=lookup[key_column].astype(str)).to_dict()
    matched_lookup_indexes = output.loc[unmatched, key_column].astype(str).map(lookup_index_by_key)
    matched_mask = matched_lookup_indexes.notna()
    if not bool(matched_mask.any()):
        return output
    target_indexes = matched_lookup_indexes.index[matched_mask]
    source_indexes = matched_lookup_indexes.loc[matched_mask].astype(int).to_list()
    matched_values = lookup.loc[source_indexes, ["_women_nonempty_metric_count", *data_columns]].reset_index(drop=True)
    for column in ["_women_nonempty_metric_count", *data_columns]:
        output.loc[target_indexes, column] = matched_values[column].to_numpy()
    output.loc[target_indexes, "女性活躍DB_結合方法"] = method
    return output


def enrich_frame(
    base: pd.DataFrame,
    source_lookup: pd.DataFrame,
    base_corporate_column: str,
    base_security_column: str = "",
    base_company_column: str = "",
) -> pd.DataFrame:
    work = base.copy()
    work["法人番号_norm"] = work[base_corporate_column].map(normalize_corporate_number)
    work["証券コード_norm"] = work[base_security_column].map(normalize_security_code) if base_security_column else ""
    work["企業名_norm"] = work[base_company_column].map(normalize_company_name_key) if base_company_column else ""

    prefixed = source_lookup.copy()
    rename_map = {column: f"女性活躍DB_{column}" for column in prefixed.columns if column not in INTERNAL_SOURCE_COLUMNS}
    prefixed = prefixed.rename(columns=rename_map)
    data_columns = [column for column in prefixed.columns if column not in INTERNAL_SOURCE_COLUMNS]
    corporate_lookup = prefixed[
        prefixed["法人番号_norm"].fillna("").astype(str).ne("")
    ][["法人番号_norm", "_women_nonempty_metric_count", *data_columns]].copy()
    corporate_lookup = corporate_lookup.sort_values(["法人番号_norm", "_women_nonempty_metric_count"], ascending=[True, False])
    corporate_lookup = corporate_lookup.drop_duplicates("法人番号_norm", keep="first")
    output = work.merge(corporate_lookup, on="法人番号_norm", how="left")
    output["女性活躍DB_結合方法"] = output["_women_nonempty_metric_count"].notna().map(lambda value: "corporate_number" if value else "")
    output = fill_unmatched_from_lookup(output, prefixed, "証券コード_norm", data_columns, "security_code")
    output = fill_unmatched_from_lookup(output, prefixed, "企業名_norm", data_columns, "company_name", unique_only=True)
    output["女性活躍DB_JOIN"] = output["_women_nonempty_metric_count"].notna().map(lambda value: "matched" if value else "not_found")
    output["女性活躍DB_取得項目数"] = output["_women_nonempty_metric_count"].fillna(0).astype(int)
    output = add_numeric_helpers(output)
    return output.drop(columns=["法人番号_norm", "証券コード_norm", "企業名_norm", "_women_nonempty_metric_count"], errors="ignore")


def metric_coverage(enriched: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    ignored = {"女性活躍DB_JOIN", "女性活躍DB_結合方法", "女性活躍DB_取得項目数"}
    target_columns = [column for column in enriched.columns if column.startswith("女性活躍DB_") and column not in ignored]
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


def summary_metrics_frame(enriched: pd.DataFrame) -> pd.DataFrame:
    columns = [column for column in SUMMARY_PREFERRED_COLUMNS if column in enriched.columns]
    seen: set[str] = set()
    unique_columns = []
    for column in columns:
        if column not in seen:
            unique_columns.append(column)
            seen.add(column)
    if not unique_columns:
        return enriched.copy()
    return enriched[unique_columns].copy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="任意の企業CSVに、女性の活躍推進企業データベースの情報を結合します")
    parser.add_argument("--women-file", required=True, help="女性の活躍推進企業データベースCSV")
    parser.add_argument("--company-file", default=str(DEFAULT_COMPANY_FILE), help="結合対象の企業CSV")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="出力フォルダ")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    women_file = Path(args.women_file)
    company_file = Path(args.company_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    companies = read_csv_flexible(company_file)
    companies, dropped_existing_women_activity_column_count = drop_existing_women_activity_columns(companies)
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

    all_columns = source_columns(women_file)
    selected_columns = select_source_columns(all_columns)
    source_lookup, source_report = build_source_lookup(
        women_file,
        selected_columns,
        target_corporate_numbers=target_corporate_numbers,
        target_security_codes=target_security_codes,
        target_company_names=target_company_names,
    )
    enriched = enrich_frame(companies, source_lookup, company_corporate_column, company_security_column, company_name_column)

    company_output = output_dir / "companies_with_women_activity_key_metrics.csv"
    ranking_ready_output = output_dir / "companies_with_women_activity_ranking_ready.csv"
    summary_output = output_dir / "companies_with_women_activity_summary_metrics.csv"
    coverage_output = output_dir / "women_activity_metric_coverage.csv"
    raw_matched_output = output_dir / "women_activity_matched_all_columns.csv"
    report_output = output_dir / "women_activity_enrichment_report.json"

    ranking_ready = ranking_ready_frame(enriched)
    ranking_ready.to_csv(company_output, index=False, encoding="utf-8-sig")
    ranking_ready.to_csv(ranking_ready_output, index=False, encoding="utf-8-sig")
    summary_metrics_frame(enriched).to_csv(summary_output, index=False, encoding="utf-8-sig")
    metric_coverage(enriched).to_csv(coverage_output, index=False, encoding="utf-8-sig")

    target_numbers = set(enriched[company_corporate_column].map(normalize_corporate_number))
    if "女性活躍DB_法人番号" in enriched.columns:
        target_numbers.update(enriched["女性活躍DB_法人番号"].map(normalize_corporate_number))
    target_numbers.discard("")
    raw_matched_count = write_matched_raw_source(women_file, target_numbers, raw_matched_output)

    matched_count = int(enriched["女性活躍DB_JOIN"].eq("matched").sum())
    report = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "company_file": str(company_file),
        "women_activity_file": str(women_file),
        "company_input_count": int(len(enriched)),
        "dropped_existing_women_activity_column_count": int(dropped_existing_women_activity_column_count),
        "company_with_original_corporate_number_count": int(enriched[company_corporate_column].map(normalize_corporate_number).ne("").sum()),
        "company_with_security_code_count": int(enriched[company_security_column].map(normalize_security_code).ne("").sum()) if company_security_column else 0,
        "company_with_company_name_key_count": int(enriched[company_name_column].map(normalize_company_name_key).ne("").sum()) if company_name_column else 0,
        "women_activity_matched_company_count": matched_count,
        "women_activity_match_by_corporate_number_count": int(enriched["女性活躍DB_結合方法"].eq("corporate_number").sum()),
        "women_activity_match_by_security_code_count": int(enriched["女性活躍DB_結合方法"].eq("security_code").sum()),
        "women_activity_match_by_company_name_count": int(enriched["女性活躍DB_結合方法"].eq("company_name").sum()),
        "women_activity_match_rate": round(matched_count / len(enriched), 4) if len(enriched) else 0,
        "raw_matched_women_activity_row_count": int(raw_matched_count),
        "outputs": {
            "companies_with_women_activity_key_metrics_csv": str(company_output),
            "companies_with_women_activity_ranking_ready_csv": str(ranking_ready_output),
            "companies_with_women_activity_summary_metrics_csv": str(summary_output),
            "women_activity_metric_coverage_csv": str(coverage_output),
            "women_activity_matched_all_columns_csv": str(raw_matched_output),
            "report_json": str(report_output),
        },
        **source_report,
    }
    with report_output.open("w", encoding="utf-8-sig") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
