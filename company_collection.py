from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import string
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app_meta import APP_BUILD_DATE, APP_NAME, APP_VERSION
from company_scoring import (
    CORPORATE_MASTER_COLUMN_ALIASES,
    JOB_COLUMN_ALIASES,
    RunContext,
    fetch_edinet_xbrl,
    find_corporate_number_candidates,
    fetch_edinet_document_list,
    make_recent_date_strings,
    normalize_column_label,
    normalize_corporate_number,
    normalize_text,
    parse_xbrl_to_edinet_record,
    setup_logging,
)


OUTPUT_DIR = "outputs"
DEFAULT_REGISTRY = "work/data_source_registry_template.json"
EDINET_DOCUMENT_METADATA_COLUMNS = [
    "sec_code",
    "fund_code",
    "ordinance_code",
    "form_code",
    "period_start",
    "fiscal_year_end",
    "submit_datetime",
    "doc_description",
    "xbrl_flag",
    "pdf_flag",
    "csv_flag",
    "english_doc_flag",
    "withdrawal_status",
    "disclosure_status",
    "legal_status",
]
EDINET_XBRL_EXTRA_COLUMNS = [
    "total_assets",
    "net_assets",
    "liabilities",
    "equity_to_asset_ratio",
    "operating_income",
    "ordinary_income",
    "ordinary_profit",
    "profit_loss",
    "gross_profit",
    "cash_flow_operating",
    "cash_flow_investing",
    "cash_flow_financing",
    "cash_and_equivalents",
    "roe",
    "eps",
    "net_assets_per_share",
    "dividend_per_share",
    "payout_ratio",
    "capital_stock",
    "capital_expenditures",
    "average_age",
    "salary_change_rate",
    "female_manager_ratio",
    "male_childcare_leave_ratio",
    "wage_gap_all",
    "wage_gap_regular",
    "wage_gap_non_regular",
    "business_description",
    "business_risks",
    "dividend_policy",
    "rd_activities_text",
    "human_resources_policy",
    "product_service_info",
    "regional_sales_info",
]
EDINET_EXTRA_COLUMNS = EDINET_DOCUMENT_METADATA_COLUMNS + EDINET_XBRL_EXTRA_COLUMNS
DATA_INTEGRITY_FLAGS_COLUMN = "data_integrity_flags"
DETAILED_INDUSTRY_COLUMNS = [
    "detailed_industry_1",
    "detailed_industry_1_score",
    "detailed_industry_2",
    "detailed_industry_2_score",
    "detailed_industry_3",
    "detailed_industry_3_score",
    "detailed_industry_candidates",
    "detailed_industry_evidence_keywords",
    "detailed_industry_evidence_text",
]
NORMALIZED_COLUMNS = [
    "source_id",
    "source_record_id",
    "collection_app_version",
    "job_id",
    "corporate_number",
    "company_name",
    "office_name",
    "job_title",
    "work_location",
    "basic_salary_min",
    "basic_salary_max",
    "annual_holidays",
    "overtime_hours_avg",
    "report_submit_date",
    *EDINET_EXTRA_COLUMNS,
    *DETAILED_INDUSTRY_COLUMNS,
    "average_annual_salary",
    "average_length_of_service",
    "rd_expenses",
    "net_sales",
    "number_of_employees",
    "property_info",
    "edinet_status",
    "xbrl_parse_status",
    "xbrl_extracted_fields",
    "xbrl_missing_fields",
    "xbrl_diagnostic_notes",
    "source_quality_status",
    "source_quality_notes",
    DATA_INTEGRITY_FLAGS_COLUMN,
]
REVIEW_QUEUE_COLUMNS = [
    "source_id",
    "source_record_id",
    "company_name",
    "work_location",
    "candidate_count",
    "top_candidate_corporate_number",
    "top_candidate_company_name",
    "top_candidate_address",
    "top_candidate_confidence",
    "reason",
]
COLLECTION_STORE_EXTRA_COLUMNS = [
    "collection_run_id",
    "collected_at",
    "source_fingerprint",
    "collection_status",
    "collection_manifest",
    "collection_normalized_csv",
]
QUALITY_SUMMARY_COLUMNS = [
    "metric",
    "value",
]
DUPLICATE_AUDIT_COLUMNS = [
    "dedupe_key",
    "duplicate_count",
    "collection_run_ids",
    "source_ids",
    "job_ids",
    "corporate_numbers",
    "company_names",
    "work_locations",
]
CSV_COLUMN_LABELS = {
    "collection_run_id": "収集実行ID",
    "collected_at": "収集日時",
    "source_fingerprint": "収集元フィンガープリント",
    "collection_status": "収集状態",
    "collection_manifest": "収集マニフェスト",
    "collection_normalized_csv": "内部用正規化CSV",
    "app_version": "アプリver",
    "app_build_date": "アプリビルド日",
    "created_at": "作成日時",
    "source_type": "収集元種別",
    "input": "入力",
    "same_input_as_previous": "過去同一入力あり",
    "previous_same_input_run_id": "過去同一入力の収集実行ID",
    "status": "状態",
    "record_count": "レコード件数",
    "error_count": "エラー件数",
    "xbrl_parsed_count": "XBRL解析済み件数",
    "xbrl_attempted_count": "XBRL解析試行件数",
    "corporate_number_review_count": "法人番号確認件数",
    "manifest": "マニフェスト",
    "normalized_csv": "内部用正規化CSV",
    "corporate_number_review_queue_csv": "法人番号確認CSV",
    "exclude_non_analysis_candidates": "分析候補外除外",
    "source_id": "収集元ID",
    "source_record_id": "収集元レコードID",
    "collection_app_version": "収集時アプリver",
    "job_id": "書類ID・求人番号",
    "corporate_number": "法人番号",
    "company_name": "企業名",
    "office_name": "事業所名・EDINETコード",
    "job_title": "職種・データ種別",
    "work_location": "勤務地",
    "basic_salary_min": "基本給下限",
    "basic_salary_max": "基本給上限",
    "annual_holidays": "年間休日数",
    "overtime_hours_avg": "月平均残業時間",
    "report_submit_date": "提出日",
    "sec_code": "証券コード",
    "fund_code": "ファンドコード",
    "ordinance_code": "府令コード",
    "form_code": "様式コード",
    "period_start": "対象期間開始日",
    "fiscal_year_end": "対象期間終了日",
    "submit_datetime": "提出日時",
    "doc_description": "書類説明",
    "xbrl_flag": "XBRL有無",
    "pdf_flag": "PDF有無",
    "csv_flag": "CSV有無",
    "english_doc_flag": "英文書類有無",
    "withdrawal_status": "取下げ状態",
    "disclosure_status": "開示状態",
    "legal_status": "法定状態",
    "total_assets": "総資産",
    "net_assets": "純資産",
    "liabilities": "負債",
    "equity_to_asset_ratio": "自己資本比率",
    "operating_income": "営業利益",
    "ordinary_income": "経常収益・経常利益",
    "ordinary_profit": "経常利益",
    "profit_loss": "当期利益",
    "gross_profit": "粗利益",
    "cash_flow_operating": "営業キャッシュフロー",
    "cash_flow_investing": "投資キャッシュフロー",
    "cash_flow_financing": "財務キャッシュフロー",
    "cash_and_equivalents": "現金及び現金同等物",
    "roe": "ROE",
    "eps": "1株当たり利益",
    "net_assets_per_share": "1株当たり純資産",
    "dividend_per_share": "1株当たり配当",
    "payout_ratio": "配当性向",
    "capital_stock": "資本金",
    "capital_expenditures": "設備投資額",
    "average_age": "平均年齢",
    "salary_change_rate": "平均給与増減率",
    "female_manager_ratio": "女性管理職比率",
    "male_childcare_leave_ratio": "男性育休取得率",
    "wage_gap_all": "男女賃金差異_全労働者",
    "wage_gap_regular": "男女賃金差異_正規",
    "wage_gap_non_regular": "男女賃金差異_非正規",
    "business_description": "事業内容",
    "business_risks": "事業等のリスク",
    "dividend_policy": "配当方針",
    "rd_activities_text": "研究開発活動",
    "human_resources_policy": "人的資本方針",
    "product_service_info": "製品サービス情報",
    "regional_sales_info": "地域別売上情報",
    "detailed_industry_1": "詳細業種1",
    "detailed_industry_1_score": "詳細業種1信頼度",
    "detailed_industry_2": "詳細業種2",
    "detailed_industry_2_score": "詳細業種2信頼度",
    "detailed_industry_3": "詳細業種3",
    "detailed_industry_3_score": "詳細業種3信頼度",
    "detailed_industry_candidates": "詳細業種候補",
    "detailed_industry_evidence_keywords": "詳細業種根拠キーワード",
    "detailed_industry_evidence_text": "詳細業種根拠テキスト",
    "average_annual_salary": "平均年間給与",
    "average_length_of_service": "平均勤続年数",
    "rd_expenses": "研究開発費",
    "net_sales": "売上高・営業収益",
    "number_of_employees": "従業員数",
    "property_info": "主要設備情報",
    "edinet_status": "EDINET取得状態",
    "xbrl_parse_status": "XBRL解析状態",
    "xbrl_extracted_fields": "XBRL取得項目",
    "xbrl_missing_fields": "XBRL不足項目",
    "xbrl_diagnostic_notes": "XBRL診断メモ",
    "source_quality_status": "収集元品質状態",
    "source_quality_notes": "収集元品質メモ",
    DATA_INTEGRITY_FLAGS_COLUMN: "データ整合性フラグ",
    "company_key": "企業キー",
    "edinet_code": "EDINETコード",
    "company_category_guess": "企業分類推定",
    "analysis_candidate": "分析候補",
    "analysis_exclusion_reason": "分析除外理由",
    "source_ids": "収集元ID一覧",
    "document_count": "書類件数",
    "latest_document_id": "最新書類ID",
    "latest_collection_run_id": "最新収集実行ID",
    "latest_collection_app_version": "最新収集アプリver",
    "latest_collected_at": "最新収集日時",
    "corporate_number_status": "法人番号状態",
    "source_warning_count": "注意件数",
    "document_ids_sample": "書類IDサンプル",
    "candidate_count": "候補件数",
    "top_candidate_corporate_number": "最有力候補法人番号",
    "top_candidate_company_name": "最有力候補企業名",
    "top_candidate_address": "最有力候補住所",
    "top_candidate_confidence": "最有力候補信頼度",
    "reason": "理由",
    "metric": "項目",
    "value": "値",
    "dedupe_key": "重複判定キー",
    "duplicate_count": "重複件数",
    "collection_run_ids": "収集実行ID一覧",
    "job_ids": "書類ID・求人番号一覧",
    "corporate_numbers": "法人番号一覧",
    "company_names": "企業名一覧",
    "work_locations": "勤務地一覧",
}


def localize_csv_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={column: CSV_COLUMN_LABELS.get(column, column) for column in frame.columns})


def write_user_csv(frame: pd.DataFrame, path: Path) -> Path:
    localized = localize_csv_columns(frame)
    try:
        localized.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}")
        localized.to_csv(fallback, index=False, encoding="utf-8-sig")
        return fallback


def columns_without_labels(columns: list[str]) -> list[str]:
    return [column for column in columns if column not in CSV_COLUMN_LABELS]


COMPANY_INDEX_COLUMNS = [
    "company_key",
    "corporate_number",
    "company_name",
    "edinet_code",
    "company_category_guess",
    "analysis_candidate",
    "analysis_exclusion_reason",
    "source_ids",
    "document_count",
    "latest_document_id",
    "latest_collection_run_id",
    "latest_collection_app_version",
    "latest_collected_at",
    "corporate_number_status",
    "report_submit_date",
    *EDINET_EXTRA_COLUMNS,
    *DETAILED_INDUSTRY_COLUMNS,
    "average_annual_salary",
    "average_length_of_service",
    "rd_expenses",
    "net_sales",
    "number_of_employees",
    "property_info",
    "edinet_status",
    "xbrl_parse_status",
    "xbrl_extracted_fields",
    "xbrl_missing_fields",
    "xbrl_diagnostic_notes",
    "source_warning_count",
    "document_ids_sample",
    DATA_INTEGRITY_FLAGS_COLUMN,
]
ANALYSIS_CANDIDATE_COLUMNS = COMPANY_INDEX_COLUMNS
FUND_OR_FINANCE_KEYWORDS = [
    "投資法人",
    "投資信託",
    "投信",
    "ファンド",
    "ETF",
    "ＥＴＦ",
    "GLOBAL X",
    "ＧＬＯＢＡＬ Ｘ",
    "Ｇｌｏｂａｌ Ｘ",
    "グローバルＸ",
    "アセット",
    "アセットマネジメント",
    "アセット・マネジメント",
    "資産運用",
    "インベストメント",
    "キャピタル",
    "証券投資",
]
NON_OPERATING_DOCUMENT_KEYWORDS = [
    "投資信託",
    "投資証券",
    "受益証券",
    "内国投資",
    "外国投資",
    "資産流動化証券",
    "有価証券投資事業権利",
    "投資事業権利",
    "有価証券届出書",
]

DETAILED_INDUSTRY_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "SI/ITサービス": [
        ("システムインテグレーション", 5.0),
        ("システム開発", 4.0),
        ("受託開発", 3.5),
        ("ITサービス", 3.0),
        ("情報システム", 2.5),
        ("DX", 2.0),
        ("デジタルトランスフォーメーション", 2.0),
        ("ERP", 2.0),
        ("保守運用", 1.8),
    ],
    "ソフトウェア": [("ソフトウェア", 4.0), ("アプリケーション", 2.5), ("ミドルウェア", 2.5), ("パッケージ", 2.0), ("組込みソフト", 2.0)],
    "クラウド/SaaS": [("SaaS", 5.0), ("クラウド", 4.0), ("サブスクリプション", 2.5), ("プラットフォーム", 2.0), ("IaaS", 2.0), ("PaaS", 2.0)],
    "通信キャリア": [("移動通信", 5.0), ("携帯電話", 4.0), ("通信キャリア", 4.0), ("電気通信役務", 3.0), ("通信サービス", 2.5)],
    "通信インフラ": [("光ファイバ", 4.0), ("基地局", 4.0), ("ネットワーク設備", 3.0), ("通信設備", 3.0), ("データセンター", 2.0)],
    "インターネット/メディア": [("インターネット", 3.0), ("メディア", 3.0), ("広告配信", 3.0), ("動画配信", 2.5), ("ポータル", 2.0), ("SNS", 2.0)],
    "ゲーム": [("ゲーム", 5.0), ("オンラインゲーム", 4.0), ("モバイルゲーム", 4.0), ("コンテンツ", 1.5)],
    "半導体": [("半導体", 5.0), ("ウェハ", 4.0), ("ウエハ", 4.0), ("パワーデバイス", 4.0), ("集積回路", 3.5), ("LSI", 3.0), ("メモリ", 2.5)],
    "半導体製造装置": [("半導体製造装置", 6.0), ("露光装置", 4.0), ("成膜装置", 4.0), ("エッチング", 4.0), ("洗浄装置", 3.0), ("検査装置", 2.5)],
    "電子部品": [("電子部品", 5.0), ("コンデンサ", 4.0), ("コネクタ", 4.0), ("センサ", 3.0), ("プリント配線板", 4.0), ("プリント基板", 4.0), ("受動部品", 3.5)],
    "電気機器": [("電気機器", 4.0), ("電気製品", 3.0), ("家電", 3.0), ("モーター", 2.0), ("インバータ", 2.0)],
    "産業機器": [("産業機器", 4.0), ("FA", 3.0), ("ファクトリーオートメーション", 4.0), ("ロボット", 3.0), ("工作機械", 3.0), ("制御装置", 2.5)],
    "計測/制御": [("計測", 4.0), ("測定器", 4.0), ("制御", 3.0), ("検査機器", 3.0), ("分析機器", 3.0)],
    "光学/精密機器": [("光学", 4.0), ("精密機器", 4.0), ("レンズ", 3.0), ("カメラ", 3.0), ("医療機器", 2.5)],
    "化学": [("化学", 3.0), ("樹脂", 3.0), ("高分子", 3.0), ("機能性材料", 3.0), ("化成品", 3.0), ("有機合成", 2.5)],
    "医薬品": [("医薬品", 5.0), ("製薬", 5.0), ("医療用医薬品", 4.0), ("新薬", 3.0), ("ジェネリック", 3.0)],
    "バイオ/創薬": [("創薬", 5.0), ("バイオ", 4.0), ("抗体", 3.0), ("細胞", 2.5), ("遺伝子", 2.5), ("治験", 2.0)],
    "素材/材料": [("素材", 3.0), ("材料", 2.5), ("複合材料", 3.0), ("炭素繊維", 4.0), ("フィルム", 2.0)],
    "鉄鋼/非鉄": [("鉄鋼", 5.0), ("非鉄", 4.0), ("アルミ", 3.0), ("銅", 2.5), ("金属材料", 3.0)],
    "ガラス/セラミックス": [("ガラス", 4.0), ("セラミックス", 4.0), ("硝子", 4.0), ("耐火物", 3.0)],
    "紙/パルプ": [("紙", 3.0), ("パルプ", 5.0), ("板紙", 4.0), ("包装材", 2.5)],
    "自動車": [("自動車", 4.0), ("完成車", 5.0), ("車両", 2.5), ("EV", 2.5), ("電動車", 2.5)],
    "自動車部品": [("自動車部品", 5.0), ("車載", 3.0), ("駆動系", 3.0), ("ブレーキ", 3.0), ("内装部品", 3.0)],
    "機械": [("機械", 2.5), ("機械装置", 3.5), ("建設機械", 3.0), ("農業機械", 3.0), ("ポンプ", 2.0), ("圧縮機", 2.0)],
    "重工/造船/航空": [("重工", 4.0), ("造船", 5.0), ("航空", 4.0), ("宇宙", 3.0), ("防衛", 3.0), ("タービン", 3.0)],
    "鉄道/輸送機器": [("鉄道", 4.0), ("車両製造", 4.0), ("輸送機器", 3.0), ("鉄道車両", 5.0)],
    "電力": [("電力", 5.0), ("発電", 4.0), ("送配電", 4.0), ("電気事業", 4.0), ("再生可能エネルギー", 2.5)],
    "ガス": [("都市ガス", 5.0), ("ガス事業", 4.0), ("LNG", 3.0), ("液化天然ガス", 3.0), ("ガス導管", 4.0)],
    "石油/資源": [("石油", 4.0), ("資源開発", 4.0), ("鉱山", 3.0), ("原油", 3.0), ("天然ガス", 2.5)],
    "建設": [("建設", 4.0), ("土木", 3.0), ("建築", 3.0), ("ゼネコン", 5.0), ("施工", 2.5)],
    "プラント/エンジニアリング": [("プラント", 5.0), ("エンジニアリング", 4.0), ("EPC", 4.0), ("設備工事", 3.0)],
    "不動産/デベロッパー": [("不動産", 4.0), ("デベロッパー", 5.0), ("賃貸", 2.5), ("分譲", 2.5), ("オフィスビル", 2.0)],
    "銀行": [("銀行", 5.0), ("預金", 3.0), ("貸出", 2.5), ("信託銀行", 4.0)],
    "証券": [("証券", 5.0), ("金融商品取引", 3.0), ("投資銀行", 3.0)],
    "保険": [("保険", 5.0), ("生命保険", 5.0), ("損害保険", 5.0), ("再保険", 4.0)],
    "リース/信販": [("リース", 5.0), ("信販", 5.0), ("クレジット", 3.0), ("割賦", 3.0)],
    "総合商社": [("総合商社", 6.0), ("トレーディング", 2.5), ("資源投資", 2.5)],
    "専門商社": [("専門商社", 5.0), ("卸売", 2.5), ("商事", 1.5), ("販売代理", 2.0)],
    "コンサル": [("コンサルティング", 5.0), ("コンサル", 4.0), ("戦略", 2.5), ("業務改革", 3.0), ("PMO", 3.0)],
    "人材/アウトソーシング": [("人材", 4.0), ("派遣", 4.0), ("アウトソーシング", 4.0), ("BPO", 3.0), ("採用支援", 3.0)],
    "教育": [("教育", 4.0), ("学習塾", 5.0), ("研修", 2.0), ("学校", 2.0)],
    "医療/介護": [("介護", 5.0), ("医療サービス", 4.0), ("病院", 3.0), ("福祉", 3.0)],
    "食品/飲料": [("食品", 4.0), ("飲料", 4.0), ("酒類", 3.0), ("加工食品", 3.0), ("菓子", 3.0)],
    "小売": [("小売", 4.0), ("店舗", 2.0), ("スーパーマーケット", 4.0), ("ドラッグストア", 4.0), ("EC", 2.0)],
    "外食": [("外食", 5.0), ("レストラン", 3.0), ("飲食店", 4.0), ("フランチャイズ", 2.5)],
    "アパレル/化粧品": [("アパレル", 5.0), ("衣料", 3.0), ("化粧品", 5.0), ("美容", 2.0)],
    "物流": [("物流", 5.0), ("倉庫", 3.0), ("配送", 3.0), ("3PL", 4.0)],
    "空運/海運/陸運": [("航空運送", 5.0), ("空運", 5.0), ("海運", 5.0), ("陸運", 4.0), ("鉄道事業", 3.0), ("バス", 2.5)],
    "旅行/レジャー": [("旅行", 4.0), ("ホテル", 3.0), ("レジャー", 4.0), ("観光", 3.0), ("リゾート", 3.0)],
    "広告/マーケティング": [("広告", 4.0), ("マーケティング", 4.0), ("PR", 2.5), ("販促", 2.5), ("ブランディング", 3.0)],
}

DETAILED_INDUSTRY_FIELD_WEIGHTS = {
    "company_name": 1.8,
    "doc_description": 1.0,
    "business_description": 1.6,
    "product_service_info": 1.5,
    "rd_activities_text": 1.2,
    "property_info": 0.8,
    "business_risks": 0.4,
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_collection_run_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}"


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            clean = {key: to_jsonable(value) for key, value in record.items()}
            handle.write(json.dumps(clean, ensure_ascii=False) + "\n")


def stamp_collection_app_version(records: list[dict[str, Any]], app_version: str = APP_VERSION) -> None:
    for record in records:
        record["collection_app_version"] = normalize_text(record.get("collection_app_version")) or app_version


def numeric_value(record: dict[str, Any], key: str) -> float | None:
    value = normalize_text(record.get(key))
    if not value:
        return None
    value = value.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def add_integrity_flag(flags: list[str], label: str, **values: Any) -> None:
    details = ", ".join(f"{key}={normalize_text(value)}" for key, value in values.items() if normalize_text(value))
    flags.append(f"{label}({details})" if details else label)


def data_integrity_flags_for_record(record: dict[str, Any]) -> str:
    flags: list[str] = []

    net_sales = numeric_value(record, "net_sales")
    operating_income = numeric_value(record, "operating_income")
    ordinary_income = numeric_value(record, "ordinary_income")
    ordinary_profit = numeric_value(record, "ordinary_profit")
    profit_loss = numeric_value(record, "profit_loss")
    gross_profit = numeric_value(record, "gross_profit")
    rd_expenses = numeric_value(record, "rd_expenses")
    total_assets = numeric_value(record, "total_assets")
    net_assets = numeric_value(record, "net_assets")
    liabilities = numeric_value(record, "liabilities")
    equity_ratio = numeric_value(record, "equity_to_asset_ratio")
    roe = numeric_value(record, "roe")
    average_salary = numeric_value(record, "average_annual_salary")
    average_age = numeric_value(record, "average_age")
    tenure = numeric_value(record, "average_length_of_service")
    employees = numeric_value(record, "number_of_employees")

    if net_sales is not None and net_sales < 0:
        add_integrity_flag(flags, "売上高が負数", 売上高=record.get("net_sales"))
    if (
        net_sales is not None
        and operating_income is not None
        and net_sales > 0
        and abs(operating_income) > abs(net_sales)
    ):
        add_integrity_flag(flags, "営業利益の絶対値が売上高を超過", 営業利益=record.get("operating_income"), 売上高=record.get("net_sales"))
    if (
        net_sales is not None
        and operating_income is not None
        and net_sales >= 10_000_000_000
        and 0 < abs(operating_income) < 1_000_000
        and any(value is not None and abs(value) >= 100_000_000 for value in [ordinary_income, ordinary_profit, profit_loss])
    ):
        add_integrity_flag(flags, "営業利益が大規模売上に対して極端に小さい", 営業利益=record.get("operating_income"), 売上高=record.get("net_sales"))
    if net_sales is not None and gross_profit is not None and net_sales > 0 and gross_profit > net_sales:
        add_integrity_flag(flags, "粗利益が売上高を超過", 粗利益=record.get("gross_profit"), 売上高=record.get("net_sales"))
    if net_sales is not None and rd_expenses is not None and net_sales > 0 and rd_expenses > net_sales:
        add_integrity_flag(flags, "研究開発費が売上高を超過", 研究開発費=record.get("rd_expenses"), 売上高=record.get("net_sales"))

    if total_assets is not None and net_assets is not None and total_assets > 0 and net_assets > total_assets:
        add_integrity_flag(flags, "純資産が総資産を超過", 純資産=record.get("net_assets"), 総資産=record.get("total_assets"))
    if total_assets is not None and liabilities is not None and total_assets > 0 and liabilities > total_assets * 1.2:
        add_integrity_flag(flags, "負債が総資産を大幅超過", 負債=record.get("liabilities"), 総資産=record.get("total_assets"))
    if total_assets is not None and net_assets is not None and liabilities is not None and total_assets > 0:
        diff = abs(total_assets - (net_assets + liabilities))
        # Balance sheet values are especially prone to consolidated/non-consolidated
        # context mix-ups in XBRL. Treat this as a high-confidence flag only.
        if diff > max(10_000_000_000, abs(total_assets) * 0.5):
            add_integrity_flag(flags, "総資産と純資産+負債が大きく不一致", 総資産=record.get("total_assets"), 純資産=record.get("net_assets"), 負債=record.get("liabilities"))

    if equity_ratio is not None and (equity_ratio > 100 or equity_ratio < -100):
        add_integrity_flag(flags, "自己資本比率が範囲外", 自己資本比率=record.get("equity_to_asset_ratio"))
    if roe is not None and abs(roe) > 10:
        add_integrity_flag(flags, "ROEが範囲外", ROE=record.get("roe"))
    if average_salary is not None and (average_salary < 1_000_000 or average_salary > 50_000_000):
        add_integrity_flag(flags, "平均年間給与が範囲外", 平均年間給与=record.get("average_annual_salary"))
    if average_age is not None and (average_age < 15 or average_age > 80):
        add_integrity_flag(flags, "平均年齢が範囲外", 平均年齢=record.get("average_age"))
    if tenure is not None and (tenure < 0 or tenure > 60):
        add_integrity_flag(flags, "平均勤続年数が範囲外", 平均勤続年数=record.get("average_length_of_service"))
    if average_age is not None and tenure is not None and tenure > average_age:
        add_integrity_flag(flags, "平均勤続年数が平均年齢を超過", 平均勤続年数=record.get("average_length_of_service"), 平均年齢=record.get("average_age"))
    if employees is not None and (employees <= 0 or employees > 5_000_000):
        add_integrity_flag(flags, "従業員数が範囲外", 従業員数=record.get("number_of_employees"))

    return " | ".join(flags)


def annotate_data_integrity_flags(records: list[dict[str, Any]]) -> None:
    for record in records:
        record[DATA_INTEGRITY_FLAGS_COLUMN] = data_integrity_flags_for_record(record)


def split_data_integrity_flag_labels(flag_text: Any) -> list[str]:
    text = normalize_text(flag_text)
    labels: list[str] = []
    for part in text.split(" | "):
        label = part.split("(", 1)[0].strip()
        if label:
            labels.append(label)
    return labels


def summarize_data_integrity_flags(records: list[dict[str, Any]], limit: int = 8) -> str:
    counter: Counter[str] = Counter()
    for record in records:
        counter.update(split_data_integrity_flag_labels(record.get(DATA_INTEGRITY_FLAGS_COLUMN)))
    return "; ".join(f"{label}:{count}" for label, count in counter.most_common(limit))


def collection_dedupe_key(record: dict[str, Any]) -> str:
    source_id = normalize_text(record.get("source_id"))
    job_id = normalize_text(record.get("job_id"))
    corporate_number = normalize_corporate_number(record.get("corporate_number"))
    company_name = normalize_text(record.get("company_name"))
    work_location = normalize_text(record.get("work_location"))
    source_record_id = normalize_text(record.get("source_record_id"))
    if job_id:
        return f"{source_id}|job|{job_id}"
    if corporate_number:
        return f"{source_id}|corp|{corporate_number}|{company_name}|{work_location}"
    if company_name or work_location:
        return f"{source_id}|name|{company_name}|{work_location}"
    return f"{source_id}|row|{source_record_id}"


def company_identity_key(record: dict[str, Any]) -> str:
    corporate_number = normalize_corporate_number(record.get("corporate_number"))
    company_name = normalize_text(record.get("company_name"))
    office_name = normalize_text(record.get("office_name"))
    if corporate_number:
        return f"corporate_number:{corporate_number}"
    if company_name:
        return f"company_name:{company_name}|office:{office_name}"
    return ""


def load_catalog_records(catalog_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not catalog_path.exists():
        return records
    with catalog_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def rebuild_collection_store(collections_root: Path, catalog_path: Path, exclude_non_analysis_candidates: bool = False) -> dict[str, Any]:
    catalog_records = load_catalog_records(catalog_path)
    merged_by_key: dict[str, dict[str, Any]] = {}
    duplicate_groups: dict[str, list[dict[str, Any]]] = {}
    for catalog in catalog_records:
        normalized_csv = Path(str(catalog.get("normalized_csv", "")))
        if not normalized_csv.exists():
            continue
        try:
            frame = pd.read_csv(normalized_csv, encoding="utf-8-sig", dtype=str).fillna("")
        except Exception:
            continue
        for row in frame.to_dict(orient="records"):
            enriched = {column: row.get(column, "") for column in NORMALIZED_COLUMNS}
            if not normalize_text(enriched.get("collection_app_version")):
                enriched["collection_app_version"] = normalize_text(catalog.get("app_version"))
            enriched.update(
                {
                    "collection_run_id": catalog.get("collection_run_id", ""),
                    "collected_at": catalog.get("created_at", ""),
                    "source_fingerprint": catalog.get("source_fingerprint", ""),
                    "collection_status": catalog.get("status", ""),
                    "collection_manifest": catalog.get("manifest", ""),
                    "collection_normalized_csv": catalog.get("normalized_csv", ""),
                }
            )
            enriched[DATA_INTEGRITY_FLAGS_COLUMN] = data_integrity_flags_for_record(enriched)
            dedupe_key = collection_dedupe_key(enriched)
            duplicate_groups.setdefault(dedupe_key, []).append(enriched)
            merged_by_key[dedupe_key] = enriched

    store_columns = COLLECTION_STORE_EXTRA_COLUMNS + NORMALIZED_COLUMNS
    all_records = list(merged_by_key.values())
    store_csv = collections_root / "all_collected_records.csv"
    store_jsonl = collections_root / "all_collected_records.jsonl"
    catalog_csv = collections_root / "collection_catalog.csv"
    quality_summary_json = collections_root / "collection_quality_summary.json"
    quality_summary_csv = collections_root / "collection_quality_summary.csv"
    duplicate_audit_csv = collections_root / "duplicate_audit.csv"
    company_index_csv = collections_root / "collected_companies.csv"
    company_index_all_csv = collections_root / "collected_companies_all.csv"
    company_index_jsonl = collections_root / "collected_companies.jsonl"
    company_index_all_jsonl = collections_root / "collected_companies_all.jsonl"
    analysis_candidate_csv = collections_root / "analysis_candidate_companies.csv"
    analysis_candidate_jsonl = collections_root / "analysis_candidate_companies.jsonl"
    store_csv = write_user_csv(pd.DataFrame(all_records, columns=store_columns), store_csv)
    write_jsonl(store_jsonl, all_records)
    catalog_csv = write_user_csv(pd.DataFrame(catalog_records), catalog_csv)
    duplicate_rows = build_duplicate_audit_rows(duplicate_groups)
    duplicate_audit_csv = write_user_csv(pd.DataFrame(duplicate_rows, columns=DUPLICATE_AUDIT_COLUMNS), duplicate_audit_csv)
    company_rows = build_company_index_rows(all_records)
    analysis_candidate_rows = [row for row in company_rows if row.get("analysis_candidate") == "yes"]
    selected_company_rows = analysis_candidate_rows if exclude_non_analysis_candidates else company_rows
    company_index_all_csv = write_user_csv(pd.DataFrame(company_rows, columns=COMPANY_INDEX_COLUMNS), company_index_all_csv)
    write_jsonl(company_index_all_jsonl, company_rows)
    company_index_csv = write_user_csv(pd.DataFrame(selected_company_rows, columns=COMPANY_INDEX_COLUMNS), company_index_csv)
    write_jsonl(company_index_jsonl, selected_company_rows)
    analysis_candidate_csv = write_user_csv(pd.DataFrame(analysis_candidate_rows, columns=ANALYSIS_CANDIDATE_COLUMNS), analysis_candidate_csv)
    write_jsonl(analysis_candidate_jsonl, analysis_candidate_rows)
    quality_summary = build_collection_quality_summary(all_records, catalog_records, duplicate_rows, company_rows)
    with quality_summary_json.open("w", encoding="utf-8-sig") as handle:
        json.dump(quality_summary, handle, ensure_ascii=False, indent=2)
    quality_summary_csv = write_user_csv(pd.DataFrame(
        [{"metric": key, "value": value} for key, value in quality_summary.items()],
        columns=QUALITY_SUMMARY_COLUMNS,
    ), quality_summary_csv)
    return {
        "all_collected_records_csv": str(store_csv),
        "all_collected_records_jsonl": str(store_jsonl),
        "collection_catalog_csv": str(catalog_csv),
        "collection_quality_summary_json": str(quality_summary_json),
        "collection_quality_summary_csv": str(quality_summary_csv),
        "duplicate_audit_csv": str(duplicate_audit_csv),
        "collected_companies_csv": str(company_index_csv),
        "collected_companies_jsonl": str(company_index_jsonl),
        "collected_companies_all_csv": str(company_index_all_csv),
        "collected_companies_all_jsonl": str(company_index_all_jsonl),
        "analysis_candidate_companies_csv": str(analysis_candidate_csv),
        "analysis_candidate_companies_jsonl": str(analysis_candidate_jsonl),
        "exclude_non_analysis_candidates": exclude_non_analysis_candidates,
        "all_collected_record_count": len(all_records),
        "unique_company_count": len(company_rows),
        "analysis_candidate_company_count": len(analysis_candidate_rows),
        "selected_company_count": len(selected_company_rows),
        "unique_corporate_number_count": int(quality_summary.get("unique_corporate_number_count", 0) or 0),
        "xbrl_attempted_count": int(quality_summary.get("xbrl_attempted_count", 0) or 0),
        "xbrl_parsed_count": int(quality_summary.get("xbrl_parsed_count", 0) or 0),
        "average_annual_salary_present_count": int(quality_summary.get("average_annual_salary_present_count", 0) or 0),
        "duplicate_group_count": len(duplicate_rows),
        "corporate_number_missing_count": int(sum(1 for row in all_records if not normalize_corporate_number(row.get("corporate_number")))),
        "source_warning_count": int(sum(1 for row in all_records if normalize_text(row.get("source_quality_status")) != "ok")),
        "data_integrity_flagged_record_count": int(quality_summary.get("data_integrity_flagged_record_count", 0) or 0),
        "data_integrity_flagged_record_rate": float(quality_summary.get("data_integrity_flagged_record_rate", 0.0) or 0.0),
        "data_integrity_flagged_company_count": int(quality_summary.get("data_integrity_flagged_company_count", 0) or 0),
        "data_integrity_flagged_company_rate": float(quality_summary.get("data_integrity_flagged_company_rate", 0.0) or 0.0),
        "data_integrity_top_flags": quality_summary.get("data_integrity_top_flags", ""),
    }


COLLECTION_STORE_INFO_KEYS = {
    "all_collected_records_csv",
    "all_collected_records_jsonl",
    "collection_catalog_csv",
    "collection_quality_summary_json",
    "collection_quality_summary_csv",
    "duplicate_audit_csv",
    "collected_companies_csv",
    "collected_companies_jsonl",
    "collected_companies_all_csv",
    "collected_companies_all_jsonl",
    "analysis_candidate_companies_csv",
    "analysis_candidate_companies_jsonl",
    "exclude_non_analysis_candidates",
    "all_collected_record_count",
    "unique_company_count",
    "analysis_candidate_company_count",
    "selected_company_count",
    "unique_corporate_number_count",
    "xbrl_attempted_count",
    "xbrl_parsed_count",
    "average_annual_salary_present_count",
    "duplicate_group_count",
    "corporate_number_missing_count",
    "source_warning_count",
    "data_integrity_flagged_record_count",
    "data_integrity_flagged_record_rate",
    "data_integrity_flagged_company_count",
    "data_integrity_flagged_company_rate",
    "data_integrity_top_flags",
}


def latest_collection_store_info(collections_root: Path) -> dict[str, Any]:
    latest_catalog_path = collections_root / "latest_collection.json"
    if not latest_catalog_path.exists():
        return {}
    try:
        with latest_catalog_path.open("r", encoding="utf-8-sig") as handle:
            latest = json.load(handle)
    except Exception:
        return {}
    if not isinstance(latest, dict):
        return {}
    return {key: latest.get(key) for key in COLLECTION_STORE_INFO_KEYS if key in latest}


def unique_joined(records: list[dict[str, Any]], key: str) -> str:
    values = sorted({normalize_text(record.get(key)) for record in records if normalize_text(record.get(key))})
    return " | ".join(values)


def build_duplicate_audit_rows(duplicate_groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dedupe_key, records in sorted(duplicate_groups.items()):
        if len(records) <= 1:
            continue
        rows.append(
            {
                "dedupe_key": dedupe_key,
                "duplicate_count": len(records),
                "collection_run_ids": unique_joined(records, "collection_run_id"),
                "source_ids": unique_joined(records, "source_id"),
                "job_ids": unique_joined(records, "job_id"),
                "corporate_numbers": unique_joined(records, "corporate_number"),
                "company_names": unique_joined(records, "company_name"),
                "work_locations": unique_joined(records, "work_location"),
            }
        )
    return rows


def latest_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    return max(records, key=lambda record: normalize_text(record.get("collected_at")))


FINANCIAL_COLUMNS = [
    "report_submit_date",
    *EDINET_EXTRA_COLUMNS,
    *DETAILED_INDUSTRY_COLUMNS,
    "average_annual_salary",
    "average_length_of_service",
    "rd_expenses",
    "net_sales",
    "number_of_employees",
    "property_info",
    "edinet_status",
    "xbrl_parse_status",
    "xbrl_extracted_fields",
    "xbrl_missing_fields",
    "xbrl_diagnostic_notes",
]


def financial_value_count(record: dict[str, Any]) -> int:
    return sum(1 for column in FINANCIAL_COLUMNS if normalize_text(record.get(column)))


def representative_financial_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    return max(records, key=lambda record: (financial_value_count(record), normalize_text(record.get("collected_at"))))


def classify_company_for_analysis(
    company_name: str,
    corporate_number: str,
    edinet_code: str,
    doc_description: str = "",
    ordinance_code: str = "",
    form_code: str = "",
) -> tuple[str, str, str]:
    normalized_name = normalize_text(company_name)
    upper_name = normalized_name.upper()
    description = normalize_text(doc_description)
    form = normalize_text(form_code)
    if not normalized_name:
        return "unknown", "no", "company_name_missing"
    if any(keyword in description for keyword in NON_OPERATING_DOCUMENT_KEYWORDS):
        return "fund_or_finance_related", "no", "non_operating_document_description"
    if form and form not in {"030000", "0300000"} and form.startswith(("06", "07", "08", "09")):
        return "fund_or_finance_related", "no", "non_operating_document_form"
    if any(keyword.upper() in upper_name for keyword in FUND_OR_FINANCE_KEYWORDS):
        return "fund_or_finance_related", "no", "fund_or_finance_keyword"
    if not normalize_corporate_number(corporate_number):
        return "company_without_corporate_number", "review", "corporate_number_missing"
    return "operating_company_candidate", "yes", ""


def keyword_occurrence_count(text: str, keyword: str) -> int:
    if not text or not keyword:
        return 0
    if re.fullmatch(r"[A-Za-z0-9+/.-]{1,4}", keyword):
        return len(re.findall(rf"(?<![A-Z0-9]){re.escape(keyword.upper())}(?![A-Z0-9])", text.upper()))
    return text.upper().count(keyword.upper())


def evidence_snippet(fields: dict[str, str], keywords: list[str], max_length: int = 140) -> str:
    for keyword in keywords:
        if not keyword:
            continue
        keyword_upper = keyword.upper()
        for value in fields.values():
            text = normalize_text(value)
            if not text:
                continue
            pos = text.upper().find(keyword_upper)
            if pos < 0:
                continue
            start = max(0, pos - 35)
            end = min(len(text), pos + len(keyword) + 85)
            snippet = text[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            return snippet[:max_length]
    return ""


def classify_detailed_industry(fields: dict[str, Any]) -> dict[str, str]:
    text_fields = {key: normalize_text(value) for key, value in fields.items()}
    scores: dict[str, float] = {}
    evidence: dict[str, float] = {}
    for industry, keywords in DETAILED_INDUSTRY_KEYWORDS.items():
        score = 0.0
        for keyword, keyword_weight in keywords:
            keyword_score = 0.0
            for field_name, field_weight in DETAILED_INDUSTRY_FIELD_WEIGHTS.items():
                count = keyword_occurrence_count(text_fields.get(field_name, ""), keyword)
                if count:
                    keyword_score += min(count, 3) * keyword_weight * field_weight
            if keyword_score:
                score += keyword_score
                evidence[keyword] = evidence.get(keyword, 0.0) + keyword_score
        if score > 0:
            scores[industry] = score

    result = {column: "" for column in DETAILED_INDUSTRY_COLUMNS}
    if not scores:
        return result

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_candidates = ranked[:3]
    total_score = sum(score for _, score in ranked)
    for index, (industry, score) in enumerate(top_candidates, start=1):
        confidence = score / total_score if total_score else 0.0
        result[f"detailed_industry_{index}"] = industry
        result[f"detailed_industry_{index}_score"] = f"{confidence:.2f}"

    result["detailed_industry_candidates"] = " | ".join(
        f"{industry}:{(score / total_score if total_score else 0.0):.2f}" for industry, score in top_candidates
    )
    evidence_keywords = [keyword for keyword, _ in sorted(evidence.items(), key=lambda item: (-item[1], item[0]))[:8]]
    result["detailed_industry_evidence_keywords"] = ", ".join(evidence_keywords)
    result["detailed_industry_evidence_text"] = evidence_snippet(text_fields, evidence_keywords)
    return result


def detailed_industry_fields_from_record(record: dict[str, Any]) -> dict[str, str]:
    existing = {column: normalize_text(record.get(column)) for column in DETAILED_INDUSTRY_COLUMNS}
    if existing.get("detailed_industry_1"):
        return existing
    return classify_detailed_industry(
        {
            "company_name": record.get("company_name"),
            "doc_description": record.get("doc_description"),
            "business_description": record.get("business_description"),
            "product_service_info": record.get("product_service_info"),
            "rd_activities_text": record.get("rd_activities_text"),
            "property_info": record.get("property_info"),
            "business_risks": record.get("business_risks"),
        }
    )


def build_company_index_rows(all_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in all_records:
        key = company_identity_key(record)
        if not key:
            continue
        grouped.setdefault(key, []).append(record)

    rows: list[dict[str, Any]] = []
    for company_key, records in sorted(grouped.items()):
        latest = latest_record(records)
        financial = representative_financial_record(records)
        corporate_numbers = sorted({normalize_corporate_number(record.get("corporate_number")) for record in records if normalize_corporate_number(record.get("corporate_number"))})
        company_names = sorted({normalize_text(record.get("company_name")) for record in records if normalize_text(record.get("company_name"))})
        edinet_codes = sorted({normalize_text(record.get("office_name")) for record in records if normalize_text(record.get("office_name"))})
        document_ids = [normalize_text(record.get("job_id")) for record in records if normalize_text(record.get("job_id"))]
        source_ids = sorted({normalize_text(record.get("source_id")) for record in records if normalize_text(record.get("source_id"))})
        warning_count = sum(1 for record in records if normalize_text(record.get("source_quality_status")) != "ok")
        corporate_number = corporate_numbers[0] if corporate_numbers else ""
        company_name = company_names[0] if company_names else ""
        edinet_code = edinet_codes[0] if edinet_codes else ""
        category, candidate, exclusion_reason = classify_company_for_analysis(
            company_name,
            corporate_number,
            edinet_code,
            normalize_text(financial.get("doc_description")),
            normalize_text(financial.get("ordinance_code")),
            normalize_text(financial.get("form_code")),
        )
        detailed_industry = detailed_industry_fields_from_record({**financial, "company_name": company_name})
        rows.append(
            {
                "company_key": company_key,
                "corporate_number": corporate_number,
                "company_name": company_name,
                "edinet_code": edinet_code,
                "company_category_guess": category,
                "analysis_candidate": candidate,
                "analysis_exclusion_reason": exclusion_reason,
                "source_ids": ",".join(source_ids),
                "document_count": len(records),
                "latest_document_id": normalize_text(latest.get("job_id")),
                "latest_collection_run_id": normalize_text(latest.get("collection_run_id")),
                "latest_collection_app_version": normalize_text(financial.get("collection_app_version") or latest.get("collection_app_version")),
                "latest_collected_at": normalize_text(latest.get("collected_at")),
                "corporate_number_status": "present" if corporate_numbers else "missing",
                "report_submit_date": normalize_text(financial.get("report_submit_date")),
                **{column: normalize_text(financial.get(column)) for column in EDINET_EXTRA_COLUMNS},
                **detailed_industry,
                "average_annual_salary": normalize_text(financial.get("average_annual_salary")),
                "average_length_of_service": normalize_text(financial.get("average_length_of_service")),
                "rd_expenses": normalize_text(financial.get("rd_expenses")),
                "net_sales": normalize_text(financial.get("net_sales")),
                "number_of_employees": normalize_text(financial.get("number_of_employees")),
                "property_info": normalize_text(financial.get("property_info")),
                "edinet_status": normalize_text(financial.get("edinet_status")),
                "xbrl_parse_status": normalize_text(financial.get("xbrl_parse_status")),
                "xbrl_extracted_fields": normalize_text(financial.get("xbrl_extracted_fields")),
                "xbrl_missing_fields": normalize_text(financial.get("xbrl_missing_fields")),
                "xbrl_diagnostic_notes": normalize_text(financial.get("xbrl_diagnostic_notes")),
                "source_warning_count": warning_count,
                "document_ids_sample": " | ".join(document_ids[:20]),
                DATA_INTEGRITY_FLAGS_COLUMN: data_integrity_flags_for_record(financial),
            }
        )
    return rows


def build_collection_quality_summary(
    all_records: list[dict[str, Any]],
    catalog_records: list[dict[str, Any]],
    duplicate_rows: list[dict[str, Any]],
    company_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    company_rows = company_rows or []
    successful_runs = [record for record in catalog_records if record.get("status") == "success"]
    failed_runs = [record for record in catalog_records if record.get("status") == "failed"]
    partial_runs = [record for record in catalog_records if record.get("status") == "partial"]
    corporate_number_missing = [record for record in all_records if not normalize_corporate_number(record.get("corporate_number"))]
    corporate_numbers = sorted({normalize_corporate_number(record.get("corporate_number")) for record in all_records if normalize_corporate_number(record.get("corporate_number"))})
    company_keys = sorted({company_identity_key(record) for record in all_records if company_identity_key(record)})
    company_name_missing = [record for record in all_records if not normalize_text(record.get("company_name"))]
    warning_records = [record for record in all_records if normalize_text(record.get("source_quality_status")) != "ok"]
    integrity_flagged_records = [record for record in all_records if normalize_text(record.get(DATA_INTEGRITY_FLAGS_COLUMN))]
    integrity_flagged_companies = [row for row in company_rows if normalize_text(row.get(DATA_INTEGRITY_FLAGS_COLUMN))]
    sources = sorted({normalize_text(record.get("source_id")) for record in all_records if normalize_text(record.get("source_id"))})
    xbrl_parsed_records = [record for record in all_records if normalize_text(record.get("xbrl_parse_status")) == "parsed"]
    xbrl_attempted_records = [record for record in all_records if normalize_text(record.get("xbrl_parse_status")) in {"parsed", "not_parsed"}]
    average_salary_records = [record for record in all_records if normalize_text(record.get("average_annual_salary"))]
    return {
        "app_version": APP_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "catalog_run_count": len(catalog_records),
        "successful_run_count": len(successful_runs),
        "partial_run_count": len(partial_runs),
        "failed_run_count": len(failed_runs),
        "all_collected_record_count": len(all_records),
        "unique_company_count": len(company_keys),
        "analysis_candidate_company_count": sum(1 for row in company_rows if row.get("analysis_candidate") == "yes"),
        "review_company_count": sum(1 for row in company_rows if row.get("analysis_candidate") == "review"),
        "excluded_company_count": sum(1 for row in company_rows if row.get("analysis_candidate") == "no"),
        "unique_corporate_number_count": len(corporate_numbers),
        "corporate_number_present_count": len(all_records) - len(corporate_number_missing),
        "source_count": len(sources),
        "sources": ",".join(sources),
        "corporate_number_missing_count": len(corporate_number_missing),
        "company_name_missing_count": len(company_name_missing),
        "xbrl_attempted_count": len(xbrl_attempted_records),
        "xbrl_parsed_count": len(xbrl_parsed_records),
        "average_annual_salary_present_count": len(average_salary_records),
        "source_warning_count": len(warning_records),
        "data_integrity_flagged_record_count": len(integrity_flagged_records),
        "data_integrity_flagged_record_rate": (len(integrity_flagged_records) / len(all_records)) if all_records else 0.0,
        "data_integrity_flagged_company_count": len(integrity_flagged_companies),
        "data_integrity_flagged_company_rate": (len(integrity_flagged_companies) / len(company_rows)) if company_rows else 0.0,
        "data_integrity_top_flags": summarize_data_integrity_flags(integrity_flagged_companies),
        "duplicate_group_count": len(duplicate_rows),
        "duplicate_record_total": int(sum(int(row.get("duplicate_count", 0) or 0) for row in duplicate_rows)),
    }


def event(
    collection_run_id: str,
    source_id: str,
    level: str,
    event_type: str,
    message: str,
    source_record_id: str = "",
    exception_type: str = "",
    traceback_text: str = "",
) -> dict[str, Any]:
    return {
        "collection_run_id": collection_run_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source_id": source_id,
        "level": level,
        "event_type": event_type,
        "message": message,
        "source_record_id": source_record_id,
        "exception_type": exception_type,
        "traceback": traceback_text,
    }


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"データソース台帳が見つかりません: {path}")
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"入力ファイルが見つかりません: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, dtype=str).fillna("")
    raise ValueError(f"未対応の入力ファイル形式です: {path.suffix}")


def build_column_map(columns: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    normalized_columns = {normalize_column_label(column): column for column in columns}
    mapping: dict[str, str] = {}
    used_source_columns: set[str] = set()
    for target, aliases in JOB_COLUMN_ALIASES.items():
        for alias in aliases:
            source = normalized_columns.get(normalize_column_label(alias))
            if source and source not in used_source_columns:
                mapping[source] = target
                used_source_columns.add(source)
                break
    unmapped = [column for column in columns if column not in used_source_columns]
    missing = [column for column in ["company_name"] if column not in mapping.values()]
    return mapping, unmapped, missing


def normalize_record(row: dict[str, Any], row_index: int, source_id: str, column_map: dict[str, str]) -> dict[str, Any]:
    renamed: dict[str, Any] = {}
    for source_column, value in row.items():
        target = column_map.get(source_column)
        if target:
            renamed[target] = value

    record = {column: "" for column in NORMALIZED_COLUMNS}
    record["source_id"] = source_id
    record["source_record_id"] = str(row_index + 1)
    for column in JOB_COLUMN_ALIASES:
        record[column] = renamed.get(column, "")
    record["corporate_number"] = normalize_corporate_number(record.get("corporate_number"))
    for column in ["job_id", "company_name", "office_name", "job_title", "work_location"]:
        record[column] = normalize_text(record.get(column))

    notes: list[str] = []
    if not record["company_name"]:
        notes.append("company_name_missing")
    if record["corporate_number"] and not re.fullmatch(r"\d{13}", str(record["corporate_number"])):
        notes.append("corporate_number_invalid")
    record["source_quality_status"] = "ok" if not notes else "warning"
    record["source_quality_notes"] = ",".join(notes)
    return record


def collect_file_source(
    source: dict[str, Any],
    collection_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_id = str(source.get("source_id", "job_file"))
    input_path = Path(str(source.get("input_path", "")))
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    normalized_records: list[dict[str, Any]] = []

    try:
        frame = read_table(input_path)
        column_map, unmapped, missing = build_column_map([str(column) for column in frame.columns])
        events.append(event(collection_run_id, source_id, "INFO", "source_loaded", f"入力ファイルを読み込みました: {input_path}"))
        if unmapped:
            warning = event(collection_run_id, source_id, "WARNING", "unmapped_columns", "未使用の入力列があります: " + ", ".join(unmapped[:30]))
            events.append(warning)
            errors.append(warning)
        if missing:
            warning = event(collection_run_id, source_id, "WARNING", "required_columns_missing", "重要な列が不足しています: " + ", ".join(missing))
            events.append(warning)
            errors.append(warning)

        for row_index, row in enumerate(frame.to_dict(orient="records")):
            source_record_id = str(row_index + 1)
            raw_records.append(
                {
                    "collection_run_id": collection_run_id,
                    "source_id": source_id,
                    "source_record_id": source_record_id,
                    "collected_at": datetime.now().isoformat(timespec="seconds"),
                    "raw_payload": row,
                }
            )
            normalized = normalize_record(row, row_index, source_id, column_map)
            normalized_records.append(normalized)
            if normalized["source_quality_status"] != "ok":
                warning = event(
                    collection_run_id,
                    source_id,
                    "WARNING",
                    "source_record_quality_warning",
                    str(normalized["source_quality_notes"]),
                    source_record_id=source_record_id,
                )
                events.append(warning)
                errors.append(warning)
    except Exception as exc:
        error_record = event(
            collection_run_id,
            source_id,
            "ERROR",
            "source_failed",
            "収集元の処理に失敗しました",
            exception_type=type(exc).__name__,
            traceback_text=traceback.format_exc(),
        )
        events.append(error_record)
        errors.append(error_record)

    return raw_records, normalized_records, events, errors


def build_corporate_number_review_queue(normalized_records: list[dict[str, Any]], corporate_master_path: Path | None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    master = pd.DataFrame()
    if corporate_master_path and corporate_master_path.exists():
        raw_master = read_table(corporate_master_path)
        normalized_columns = {normalize_column_label(column): column for column in raw_master.columns}
        mapping: dict[str, str] = {}
        used_source_columns: set[str] = set()
        for target, aliases in CORPORATE_MASTER_COLUMN_ALIASES.items():
            for alias in aliases:
                source = normalized_columns.get(normalize_column_label(alias))
                if source and source not in used_source_columns:
                    mapping[source] = target
                    used_source_columns.add(source)
                    break
        master = raw_master.rename(columns=mapping)
        for column in CORPORATE_MASTER_COLUMN_ALIASES:
            if column not in master.columns:
                master[column] = ""
        master = master[list(CORPORATE_MASTER_COLUMN_ALIASES.keys())].copy()
        master["corporate_number"] = master["corporate_number"].map(normalize_corporate_number)
        master["company_name"] = master["company_name"].map(normalize_text)
        master["address"] = master["address"].map(normalize_text)
        master = master[master["corporate_number"].map(lambda value: bool(re.fullmatch(r"\d{13}", value or "")))].copy()

    for record in normalized_records:
        corporate_number = normalize_corporate_number(record.get("corporate_number"))
        reason = ""
        candidates: list[dict[str, Any]] = []
        if corporate_number:
            continue
        if not normalize_text(record.get("company_name")):
            reason = "company_name_missing"
        elif master.empty:
            reason = "corporate_master_missing"
        else:
            candidates = find_corporate_number_candidates(pd.Series(record), master)
            if not candidates:
                reason = "candidate_not_found"
            elif len(candidates) > 1:
                reason = "multiple_candidates"
            elif candidates[0].get("confidence", 0) < 0.92:
                reason = "low_confidence"
            else:
                reason = "candidate_found_review_recommended"
        top = candidates[0] if candidates else {}
        rows.append(
            {
                "source_id": record.get("source_id", ""),
                "source_record_id": record.get("source_record_id", ""),
                "company_name": record.get("company_name", ""),
                "work_location": record.get("work_location", ""),
                "candidate_count": len(candidates),
                "top_candidate_corporate_number": top.get("corporate_number", ""),
                "top_candidate_company_name": top.get("company_name", ""),
                "top_candidate_address": top.get("address", ""),
                "top_candidate_confidence": top.get("confidence", ""),
                "reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=REVIEW_QUEUE_COLUMNS)


def collect_edinet_source(
    source: dict[str, Any],
    collection_run_id: str,
    output_root: Path,
    lookback_days: int,
    max_records: int = 0,
    xbrl_limit: int = 20,
    target_dates: list[str] | None = None,
    stop_file: Path | None = None,
    skip_document_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_id = str(source.get("source_id", "edinet_api"))
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    normalized_records: list[dict[str, Any]] = []

    if not os.getenv("EDINET_API_KEY"):
        error_record = event(
            collection_run_id,
            source_id,
            "ERROR",
            "api_key_missing",
            "EDINET_API_KEY が未設定のためEDINET収集を実行できません",
        )
        return raw_records, normalized_records, [error_record], [error_record]

    try:
        ctx = RunContext(collection_run_id, output_root)
        setup_logging(ctx)
        target_dates = target_dates or make_recent_date_strings(lookback_days)
        frame = fetch_edinet_document_list(target_dates, ctx)
        events.append(event(collection_run_id, source_id, "INFO", "edinet_document_list_collected", f"EDINET書類一覧を取得しました: {len(frame)}件"))
        if not frame.empty:
            if skip_document_ids:
                document_id_series = frame.get("document_id", pd.Series([""] * len(frame))).fillna("").astype(str)
                frame = frame.loc[~document_id_series.isin(skip_document_ids)].copy()
                events.append(
                    event(
                        collection_run_id,
                        source_id,
                        "INFO",
                        "edinet_document_list_resume_filtered",
                        f"再開済み書類IDを除外しました。処理対象: {len(frame)}件",
                    )
                )

            def candidate_priority(row: pd.Series) -> int:
                _category, candidate, _reason = classify_company_for_analysis(
                    normalize_text(row.get("filer_name")),
                    normalize_corporate_number(row.get("corporate_number")),
                    normalize_text(row.get("edinet_code")),
                    normalize_text(row.get("doc_description")),
                    normalize_text(row.get("ordinance_code")),
                    normalize_text(row.get("form_code")),
                )
                return 0 if candidate == "yes" else 1

            frame = frame.copy()
            frame["_analysis_candidate_priority"] = frame.apply(candidate_priority, axis=1)
            frame = frame.sort_values(["_analysis_candidate_priority", "report_submit_date"], ascending=[True, False]).drop(
                columns=["_analysis_candidate_priority"]
            )
        if max_records > 0 and len(frame) > max_records:
            frame = frame.head(max_records).copy()
            events.append(event(collection_run_id, source_id, "INFO", "edinet_document_list_limited", f"最大取得件数に合わせて {max_records} 件に制限しました"))
        xbrl_attempt_count = 0
        xbrl_success_count = 0
        xbrl_attempted_corporate_numbers: set[str] = set()
        rows = frame.fillna("").to_dict(orient="records")
        total_records = len(rows)
        events.append(event(collection_run_id, source_id, "INFO", "edinet_record_processing_started", f"EDINET書類処理を開始します: {total_records}件"))
        for row_index, row in enumerate(rows):
            if stop_file and stop_file.exists():
                stop_event = event(
                    collection_run_id,
                    source_id,
                    "WARNING",
                    "collection_stop_requested",
                    "中断要求を検知したため、この時点までのデータを保存して終了します",
                )
                events.append(stop_event)
                print(f"{stop_event['timestamp']} WARNING collection | collection_stop_requested | {stop_event['message']}", flush=True)
                break
            source_record_id = str(row_index + 1)
            raw_records.append(
                {
                    "collection_run_id": collection_run_id,
                    "source_id": source_id,
                    "source_record_id": source_record_id,
                    "collected_at": datetime.now().isoformat(timespec="seconds"),
                    "raw_payload": row,
                }
            )
            corporate_number = normalize_corporate_number(row.get("corporate_number"))
            company_name = normalize_text(row.get("filer_name"))
            report_submit_date = normalize_text(row.get("report_submit_date") or row.get("submit_date") or row.get("submitDateTime"))[:10]
            if row_index == 0 or (row_index + 1) % 25 == 0 or row_index + 1 == total_records:
                progress_message = f"EDINET書類処理中: {row_index + 1}/{total_records}件目"
                if report_submit_date:
                    progress_message += f" 日付={report_submit_date}"
                if company_name:
                    progress_message += f" 企業={company_name}"
                progress = event(collection_run_id, source_id, "INFO", "edinet_record_progress", progress_message, source_record_id=source_record_id)
                events.append(progress)
                print(f"{progress['timestamp']} INFO collection | edinet_record_progress | {progress_message}", flush=True)
            notes: list[str] = []
            if not corporate_number:
                notes.append("corporate_number_missing")
            if not company_name:
                notes.append("company_name_missing")
            edinet_code = normalize_text(row.get("edinet_code"))
            document_id = normalize_text(row.get("document_id"))
            category, candidate, exclusion_reason = classify_company_for_analysis(
                company_name,
                corporate_number,
                edinet_code,
                normalize_text(row.get("doc_description")),
                normalize_text(row.get("ordinance_code")),
                normalize_text(row.get("form_code")),
            )
            xbrl_values: dict[str, Any] = {}
            edinet_status = "document_list_only"
            xbrl_parse_status = "not_requested"
            xbrl_extracted_fields = ""
            xbrl_missing_fields = ""
            xbrl_diagnostic_notes = ""
            already_attempted_company = bool(corporate_number) and corporate_number in xbrl_attempted_corporate_numbers
            should_fetch_xbrl = (
                xbrl_limit != 0
                and candidate == "yes"
                and bool(corporate_number)
                and not already_attempted_company
                and bool(document_id)
                and (xbrl_limit < 0 or xbrl_attempt_count < xbrl_limit)
            )
            if should_fetch_xbrl:
                xbrl_attempt_count += 1
                xbrl_attempted_corporate_numbers.add(corporate_number)
                xbrl_limit_label = "無制限" if xbrl_limit < 0 else str(xbrl_limit)
                xbrl_progress_message = (
                    f"XBRL取得中: {xbrl_attempt_count}/{xbrl_limit_label}社目 "
                    f"(書類 {row_index + 1}/{total_records}件目)"
                )
                if report_submit_date:
                    xbrl_progress_message += f" 日付={report_submit_date}"
                if company_name:
                    xbrl_progress_message += f" 企業={company_name}"
                xbrl_progress = event(collection_run_id, source_id, "INFO", "xbrl_company_progress", xbrl_progress_message, source_record_id=source_record_id)
                events.append(xbrl_progress)
                print(f"{xbrl_progress['timestamp']} INFO collection | xbrl_company_progress | {xbrl_progress_message}", flush=True)
                xbrl_bytes = fetch_edinet_xbrl(document_id, ctx)
                xbrl_values = parse_xbrl_to_edinet_record(xbrl_bytes, corporate_number, ctx)
                if xbrl_values:
                    xbrl_success_count += 1
                    edinet_status = "xbrl_parsed"
                    xbrl_parse_status = "parsed"
                    xbrl_extracted_fields = normalize_text(xbrl_values.get("_xbrl_extracted_fields"))
                    xbrl_missing_fields = normalize_text(xbrl_values.get("_xbrl_missing_fields"))
                    if xbrl_missing_fields:
                        xbrl_diagnostic_notes = f"一部XBRL項目を抽出できませんでした: {xbrl_missing_fields}"
                    else:
                        xbrl_diagnostic_notes = "XBRL項目を抽出しました"
                else:
                    edinet_status = "xbrl_not_parsed"
                    xbrl_parse_status = "not_parsed"
                    xbrl_diagnostic_notes = "XBRL ZIPを取得できない、または解析できませんでした"
                    notes.append("xbrl_not_parsed")
            elif xbrl_limit == 0:
                xbrl_diagnostic_notes = "XBRL取得上限が0のためスキップしました"
            elif candidate != "yes":
                xbrl_parse_status = "skipped"
                xbrl_diagnostic_notes = f"分析候補外のためスキップしました: {exclusion_reason or category}"
            elif already_attempted_company:
                xbrl_parse_status = "skipped"
                xbrl_diagnostic_notes = "同じ法人番号のXBRLをすでに確認したためスキップしました"
            else:
                xbrl_parse_status = "skipped"
                xbrl_diagnostic_notes = f"XBRL取得上限 {xbrl_limit} 件に達したためスキップしました"
            detailed_industry = classify_detailed_industry(
                {
                    "company_name": company_name,
                    "doc_description": normalize_text(row.get("doc_description")),
                    "business_description": xbrl_values.get("business_description"),
                    "product_service_info": xbrl_values.get("product_service_info"),
                    "rd_activities_text": xbrl_values.get("rd_activities_text"),
                    "property_info": xbrl_values.get("property_info"),
                    "business_risks": xbrl_values.get("business_risks"),
                }
            )
            normalized_records.append(
                {
                    "source_id": source_id,
                    "source_record_id": source_record_id,
                    "job_id": document_id,
                    "corporate_number": corporate_number,
                    "company_name": company_name,
                    "office_name": edinet_code,
                    "job_title": "EDINET提出企業",
                    "work_location": "",
                    "basic_salary_min": "",
                    "basic_salary_max": "",
                    "annual_holidays": "",
                    "overtime_hours_avg": "",
                    "report_submit_date": report_submit_date,
                    **{column: normalize_text(row.get(column)) for column in EDINET_DOCUMENT_METADATA_COLUMNS},
                    **{column: normalize_text(xbrl_values.get(column)) for column in EDINET_XBRL_EXTRA_COLUMNS},
                    **detailed_industry,
                    "average_annual_salary": xbrl_values.get("average_annual_salary", ""),
                    "average_length_of_service": xbrl_values.get("average_length_of_service", ""),
                    "rd_expenses": xbrl_values.get("rd_expenses", ""),
                    "net_sales": xbrl_values.get("net_sales", ""),
                    "number_of_employees": xbrl_values.get("number_of_employees", ""),
                    "property_info": normalize_text(xbrl_values.get("property_info")),
                    "edinet_status": edinet_status,
                    "xbrl_parse_status": xbrl_parse_status,
                    "xbrl_extracted_fields": xbrl_extracted_fields,
                    "xbrl_missing_fields": xbrl_missing_fields,
                    "xbrl_diagnostic_notes": xbrl_diagnostic_notes,
                    "source_quality_status": "ok" if not notes else "warning",
                    "source_quality_notes": ",".join(notes),
                }
            )
        events.append(event(collection_run_id, source_id, "INFO", "xbrl_collection_finished", f"XBRL取得を {xbrl_attempt_count} 件試行し、{xbrl_success_count} 件解析しました"))
        for ctx_error in ctx.errors:
            converted = event(
                collection_run_id,
                source_id,
                str(ctx_error.get("level", "WARNING")),
                str(ctx_error.get("event_type", "edinet_warning")),
                str(ctx_error.get("message", "")),
                exception_type=str(ctx_error.get("exception_type") or ""),
                traceback_text=str(ctx_error.get("traceback") or ""),
            )
            events.append(converted)
            if converted["level"] == "ERROR":
                errors.append(converted)
    except Exception as exc:
        error_record = event(
            collection_run_id,
            source_id,
            "ERROR",
            "source_failed",
            "EDINET収集に失敗しました",
            exception_type=type(exc).__name__,
            traceback_text=traceback.format_exc(),
        )
        events.append(error_record)
        errors.append(error_record)
    return raw_records, normalized_records, events, errors


def save_collection_outputs(
    *,
    collection_run_id: str,
    output_root: Path,
    registry_path: Path,
    source: dict[str, Any],
    source_fingerprint: str,
    started_at: str,
    raw_records: list[dict[str, Any]],
    normalized_records: list[dict[str, Any]],
    events: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    corporate_master_path: Path | None,
    collections_root: Path,
    catalog_path: Path,
    exclude_non_analysis_candidates: bool,
    previous_same_input: dict[str, Any] | None = None,
    edinet_lookback_days: int | str = "",
    max_records: int | str = "",
    edinet_xbrl_limit: int | str = "",
    extra_manifest: dict[str, Any] | None = None,
    extra_catalog: dict[str, Any] | None = None,
    rebuild_store: bool = True,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    stamp_collection_app_version(normalized_records)
    annotate_data_integrity_flags(normalized_records)
    status = "success"
    if any(item.get("level") == "ERROR" for item in errors):
        status = "failed"
    elif errors:
        status = "partial"

    output_root.mkdir(parents=True, exist_ok=True)
    raw_path = output_root / "raw_collected_records.jsonl"
    normalized_path = output_root / "normalized_records.csv"
    events_path = output_root / "collection_events.jsonl"
    errors_path = output_root / "collection_errors.jsonl"
    review_queue_path = output_root / "corporate_number_review_queue.csv"
    manifest_path = output_root / "collection_manifest.json"

    write_jsonl(raw_path, raw_records)
    pd.DataFrame(normalized_records, columns=NORMALIZED_COLUMNS).to_csv(normalized_path, index=False, encoding="utf-8-sig")
    review_queue = build_corporate_number_review_queue(normalized_records, corporate_master_path)
    review_queue_path = write_user_csv(review_queue, review_queue_path)
    write_jsonl(events_path, events)
    write_jsonl(errors_path, errors)

    manifest = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "collection_run_id": collection_run_id,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "registry": str(registry_path),
        "source_fingerprint": source_fingerprint,
        "edinet_lookback_days": edinet_lookback_days,
        "max_records": max_records,
        "edinet_xbrl_limit": edinet_xbrl_limit,
        "exclude_non_analysis_candidates": bool(exclude_non_analysis_candidates),
        "xbrl_parsed_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") == "parsed"),
        "xbrl_attempted_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") in {"parsed", "not_parsed"}),
        "same_input_as_previous": previous_same_input is not None,
        "previous_same_input_run_id": previous_same_input.get("collection_run_id") if previous_same_input else "",
        "sources": [
            {
                "source_id": source.get("source_id"),
                "source_type": source.get("source_type"),
                "status": status,
                "input": source.get("input_path"),
                "record_count": len(normalized_records),
                "error_count": len(errors),
            }
        ],
        "output_files": {
            "raw_jsonl": str(raw_path),
            "normalized_csv": str(normalized_path),
            "events_jsonl": str(events_path),
            "errors_jsonl": str(errors_path),
            "corporate_number_review_queue_csv": str(review_queue_path),
            "manifest": str(manifest_path),
        },
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    with manifest_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    catalog_record = {
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "collection_run_id": collection_run_id,
        "created_at": manifest["finished_at"],
        "source_id": source.get("source_id"),
        "source_type": source.get("source_type"),
        "input": source.get("input_path"),
        "source_fingerprint": source_fingerprint,
        "same_input_as_previous": previous_same_input is not None,
        "previous_same_input_run_id": previous_same_input.get("collection_run_id") if previous_same_input else "",
        "status": status,
        "record_count": len(normalized_records),
        "error_count": len(errors),
        "xbrl_parsed_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") == "parsed"),
        "xbrl_attempted_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") in {"parsed", "not_parsed"}),
        "corporate_number_review_count": int(len(review_queue)),
        "manifest": str(manifest_path),
        "normalized_csv": str(normalized_path),
        "corporate_number_review_queue_csv": str(review_queue_path),
        "exclude_non_analysis_candidates": bool(exclude_non_analysis_candidates),
    }
    if extra_catalog:
        catalog_record.update(extra_catalog)
    with catalog_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(catalog_record, ensure_ascii=False) + "\n")

    if rebuild_store:
        store_info = rebuild_collection_store(collections_root, catalog_path, bool(exclude_non_analysis_candidates))
        catalog_record["store_rebuild_skipped"] = False
        catalog_record["store_rebuild_pending"] = False
    else:
        store_info = latest_collection_store_info(collections_root)
        catalog_record["store_rebuild_skipped"] = True
        catalog_record["store_rebuild_pending"] = True
    catalog_record.update(store_info)
    latest_catalog_path = collections_root / "latest_collection.json"
    with latest_catalog_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(catalog_record, handle, ensure_ascii=False, indent=2)
    return status, manifest, catalog_record


def continuous_state_path(collections_root: Path) -> Path:
    return collections_root / "continuous_edinet_state.json"


def continuous_stop_path(collections_root: Path) -> Path:
    return collections_root / "continuous_edinet_stop.request"


def load_continuous_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_continuous_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def choose_next_continuous_date(state: dict[str, Any]) -> str:
    today = datetime.now().date()
    processed_dates = {normalize_text(item) for item in state.get("processed_dates", []) if normalize_text(item)}
    cursor_text = normalize_text(state.get("oldest_next_date"))
    try:
        cursor = datetime.fromisoformat(cursor_text).date() if cursor_text else today
    except ValueError:
        cursor = today
    if cursor > today:
        cursor = today
    day = today
    while day >= cursor:
        date_text = day.isoformat()
        if date_text not in processed_dates:
            return date_text
        day -= timedelta(days=1)
    return (cursor - timedelta(days=1)).isoformat()


def normalize_processed_document_versions(state: dict[str, Any], previous_app_version: str) -> dict[str, str]:
    raw_versions = state.get("processed_document_versions", {})
    if isinstance(raw_versions, dict):
        versions = {
            normalize_text(document_id): normalize_text(version) or "unknown"
            for document_id, version in raw_versions.items()
            if normalize_text(document_id)
        }
    else:
        versions = {}
    legacy_version = previous_app_version or "unknown"
    for document_id in state.get("processed_document_ids", []):
        document_id = normalize_text(document_id)
        if document_id and document_id not in versions:
            versions[document_id] = legacy_version
    return versions


def prepare_continuous_state_for_app_version(state: dict[str, Any]) -> tuple[dict[str, str], bool, str]:
    previous_app_version = normalize_text(state.get("app_version"))
    processed_versions = normalize_processed_document_versions(state, previous_app_version)
    has_previous_collection = bool(processed_versions or state.get("processed_dates"))
    version_changed = has_previous_collection and previous_app_version != APP_VERSION
    if version_changed:
        state["previous_app_version"] = previous_app_version or "unknown"
        state["version_mismatch_detected_at"] = datetime.now().isoformat(timespec="seconds")
        state["version_mismatch_action"] = "restart_from_latest_and_refetch_non_current_version_documents"
        state["processed_dates"] = []
        state["oldest_next_date"] = datetime.now().date().isoformat()
    state["app_version"] = APP_VERSION
    state["app_build_date"] = APP_BUILD_DATE
    state["processed_document_versions"] = dict(sorted(processed_versions.items()))
    state["processed_document_ids"] = sorted(processed_versions)
    return processed_versions, version_changed, previous_app_version


def run_continuous_edinet_collection(
    args: argparse.Namespace,
    source: dict[str, Any],
    registry_path: Path,
    collections_root: Path,
    catalog_path: Path,
) -> int:
    if not os.getenv("EDINET_API_KEY"):
        print("EDINET_API_KEY が未設定のため、止めるまで収集モードを開始できません", flush=True)
        return 1

    state_file = Path(args.continuous_state_file) if args.continuous_state_file else continuous_state_path(collections_root)
    stop_file = Path(args.continuous_stop_file) if args.continuous_stop_file else continuous_stop_path(collections_root)
    if stop_file.exists():
        stop_file.unlink()
    state = load_continuous_state(state_file)
    state.setdefault("mode", "continuous_edinet")
    state.setdefault("processed_dates", [])
    state.setdefault("processed_document_ids", [])
    state.setdefault("oldest_next_date", datetime.now().date().isoformat())
    processed_document_versions, version_changed, previous_app_version = prepare_continuous_state_for_app_version(state)
    state["status"] = "running"
    state["last_started_at"] = datetime.now().isoformat(timespec="seconds")
    state["stop_file"] = str(stop_file)
    refresh_all = bool(getattr(args, "continuous_refresh_all", False))
    state["continuous_refresh_all"] = refresh_all
    if refresh_all:
        state["processed_dates"] = []
        state["oldest_next_date"] = datetime.now().date().isoformat()
    save_continuous_state(state_file, state)
    if version_changed:
        print(
            f"{state['last_started_at']} INFO collection | continuous_app_version_changed | "
            f"前回ver={previous_app_version or 'unknown'} 現在ver={APP_VERSION}。直近日付から再補完します。",
            flush=True,
        )
    if refresh_all:
        print(
            f"{state['last_started_at']} WARNING collection | continuous_refresh_all_enabled | "
            "既存データも含めて全書類を再取得します。処理済み書類IDによるスキップは無効です。",
            flush=True,
        )

    corporate_master_path = Path(args.corporate_master) if args.corporate_master else None
    saved_batches = 0
    pending_store_rebuild = False
    store_rebuild_interval = max(0, int(getattr(args, "continuous_store_rebuild_interval", 0) or 0))
    last_catalog: dict[str, Any] = {}
    while True:
        if stop_file.exists():
            break
        date_text = choose_next_continuous_date(state)
        processed_document_versions = normalize_processed_document_versions(state, normalize_text(state.get("app_version")))
        current_version_document_ids = set()
        if not refresh_all:
            current_version_document_ids = {
                document_id
                for document_id, version in processed_document_versions.items()
                if normalize_text(version) == APP_VERSION
            }
        collection_run_id = make_collection_run_id()
        output_root = collections_root / collection_run_id
        started_at = datetime.now().isoformat(timespec="seconds")
        progress_message = f"止めるまで収集中: 日付={date_text} 保存単位={saved_batches + 1}"
        print(f"{started_at} INFO collection | continuous_date_started | {progress_message}", flush=True)

        raw_records, normalized_records, events, errors = collect_edinet_source(
            source,
            collection_run_id,
            output_root,
            lookback_days=1,
            max_records=0,
            xbrl_limit=-1,
            target_dates=[date_text],
            stop_file=stop_file,
            skip_document_ids=current_version_document_ids,
        )
        if normalized_records:
            for record in normalized_records:
                document_id = normalize_text(record.get("job_id"))
                if document_id:
                    processed_document_versions[document_id] = APP_VERSION
            state["processed_document_versions"] = dict(sorted(processed_document_versions.items()))
            state["processed_document_ids"] = sorted(processed_document_versions)

        interrupted = stop_file.exists() or any(item.get("event_type") == "collection_stop_requested" for item in events)
        if not interrupted:
            processed_dates = {normalize_text(item) for item in state.get("processed_dates", []) if normalize_text(item)}
            processed_dates.add(date_text)
            state["processed_dates"] = sorted(processed_dates)
            try:
                current_oldest = datetime.fromisoformat(normalize_text(state.get("oldest_next_date"))).date()
            except ValueError:
                current_oldest = datetime.now().date()
            processed_day = datetime.fromisoformat(date_text).date()
            if processed_day <= current_oldest:
                state["oldest_next_date"] = (processed_day - timedelta(days=1)).isoformat()

        should_save_batch = bool(raw_records or normalized_records or errors or interrupted)
        if should_save_batch:
            next_batch_number = saved_batches + 1
            should_rebuild_store = interrupted or (
                store_rebuild_interval > 0 and next_batch_number % store_rebuild_interval == 0
            )
            source_fingerprint = f"edinet_api:continuous:date={date_text}:xbrl=unlimited"
            extra = {
                "continuous_mode": True,
                "continuous_date": date_text,
                "continuous_state_file": str(state_file),
                "continuous_stop_file": str(stop_file),
                "continuous_interrupted": interrupted,
                "continuous_app_version": APP_VERSION,
                "continuous_previous_app_version": previous_app_version,
                "continuous_version_changed": version_changed,
                "continuous_refresh_all": refresh_all,
                "continuous_store_rebuild_interval": store_rebuild_interval,
                "continuous_store_rebuilt": should_rebuild_store,
            }
            print(
                f"{datetime.now().isoformat(timespec='seconds')} INFO collection | continuous_batch_save_started | "
                f"個別CSV保存中: 日付={date_text} record_count={len(normalized_records)} "
                f"集約CSV={'更新' if should_rebuild_store else '中断時に更新'}",
                flush=True,
            )
            status, manifest, last_catalog = save_collection_outputs(
                collection_run_id=collection_run_id,
                output_root=output_root,
                registry_path=registry_path,
                source=source,
                source_fingerprint=source_fingerprint,
                started_at=started_at,
                raw_records=raw_records,
                normalized_records=normalized_records,
                events=events,
                errors=errors,
                corporate_master_path=corporate_master_path,
                collections_root=collections_root,
                catalog_path=catalog_path,
                exclude_non_analysis_candidates=bool(args.exclude_non_analysis_candidates),
                edinet_lookback_days="continuous",
                max_records="unlimited",
                edinet_xbrl_limit="unlimited",
                extra_manifest=extra,
                extra_catalog=extra,
                rebuild_store=should_rebuild_store,
            )
            saved_batches += 1
            pending_store_rebuild = not should_rebuild_store
            save_message = (
                f"定期保存完了: 日付={date_text} record_count={len(normalized_records)} "
                f"XBRL解析={manifest.get('xbrl_parsed_count', 0)} status={status} "
                f"集約CSV={'更新' if should_rebuild_store else '後で更新'}"
            )
            print(f"{datetime.now().isoformat(timespec='seconds')} INFO collection | continuous_batch_saved | {save_message}", flush=True)
        else:
            print(
                f"{datetime.now().isoformat(timespec='seconds')} INFO collection | continuous_empty_date_skipped | "
                f"保存対象なし: 日付={date_text} 対象書類0件。CSV再構築を省略して次の日付へ進みます。",
                flush=True,
            )
        state["last_saved_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_processed_date"] = date_text
        state["last_collection_run_id"] = collection_run_id
        save_continuous_state(state_file, state)
        if interrupted:
            break

    if pending_store_rebuild:
        print(
            f"{datetime.now().isoformat(timespec='seconds')} INFO collection | continuous_final_store_rebuild_started | "
            "中断前の未反映バッチを集約CSVへ反映しています。",
            flush=True,
        )
        store_info = rebuild_collection_store(collections_root, catalog_path, bool(args.exclude_non_analysis_candidates))
        last_catalog.update(store_info)
        last_catalog["store_rebuild_skipped"] = False
        last_catalog["store_rebuild_pending"] = False
        last_catalog["continuous_final_store_rebuilt"] = True
        latest_catalog_path = collections_root / "latest_collection.json"
        with latest_catalog_path.open("w", encoding="utf-8-sig") as handle:
            json.dump(last_catalog, handle, ensure_ascii=False, indent=2)
        print(
            f"{datetime.now().isoformat(timespec='seconds')} INFO collection | continuous_final_store_rebuild_finished | "
            f"集約CSV更新完了: 企業数={store_info.get('selected_company_count', 0)}",
            flush=True,
        )

    state["status"] = "stopped"
    state["last_stopped_at"] = datetime.now().isoformat(timespec="seconds")
    state["saved_batch_count"] = int(state.get("saved_batch_count", 0) or 0) + saved_batches
    save_continuous_state(state_file, state)
    print(
        json.dumps(
            {
                "continuous_mode": True,
                "status": "stopped",
                "saved_batches": saved_batches,
                "state_file": str(state_file),
                "latest_collection": last_catalog,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="企業データ収集ジョブ")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="データソース台帳JSON")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="出力フォルダ")
    parser.add_argument("--source-id", default="job_file", help="実行するsource_id")
    parser.add_argument("--input-file", default="", help="台帳のinput_pathを一時的に上書きします")
    parser.add_argument("--corporate-master", default="", help="法人番号確認キュー作成に使うローカルマスタ")
    parser.add_argument("--edinet-lookback-days", type=int, default=1, help="EDINET書類一覧を遡る日数")
    parser.add_argument("--max-records", type=int, default=0, help="最大取得件数。0なら制限なし")
    parser.add_argument("--edinet-xbrl-limit", type=int, default=20, help="EDINET XBRLを取得・解析する最大件数。0なら取得しません")
    parser.add_argument("--exclude-non-analysis-candidates", action="store_true", help="企業一覧CSVから分析候補外を除外します。全件版はcollected_companies_all.csvに残します")
    parser.add_argument("--continuous-edinet", action="store_true", help="中断要求があるまでEDINETを日付単位で収集し続けます")
    parser.add_argument("--continuous-state-file", default="", help="止めるまで収集モードの再開状態ファイル")
    parser.add_argument("--continuous-stop-file", default="", help="このファイルが作成されたら止めるまで収集モードを中断します")
    parser.add_argument("--continuous-store-rebuild-interval", type=int, default=0, help="止めるまで収集中に集約CSVを再構築する保存バッチ間隔。0なら中断時だけ更新します")
    parser.add_argument("--continuous-refresh-all", action="store_true", help="止めるまで収集で処理済み書類IDもスキップせず、直近日付から全データを再取得します。既存データが疑わしい時に使います")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} collector v{APP_VERSION} ({APP_BUILD_DATE})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry)
    started_at = datetime.now().isoformat(timespec="seconds")
    registry = load_registry(registry_path)
    sources = registry.get("sources", [])
    selected_sources = [source for source in sources if source.get("source_id") == args.source_id]
    if not selected_sources:
        raise ValueError(f"収集元が見つかりません: {args.source_id}")
    source = dict(selected_sources[0])
    if args.input_file:
        source["input_path"] = args.input_file
    collections_root = Path(args.output_dir) / "collections"
    catalog_path = collections_root / "collection_catalog.jsonl"
    if args.continuous_edinet:
        if args.source_id != "edinet_api":
            raise ValueError("止めるまで収集モードはEDINET API収集元でのみ使えます")
        return run_continuous_edinet_collection(args, source, registry_path, collections_root, catalog_path)
    collection_run_id = make_collection_run_id()
    output_root = collections_root / collection_run_id
    output_root.mkdir(parents=True, exist_ok=True)
    input_path = Path(str(source.get("input_path", "")))
    if args.source_id == "job_file":
        source_fingerprint = file_sha256(input_path) if input_path.exists() and input_path.is_file() else ""
    else:
        source_fingerprint = f"{args.source_id}:{datetime.now().date().isoformat()}:lookback={args.edinet_lookback_days}:max={args.max_records}:xbrl={args.edinet_xbrl_limit}"

    previous_same_input: dict[str, Any] | None = None
    if catalog_path.exists() and source_fingerprint:
        for item in load_catalog_records(catalog_path):
            if item.get("source_id") == source.get("source_id") and item.get("source_fingerprint") == source_fingerprint:
                previous_same_input = item

    if args.source_id == "job_file":
        raw_records, normalized_records, events, errors = collect_file_source(source, collection_run_id)
    elif args.source_id == "edinet_api":
        raw_records, normalized_records, events, errors = collect_edinet_source(
            source,
            collection_run_id,
            output_root,
            args.edinet_lookback_days,
            args.max_records,
            args.edinet_xbrl_limit,
        )
    else:
        error_record = event(
            collection_run_id,
            args.source_id,
            "ERROR",
            "source_not_implemented",
            f"収集元はまだ未実装です: {args.source_id}",
        )
        raw_records, normalized_records, events, errors = [], [], [error_record], [error_record]
    stamp_collection_app_version(normalized_records)
    annotate_data_integrity_flags(normalized_records)
    status = "success"
    if any(item.get("level") == "ERROR" for item in errors):
        status = "failed"
    elif errors:
        status = "partial"

    raw_path = output_root / "raw_collected_records.jsonl"
    normalized_path = output_root / "normalized_records.csv"
    events_path = output_root / "collection_events.jsonl"
    errors_path = output_root / "collection_errors.jsonl"
    review_queue_path = output_root / "corporate_number_review_queue.csv"
    manifest_path = output_root / "collection_manifest.json"

    write_jsonl(raw_path, raw_records)
    pd.DataFrame(normalized_records, columns=NORMALIZED_COLUMNS).to_csv(normalized_path, index=False, encoding="utf-8-sig")
    corporate_master_path = Path(args.corporate_master) if args.corporate_master else None
    review_queue = build_corporate_number_review_queue(normalized_records, corporate_master_path)
    review_queue_path = write_user_csv(review_queue, review_queue_path)
    write_jsonl(events_path, events)
    write_jsonl(errors_path, errors)

    manifest = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "collection_run_id": collection_run_id,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "registry": str(registry_path),
        "source_fingerprint": source_fingerprint,
        "edinet_lookback_days": args.edinet_lookback_days,
        "max_records": args.max_records,
        "edinet_xbrl_limit": args.edinet_xbrl_limit,
        "exclude_non_analysis_candidates": bool(args.exclude_non_analysis_candidates),
        "xbrl_parsed_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") == "parsed"),
        "xbrl_attempted_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") in {"parsed", "not_parsed"}),
        "same_input_as_previous": previous_same_input is not None,
        "previous_same_input_run_id": previous_same_input.get("collection_run_id") if previous_same_input else "",
        "sources": [
            {
                "source_id": source.get("source_id"),
                "source_type": source.get("source_type"),
                "status": status,
                "input": source.get("input_path"),
                "record_count": len(normalized_records),
                "error_count": len(errors),
            }
        ],
        "output_files": {
            "raw_jsonl": str(raw_path),
            "normalized_csv": str(normalized_path),
            "events_jsonl": str(events_path),
            "errors_jsonl": str(errors_path),
            "corporate_number_review_queue_csv": str(review_queue_path),
            "manifest": str(manifest_path),
        },
    }
    with manifest_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    catalog_record = {
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "collection_run_id": collection_run_id,
        "created_at": manifest["finished_at"],
        "source_id": source.get("source_id"),
        "source_type": source.get("source_type"),
        "input": source.get("input_path"),
        "source_fingerprint": source_fingerprint,
        "same_input_as_previous": previous_same_input is not None,
        "previous_same_input_run_id": previous_same_input.get("collection_run_id") if previous_same_input else "",
        "status": status,
        "record_count": len(normalized_records),
        "error_count": len(errors),
        "xbrl_parsed_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") == "parsed"),
        "xbrl_attempted_count": sum(1 for record in normalized_records if record.get("xbrl_parse_status") in {"parsed", "not_parsed"}),
        "corporate_number_review_count": int(len(review_queue)),
        "manifest": str(manifest_path),
        "normalized_csv": str(normalized_path),
        "corporate_number_review_queue_csv": str(review_queue_path),
        "exclude_non_analysis_candidates": bool(args.exclude_non_analysis_candidates),
    }
    with catalog_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(catalog_record, ensure_ascii=False) + "\n")
    store_info = rebuild_collection_store(collections_root, catalog_path, bool(args.exclude_non_analysis_candidates))
    catalog_record.update(store_info)
    latest_catalog_path = collections_root / "latest_collection.json"
    with latest_catalog_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(catalog_record, handle, ensure_ascii=False, indent=2)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
