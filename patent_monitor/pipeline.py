from __future__ import annotations

import csv
import base64
import gzip
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import statistics
import time
import unicodedata
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

APP_VERSION = "0.10.1"
PARSER_VERSION = "epo-ops-xml-v3"
PROMPT_VERSION = "patent-materiality-ja-v2-independent"


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).upper()
    # OPS applicant names frequently include a trailing country marker such as
    # ``FUJITSU LTD [JP]``.  It is metadata, not part of the legal name.
    text = re.sub(r"\[[A-Z]{2}\]", " ", text)
    text = re.sub(r"\bIP\s+MAN\b", " INTELLECTUAL PROPERTY MANAGEMENT ", text)
    text = re.sub(r"\bMFG\b", " MANUFACTURING ", text)
    text = re.sub(r"\bIND\b", " INDUSTRIES ", text)
    text = text.replace("도레이 카부시키가이샤", "TORAY INDUSTRIES")
    text = re.sub(r"^(?:株式会社|有限会社|合同会社)", "", text)
    text = re.sub(r"(?:株式会社|有限会社|合同会社|ホールディングス)$", "", text)
    text = re.sub(r"\bKABUSHIKI\s+KAISHA\b", " ", text)
    text = re.sub(
        r"\b(?:HOLDINGS?|CORPORATION|CORP|INCORPORATED|INC|LIMITED|LTD|CO|COMPANY|GROUP)\b\.?,?",
        " ",
        text,
    )
    return re.sub(r"[^0-9A-Z一-龥ぁ-んァ-ヶ]", "", text)


def split_values(value: Any) -> list[str]:
    return [x.strip() for x in re.split(r"[|;；\n]", clean_text(value)) if x.strip()]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = clean_text(value).replace(",", "")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def character_ngrams(text: str, n: int = 3) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", clean_text(text)).lower()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        return Counter()
    if len(normalized) < n:
        return Counter({normalized: 1})
    return Counter(normalized[i : i + n] for i in range(len(normalized) - n + 1))


def cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[key] * right[key] for key in common)
    norm_left = math.sqrt(sum(value * value for value in left.values()))
    norm_right = math.sqrt(sum(value * value for value in right.values()))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


@dataclass
class PatentRecord:
    source: str
    publication_number: str
    application_number: str = ""
    family_id: str = ""
    priority_number: str = ""
    title: str = ""
    abstract: str = ""
    claims: str = ""
    applicants: list[str] = field(default_factory=list)
    inventors: list[str] = field(default_factory=list)
    ipc_codes: list[str] = field(default_factory=list)
    cpc_codes: list[str] = field(default_factory=list)
    filing_date: str = ""
    publication_date: str = ""
    priority_date: str = ""
    country_codes: list[str] = field(default_factory=list)
    citation_count: int = 0
    family_size: int = 1
    claim_count: int = 0
    legal_status: str = ""
    priority_numbers: list[str] = field(default_factory=list)
    family_members: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    cited_by: list[str] = field(default_factory=list)
    non_patent_citations: list[str] = field(default_factory=list)
    legal_events: list[dict[str, Any]] = field(default_factory=list)
    ownership_events: list[dict[str, Any]] = field(default_factory=list)
    retrieved_at: str = ""
    source_endpoint: str = ""
    query_window_start: str = ""
    query_window_end: str = ""
    payload_hash: str = ""
    parser_version: str = PARSER_VERSION
    source_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def detail_enriched(self) -> bool:
        return bool(self.raw.get("epo_detail_enriched"))
    @property
    def identity(self) -> str:
        value = self.publication_number or self.application_number
        if value:
            return re.sub(r"[^0-9A-Z]", "", value.upper())
        material = "|".join([self.title, self.priority_date, *self.applicants])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    @property
    def family_key(self) -> str:
        raw = self.family_id or self.priority_number
        if raw:
            return re.sub(r"[^0-9A-Z]", "", raw.upper())
        material = "|".join([normalize_name(self.title), self.priority_date, normalize_name(self.applicants[0] if self.applicants else "")])
        return "SYN-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]

    @property
    def searchable_text(self) -> str:
        return " ".join([self.title, self.abstract, self.claims, *self.ipc_codes, *self.cpc_codes])


def _record_epo_detail_issue(
    record: PatentRecord,
    kind: str,
    exc: Exception,
    summary: dict[str, Any],
) -> None:
    message = str(exc)
    if "CLIENT.InvalidCountryCode" in message:
        category = "unsupported_country"
    elif "HTTP 404 Not Found" in message or "SERVER.EntityNotFound" in message:
        category = "not_found"
    else:
        category = ""
    if category:
        key = f"{kind}.{category}"
        warnings = record.raw.setdefault("epo_detail_warnings", [])
        if not warnings:
            summary["warning_count"] += 1
        warnings.append(key)
        summary["warning_detail_count"] += 1
        summary["warning_kind_counts"][key] = summary["warning_kind_counts"].get(key, 0) + 1
        return
    errors = record.raw.setdefault("epo_detail_errors", [])
    if not errors:
        summary["error_count"] += 1
    errors.append(f"{kind}: {message}")
    summary["error_detail_count"] += 1
    summary["error_kind_counts"][kind] = summary["error_kind_counts"].get(kind, 0) + 1


@dataclass
class Company:
    company_id: str
    company_name: str
    ticker: str = ""
    aliases: list[str] = field(default_factory=list)
    subsidiaries: list[str] = field(default_factory=list)
    market_cap_jpy: float = 0.0
    target: bool = True
    technology_tags: list[str] = field(default_factory=list)

    @property
    def all_names(self) -> list[str]:
        return [self.company_name, *self.aliases, *self.subsidiaries]

    def search_names(self, limit: int) -> list[str]:
        """Return applicant names ordered for EPO search, without limiting matcher aliases."""
        names = list(dict.fromkeys(name.replace('"', " ").strip() for name in self.all_names if name.strip()))
        if limit <= 0:
            return names

        def priority(name: str) -> tuple[int, int]:
            normalized = unicodedata.normalize("NFKC", name)
            ascii_like = bool(re.fullmatch(r"[0-9A-Za-z .,&'()/-]+", normalized))
            is_ip_holder = bool(re.search(r"\b(?:IP|INTELLECTUAL PROPERTY|PATENT)\b", normalized.upper()))
            is_subsidiary = name in self.subsidiaries
            if name == self.company_name:
                rank = 0
            elif is_ip_holder:
                rank = 1
            elif ascii_like and name in self.aliases:
                rank = 2
            elif ascii_like and is_subsidiary:
                rank = 3
            elif ascii_like:
                rank = 4
            else:
                rank = 5
            return (rank, len(normalize_name(name)))

        return sorted(names, key=priority)[:limit]


@dataclass
class MatchResult:
    company_id: str = ""
    company_name: str = ""
    applicant: str = ""
    method: str = "unmatched"
    confidence: float = 0.0
    review_required: bool = False


@dataclass
class ScoredPatent:
    patent: PatentRecord
    match: MatchResult
    duplicate_count: int = 1
    family_members: list[str] = field(default_factory=list)
    technology_score: float = 0.0
    technology_reasons: list[str] = field(default_factory=list)
    metadata_score: float = 0.0
    metadata_reasons: list[str] = field(default_factory=list)
    novelty_score: float = 0.0
    novelty_reasons: list[str] = field(default_factory=list)
    materiality_score: float = 0.0
    materiality_reasons: list[str] = field(default_factory=list)
    final_score: float = 0.0
    company_percentile: float = 0.0
    percentile_basis: str = "neutral"
    percentile_sample_count: int = 0
    route: str = "rejected"
    route_reason: str = ""
    gpt_result: dict[str, Any] | None = None
    gemini_result: dict[str, Any] | None = None
    learned_upside_probability: float = 0.0
    learned_model_samples: int = 0
    learned_model_created_at: str = ""
    error: str = ""


@dataclass
class PipelineConfig:
    gemini_threshold: float = 68.0
    company_percentile_threshold: float = 95.0
    fuzzy_match_threshold: float = 0.90
    fuzzy_match_margin: float = 0.05
    random_reject_audit_rate: float = 0.05
    monthly_gpt_limit: int = 500
    daily_gpt_limit: int = 40
    gpt_model: str = "gpt-5-nano"
    monthly_gemini_limit: int = 500
    daily_gemini_limit: int = 10
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_escalation_threshold: float = 85.0
    adaptive_gemini_escalation: bool = True
    gemini_target_escalation_rate: float = 0.10
    adaptive_escalation_min_samples: int = 30
    adaptive_escalation_window: int = 300
    adaptive_escalation_max_step: float = 2.0
    force_two_stage_review: bool = False
    ranked_ai_enabled: bool = False
    ranked_gpt_top_rate: float = 0.20
    ranked_gpt_audit_rate: float = 0.01
    ranked_gemini_top_rate: float = 0.15
    annual_ai_budget_jpy: float = 2000.0
    ai_budget_call_reserve_jpy: float = 5.0
    ai_usd_jpy_rate: float = 160.0
    openai_input_usd_per_million: float = 0.05
    openai_output_usd_per_million: float = 0.40
    gemini_input_usd_per_million: float = 0.25
    gemini_output_usd_per_million: float = 1.50
    metadata_weight: float = 0.22
    technology_weight: float = 0.26
    novelty_weight: float = 0.25
    materiality_weight: float = 0.27
    history_lookback: int = 200
    company_percentile_min_history: int = 5
    global_percentile_min_history: int = 30
    request_timeout_seconds: int = 45
    ops_requests_per_minute: int = 10
    ops_enrich_details: bool = True
    ops_detail_max_records: int = 200
    ops_enrich_claims: bool = True
    ops_claims_fulltext_top_rate: float = 0.02
    ops_enrich_family_legal: bool = True
    ops_enrich_forward_citations: bool = True
    ops_forward_citation_max_records: int = 100
    ops_company_search_name_limit: int = 12
    ops_detail_cache_enabled: bool = True
    ops_detail_cache_path: str = ""
    technology_discovery_enabled: bool = False
    technology_discovery_lookback_days: int = 3
    technology_discovery_max_records_per_query: int = 10
    technology_discovery_email_limit: int = 10
    technology_discovery_gpt_top_rate: float = 0.25
    technology_discovery_queries: list[dict[str, Any]] = field(default_factory=list)
    company_search_time_guard_minutes: float = 90.0
    postprocess_time_guard_minutes: float = 90.0
    finalization_reserve_minutes: float = 20.0
    github_actions_time_guard_minutes: float = 0.0
    target_cpc_prefixes: list[str] = field(default_factory=list)
    target_ipc_prefixes: list[str] = field(default_factory=list)
    target_keywords: list[str] = field(default_factory=list)
    broad_cpc_prefixes: list[str] = field(default_factory=list)
    broad_ipc_prefixes: list[str] = field(default_factory=list)
    broad_keywords: list[str] = field(default_factory=list)
    broad_technology_gemini_threshold: float = 24.0
    broad_materiality_minimum: float = 18.0
    daily_digest_enabled: bool = True
    urgent_notifications_enabled: bool = True
    urgent_max_digest_share: float = 0.01
    urgent_min_importance: float = 98.0
    urgent_min_short_term_market_impact: float = 95.0
    urgent_min_materiality: float = 95.0
    urgent_min_company_percentile: float = 99.0
    urgent_min_learned_upside_probability: float = 0.0
    excluded_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, path: str | Path) -> "PipelineConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {item.name for item in __import__("dataclasses").fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


CSV_ALIASES = {
    "publication_number": ["publication_number", "公開番号", "公報番号", "publication"],
    "application_number": ["application_number", "出願番号", "application"],
    "family_id": ["family_id", "特許ファミリー", "family"],
    "priority_number": ["priority_number", "優先権番号", "priority"],
    "title": ["title", "発明の名称", "名称"],
    "abstract": ["abstract", "要約", "要約文"],
    "claims": ["claims", "請求項"],
    "applicants": ["applicants", "applicant", "出願人", "申請人"],
    "inventors": ["inventors", "inventor", "発明者"],
    "ipc_codes": ["ipc_codes", "ipc", "IPC"],
    "cpc_codes": ["cpc_codes", "cpc", "CPC"],
    "filing_date": ["filing_date", "出願日"],
    "publication_date": ["publication_date", "公開日", "公報発行日"],
    "priority_date": ["priority_date", "優先日"],
    "country_codes": ["country_codes", "countries", "国コード"],
    "citation_count": ["citation_count", "被引用数", "引用数"],
    "family_size": ["family_size", "ファミリー件数"],
    "claim_count": ["claim_count", "請求項数"],
    "legal_status": ["legal_status", "法的状態", "status"],
    "source_url": ["source_url", "url", "URL"],
}


def _column_map(fieldnames: Iterable[str]) -> dict[str, str]:
    normalized = {unicodedata.normalize("NFKC", name).strip().lower(): name for name in fieldnames}
    result: dict[str, str] = {}
    for target, aliases in CSV_ALIASES.items():
        for alias in aliases:
            key = unicodedata.normalize("NFKC", alias).strip().lower()
            if key in normalized:
                result[target] = normalized[key]
                break
    return result


def read_patent_csv(path: str | Path) -> list[PatentRecord]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            with Path(path).open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    return []
                columns = _column_map(reader.fieldnames)
                records: list[PatentRecord] = []
                for row in reader:
                    get = lambda key: row.get(columns.get(key, ""), "")
                    records.append(
                        PatentRecord(
                            source="csv",
                            publication_number=clean_text(get("publication_number")),
                            application_number=clean_text(get("application_number")),
                            family_id=clean_text(get("family_id")),
                            priority_number=clean_text(get("priority_number")),
                            title=clean_text(get("title")),
                            abstract=clean_text(get("abstract")),
                            claims=clean_text(get("claims")),
                            applicants=split_values(get("applicants")),
                            inventors=split_values(get("inventors")),
                            ipc_codes=split_values(get("ipc_codes")),
                            cpc_codes=split_values(get("cpc_codes")),
                            filing_date=clean_text(get("filing_date")),
                            publication_date=clean_text(get("publication_date")),
                            priority_date=clean_text(get("priority_date")),
                            country_codes=split_values(get("country_codes")),
                            citation_count=int(safe_float(get("citation_count"))),
                            family_size=max(1, int(safe_float(get("family_size"), 1))),
                            claim_count=int(safe_float(get("claim_count"))),
                            legal_status=clean_text(get("legal_status")),
                            source_url=clean_text(get("source_url")),
                            raw=row,
                        )
                    )
                return records
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"CSVの文字コードを判定できません: {last_error}")


def read_companies(path: str | Path) -> list[Company]:
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            with Path(path).open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                result = []
                for index, row in enumerate(reader, 1):
                    name = clean_text(row.get("company_name") or row.get("企業名"))
                    if not name:
                        continue
                    target_raw = clean_text(row.get("target") or row.get("対象") or "1").lower()
                    result.append(
                        Company(
                            company_id=clean_text(row.get("company_id") or row.get("企業ID") or f"C{index:05d}"),
                            company_name=name,
                            ticker=clean_text(row.get("ticker") or row.get("証券コード")),
                            aliases=split_values(row.get("aliases") or row.get("別名")),
                            subsidiaries=split_values(row.get("subsidiaries") or row.get("子会社")),
                            market_cap_jpy=safe_float(row.get("market_cap_jpy") or row.get("時価総額円")),
                            target=target_raw not in {"0", "false", "no", "off", "対象外"},
                            technology_tags=split_values(row.get("technology_tags") or row.get("技術タグ")),
                        )
                    )
                return result
        except UnicodeDecodeError:
            continue
    raise ValueError("企業マスタの文字コードを判定できません")


def consolidate_families(records: list[PatentRecord]) -> list[tuple[PatentRecord, list[str]]]:
    groups: dict[str, list[PatentRecord]] = defaultdict(list)
    for record in records:
        groups[record.family_key].append(record)
    consolidated = []
    for members in groups.values():
        representative = max(
            members,
            key=lambda item: len(item.abstract) + len(item.claims) + 30 * len(item.cpc_codes) + 20 * item.citation_count,
        )
        representative.family_size = max(representative.family_size, len(members))
        representative.country_codes = sorted({code for member in members for code in member.country_codes})
        representative.cpc_codes = sorted({code for member in members for code in member.cpc_codes})
        representative.ipc_codes = sorted({code for member in members for code in member.ipc_codes})
        representative.applicants = list(dict.fromkeys(name for member in members for name in member.applicants))
        consolidated.append((representative, [member.identity for member in members]))
    return consolidated


class CompanyMatcher:
    def __init__(self, companies: list[Company], threshold: float, margin: float):
        self.companies = [company for company in companies if company.target]
        self.threshold = threshold
        self.margin = margin
        self.alias_map: dict[str, list[tuple[Company, str]]] = defaultdict(list)
        for company in self.companies:
            for name in company.all_names:
                normalized = normalize_name(name)
                if normalized:
                    relation = "subsidiary" if name in company.subsidiaries else "exact"
                    self.alias_map[normalized].append((company, relation))

    def match(self, applicants: list[str]) -> MatchResult:
        for applicant in applicants:
            normalized = normalize_name(applicant)
            exact = self.alias_map.get(normalized, [])
            exact_companies = {company.company_id: (company, method) for company, method in exact}
            if len(exact_companies) == 1:
                company, method = next(iter(exact_companies.values()))
                return MatchResult(company.company_id, company.company_name, applicant, method, 1.0, False)
            if len(exact_companies) > 1:
                return MatchResult(applicant=applicant, method="ambiguous_exact", confidence=1.0, review_required=True)

        candidates: list[tuple[float, Company, str]] = []
        for applicant in applicants:
            normalized = normalize_name(applicant)
            if not normalized:
                continue
            for alias, entries in self.alias_map.items():
                ratio = SequenceMatcher(None, normalized, alias).ratio()
                if ratio >= self.threshold:
                    for company, _ in entries:
                        candidates.append((ratio, company, applicant))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return MatchResult(applicant=applicants[0] if applicants else "")
        best = candidates[0]
        second = next((item for item in candidates[1:] if item[1].company_id != best[1].company_id), None)
        uncertain = second is not None and best[0] - second[0] < self.margin
        return MatchResult(
            company_id="" if uncertain else best[1].company_id,
            company_name="" if uncertain else best[1].company_name,
            applicant=best[2],
            method="ambiguous_fuzzy" if uncertain else "fuzzy",
            confidence=best[0],
            review_required=uncertain,
        )


def technology_score(record: PatentRecord, company: Company | None, config: PipelineConfig) -> tuple[float, list[str]]:
    codes = [code.upper().replace(" ", "") for code in [*record.cpc_codes, *record.ipc_codes]]
    text = record.searchable_text.lower()
    compact_text = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]", "", unicodedata.normalize("NFKC", text))
    reasons: list[str] = []
    score = 0.0
    matched_cpc = sorted({prefix for prefix in config.target_cpc_prefixes if any(code.startswith(prefix.upper()) for code in codes)})
    matched_ipc = sorted({prefix for prefix in config.target_ipc_prefixes if any(code.startswith(prefix.upper()) for code in codes)})
    matched_broad_cpc = sorted({
        prefix for prefix in config.broad_cpc_prefixes
        if prefix not in matched_cpc and any(code.startswith(prefix.upper()) for code in codes)
    })
    matched_broad_ipc = sorted({
        prefix for prefix in config.broad_ipc_prefixes
        if prefix not in matched_ipc and any(code.startswith(prefix.upper()) for code in codes)
    })

    def keyword_matches(words: Iterable[str]) -> list[str]:
        found = []
        for word in words:
            normalized = unicodedata.normalize("NFKC", clean_text(word)).lower()
            compact = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]", "", normalized)
            if normalized and (normalized in text or (compact and compact in compact_text)):
                found.append(word)
        return found

    matched_keywords = keyword_matches(config.target_keywords)
    matched_broad_keywords = [word for word in keyword_matches(config.broad_keywords) if word not in matched_keywords]
    company_keywords = keyword_matches(company.technology_tags if company else [])
    excluded = [word for word in config.excluded_keywords if word.lower() in text]
    if matched_cpc:
        score += min(55, 35 + 7 * len(matched_cpc))
        reasons.append("対象CPC=" + ",".join(matched_cpc))
    if matched_ipc:
        score += min(20, 8 + 4 * len(matched_ipc))
        reasons.append("対象IPC=" + ",".join(matched_ipc))
    broad_codes = sorted(set(matched_broad_cpc + matched_broad_ipc))
    if broad_codes:
        score += min(30, 12 + 4 * len(broad_codes))
        reasons.append("broad_classification=" + ",".join(broad_codes[:12]))
    if matched_keywords:
        score += min(30, 6 * len(matched_keywords))
        reasons.append("技術語=" + ",".join(matched_keywords[:8]))
    if matched_broad_keywords:
        score += min(24, 3 * len(matched_broad_keywords))
        reasons.append("broad_technology=" + ",".join(matched_broad_keywords[:12]))
    if company_keywords:
        score += min(20, 5 * len(company_keywords))
        reasons.append("企業技術語=" + ",".join(company_keywords[:6]))
    if excluded:
        score -= min(50, 15 * len(excluded))
        reasons.append("除外語=" + ",".join(excluded[:6]))
    if not reasons:
        reasons.append("対象分類・技術語なし")
    return clamp(score), reasons


def metadata_score(record: PatentRecord) -> tuple[float, list[str]]:
    reasons = []
    family = min(25.0, 7.0 * math.log2(max(1, record.family_size)))
    countries = min(15.0, 4.0 * len(set(record.country_codes)))
    citations = min(25.0, 6.0 * math.log2(1 + max(0, record.citation_count)))
    claims = min(15.0, 0.75 * max(0, record.claim_count))
    status_text = record.legal_status.lower()
    status = 12.0 if any(word in status_text for word in ("grant", "registered", "登録", "特許")) else 5.0 if status_text else 0.0
    detail = 8.0 if record.abstract and record.claims else 4.0 if record.abstract else 0.0
    if family:
        reasons.append(f"family={record.family_size}")
    if countries:
        reasons.append(f"countries={len(set(record.country_codes))}")
    if citations:
        reasons.append(f"citations={record.citation_count}")
    if claims:
        reasons.append(f"claims={record.claim_count}")
    if status:
        reasons.append(f"status={record.legal_status}")
    return clamp(family + countries + citations + claims + status + detail), reasons or ["メタデータ不足"]


class PatentDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS patents (
                patent_id TEXT PRIMARY KEY, family_key TEXT, company_id TEXT, company_name TEXT,
                publication_date TEXT, title TEXT, text_body TEXT, cpc_json TEXT, raw_json TEXT,
                first_seen_at TEXT, last_seen_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_patents_company ON patents(company_id, publication_date);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT, app_version TEXT,
                source TEXT, input_count INTEGER, family_count INTEGER, gpt_count INTEGER, gemini_count INTEGER,
                error_count INTEGER, config_json TEXT
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                run_id TEXT, patent_id TEXT, company_id TEXT, match_method TEXT, match_confidence REAL,
                review_required INTEGER, technology_score REAL, metadata_score REAL, novelty_score REAL,
                materiality_score REAL, final_score REAL, company_percentile REAL, route TEXT,
                route_reason TEXT, reasons_json TEXT, gpt_json TEXT, gemini_json TEXT, error TEXT,
                prompt_version TEXT, model TEXT, gpt_model TEXT, gemini_model TEXT, created_at TEXT,
                PRIMARY KEY(run_id, patent_id)
            );
            CREATE TABLE IF NOT EXISTS api_usage (
                service TEXT, period TEXT, calls INTEGER, input_chars INTEGER, output_chars INTEGER,
                PRIMARY KEY(service, period)
            );
            CREATE TABLE IF NOT EXISTS adaptive_settings (
                setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                usage_year TEXT NOT NULL,
                service TEXT NOT NULL,
                model TEXT NOT NULL,
                run_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                cost_jpy REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_cost_year ON ai_cost_ledger(usage_year, service);
            CREATE TABLE IF NOT EXISTS ai_budget_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                usage_year TEXT NOT NULL,
                run_id TEXT NOT NULL,
                service TEXT NOT NULL,
                event TEXT NOT NULL,
                annual_spend_jpy REAL NOT NULL,
                annual_budget_jpy REAL NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_review_cache (
                service TEXT NOT NULL,
                patent_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                PRIMARY KEY(service, patent_id, model, prompt_version, input_hash)
            );
            """
        )
        self._ensure_column("runs", "gpt_count", "INTEGER")
        self._ensure_column("evaluations", "gpt_json", "TEXT")
        self._ensure_column("evaluations", "gpt_model", "TEXT")
        self._ensure_column("evaluations", "gemini_model", "TEXT")
        self._ensure_column("patents", "record_json", "TEXT")
        self._ensure_column("patents", "retrieved_at", "TEXT")
        self._ensure_column("patents", "source_endpoint", "TEXT")
        self._ensure_column("patents", "payload_hash", "TEXT")
        self._ensure_column("patents", "parser_version", "TEXT")
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.connection.close()

    def historical_patents(self, company_id: str, limit: int, exclude_patent_id: str = "") -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT title, text_body, cpc_json FROM patents
               WHERE company_id=? AND patent_id<>? ORDER BY publication_date DESC LIMIT ?""",
            (company_id, exclude_patent_id, limit),
        ).fetchall()

    def prior_scores(self, company_id: str, exclude_patent_id: str = "", limit: int = 500) -> list[float]:
        if not company_id:
            return []
        rows = self.connection.execute(
            """SELECT final_score FROM evaluations
               WHERE company_id=? AND patent_id<>? AND final_score IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (company_id, exclude_patent_id, limit),
        ).fetchall()
        return [float(row[0]) for row in rows]

    def prior_scores_global(self, limit: int = 2000) -> list[float]:
        rows = self.connection.execute(
            """SELECT final_score FROM evaluations
               WHERE company_id<>'' AND final_score IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [float(row[0]) for row in rows]

    def usage(self, service: str, period: str) -> int:
        row = self.connection.execute(
            "SELECT calls FROM api_usage WHERE service=? AND period=?", (service, period)
        ).fetchone()
        return int(row[0]) if row else 0

    def gemini_usage(self, period: str) -> int:
        return self.usage("gemini", period)

    def add_usage(self, service: str, input_chars: int, output_chars: int) -> None:
        period = datetime.now().strftime("%Y-%m")
        self.connection.execute(
            """INSERT INTO api_usage(service,period,calls,input_chars,output_chars) VALUES(?,?,?,?,?)
               ON CONFLICT(service,period) DO UPDATE SET calls=calls+1,
               input_chars=input_chars+excluded.input_chars, output_chars=output_chars+excluded.output_chars""",
            (service, period, 1, input_chars, output_chars),
        )
        self.connection.commit()

    def annual_ai_spend_jpy(self, usage_year: str) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(cost_jpy), 0) FROM ai_cost_ledger WHERE usage_year = ?",
            (usage_year,),
        ).fetchone()
        return float(row[0] or 0)

    def ai_calls_on_date(self, service: str, usage_date: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) FROM ai_cost_ledger
               WHERE service=? AND substr(created_at,1,10)=?""",
            (service, usage_date),
        ).fetchone()
        return int(row[0] or 0)

    def add_ai_cost(
        self,
        usage_year: str,
        service: str,
        model: str,
        run_id: str,
        input_tokens: int,
        output_tokens: int,
        input_usd_per_million: float,
        output_usd_per_million: float,
        usd_jpy_rate: float,
    ) -> float:
        cost_usd = (
            max(0, input_tokens) * input_usd_per_million
            + max(0, output_tokens) * output_usd_per_million
        ) / 1_000_000
        cost_jpy = cost_usd * usd_jpy_rate
        self.connection.execute(
            """
            INSERT INTO ai_cost_ledger (
                created_at, usage_year, service, model, run_id,
                input_tokens, output_tokens, cost_usd, cost_jpy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                usage_year,
                service,
                model,
                run_id,
                int(input_tokens),
                int(output_tokens),
                cost_usd,
                cost_jpy,
            ),
        )
        self.connection.commit()
        return cost_jpy

    def record_ai_budget_event(
        self,
        usage_year: str,
        run_id: str,
        service: str,
        annual_spend_jpy: float,
        annual_budget_jpy: float,
        detail: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO ai_budget_events (
                created_at, usage_year, run_id, service, event,
                annual_spend_jpy, annual_budget_jpy, detail
            ) VALUES (?, ?, ?, ?, 'budget_stop', ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                usage_year,
                run_id,
                service,
                annual_spend_jpy,
                annual_budget_jpy,
                detail,
            ),
        )
        self.connection.commit()

    def adaptive_value(self, key: str) -> str:
        row = self.connection.execute(
            "SELECT setting_value FROM adaptive_settings WHERE setting_key=?", (key,)
        ).fetchone()
        return str(row[0]) if row else ""

    def set_adaptive_value(self, key: str, value: Any) -> None:
        self.connection.execute(
            """INSERT INTO adaptive_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
               updated_at=excluded.updated_at""",
            (key, str(value), datetime.now().isoformat(timespec="seconds")),
        )
        self.connection.commit()

    def recent_gpt_peak_scores(self, limit: int) -> list[float]:
        rows = self.connection.execute(
            """SELECT gpt_json FROM evaluations WHERE gpt_json<>''
               ORDER BY created_at DESC LIMIT ?""", (limit,)
        ).fetchall()
        result = []
        for row in reversed(rows):
            try:
                data = json.loads(row[0])
                result.append(max(float(data.get(key, 0) or 0) for key in (
                    "importance_score", "materiality_score", "short_term_market_impact_score",
                    "long_term_business_value_score", "novelty_score",
                )))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def cached_ai_review(
        self, service: str, patent_id: str, model: str, input_hash: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT result_json FROM ai_review_cache
               WHERE service=? AND patent_id=? AND model=? AND prompt_version=? AND input_hash=?""",
            (service, patent_id, model, PROMPT_VERSION, input_hash),
        ).fetchone()
        if not row:
            return None
        try:
            result = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        self.connection.execute(
            """UPDATE ai_review_cache SET last_used_at=?
               WHERE service=? AND patent_id=? AND model=? AND prompt_version=? AND input_hash=?""",
            (
                datetime.now().isoformat(timespec="seconds"), service, patent_id,
                model, PROMPT_VERSION, input_hash,
            ),
        )
        self.connection.commit()
        return result if isinstance(result, dict) else None

    def store_ai_review(
        self, service: str, patent_id: str, model: str, input_hash: str,
        result: dict[str, Any],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.connection.execute(
            """INSERT OR REPLACE INTO ai_review_cache(
               service,patent_id,model,prompt_version,input_hash,result_json,created_at,last_used_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                service, patent_id, model, PROMPT_VERSION, input_hash,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")), now, now,
            ),
        )
        self.connection.commit()

    def save_run(self, run_id: str, source: str, config: PipelineConfig, started_at: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO runs(run_id,started_at,app_version,source,config_json) VALUES(?,?,?,?,?)",
            (run_id, started_at, APP_VERSION, source, json.dumps(asdict(config), ensure_ascii=False)),
        )
        self.connection.commit()

    def finish_run(
        self, run_id: str, input_count: int, family_count: int,
        gpt_count: int, gemini_count: int, error_count: int,
    ) -> None:
        self.connection.execute(
            "UPDATE runs SET completed_at=?,input_count=?,family_count=?,gpt_count=?,gemini_count=?,error_count=? WHERE run_id=?",
            (datetime.now().isoformat(timespec="seconds"), input_count, family_count, gpt_count, gemini_count, error_count, run_id),
        )
        self.connection.commit()

    def save_scored(self, run_id: str, item: ScoredPatent, gpt_model: str, gemini_model: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        patent = item.patent
        reasons = {
            "technology": item.technology_reasons,
            "metadata": item.metadata_reasons,
            "novelty": item.novelty_reasons,
            "materiality": item.materiality_reasons,
            "family_members": item.family_members,
        }
        self.connection.execute(
            """INSERT INTO patents(patent_id,family_key,company_id,company_name,publication_date,title,text_body,
               cpc_json,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(patent_id) DO UPDATE SET company_id=excluded.company_id,company_name=excluded.company_name,
               publication_date=excluded.publication_date,title=excluded.title,text_body=excluded.text_body,
               cpc_json=excluded.cpc_json,raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at""",
            (
                patent.identity, patent.family_key, item.match.company_id, item.match.company_name,
                patent.publication_date, patent.title, patent.searchable_text,
                json.dumps([*patent.cpc_codes, *patent.ipc_codes], ensure_ascii=False),
                json.dumps(patent.raw, ensure_ascii=False), now, now,
            ),
        )
        self.connection.execute(
            """UPDATE patents SET record_json=?,retrieved_at=?,source_endpoint=?,payload_hash=?,parser_version=?
               WHERE patent_id=?""",
            (
                json.dumps(asdict(patent), ensure_ascii=False, separators=(",", ":"), default=str),
                patent.retrieved_at,
                patent.source_endpoint,
                patent.payload_hash,
                patent.parser_version,
                patent.identity,
            ),
        )
        self.connection.execute(
            """INSERT OR REPLACE INTO evaluations(
               run_id,patent_id,company_id,match_method,match_confidence,review_required,
               technology_score,metadata_score,novelty_score,materiality_score,final_score,
               company_percentile,route,route_reason,reasons_json,gpt_json,gemini_json,error,
               prompt_version,model,gpt_model,gemini_model,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, patent.identity, item.match.company_id, item.match.method, item.match.confidence,
                int(item.match.review_required), item.technology_score, item.metadata_score, item.novelty_score,
                item.materiality_score, item.final_score, item.company_percentile, item.route, item.route_reason,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(item.gpt_result, ensure_ascii=False) if item.gpt_result else "",
                json.dumps(item.gemini_result, ensure_ascii=False) if item.gemini_result else "", item.error,
                PROMPT_VERSION, gemini_model or gpt_model, gpt_model, gemini_model, now,
            ),
        )
        self.connection.commit()


def novelty_score(record: PatentRecord, match: MatchResult, database: PatentDatabase, lookback: int) -> tuple[float, list[str]]:
    if not match.company_id:
        return 25.0, ["企業未一致のため履歴比較不能"]
    history = database.historical_patents(match.company_id, lookback, record.identity)
    if not history:
        return 50.0, ["当該企業の初回観測（新規性は中立値）"]
    current_vector = character_ngrams(record.title + " " + record.abstract)
    similarities = [cosine_similarity(current_vector, character_ngrams(row["title"] + " " + row["text_body"])) for row in history]
    max_similarity = max(similarities, default=0.0)
    historical_codes = {code for row in history for code in json.loads(row["cpc_json"] or "[]")}
    current_roots = {
        re.sub(r"[^A-Z0-9]", "", code.upper())[:4]
        for code in [*record.cpc_codes, *record.ipc_codes] if code
    }
    historical_roots = {re.sub(r"[^A-Z0-9]", "", code.upper())[:4] for code in historical_codes if code}
    new_roots = sorted(current_roots - historical_roots)
    text_novelty = (1.0 - max_similarity) * 75.0
    code_bonus = min(25.0, 10.0 * len(new_roots))
    reasons = [f"過去類似度最大={max_similarity:.3f}"]
    if new_roots:
        reasons.append("new_classification=" + ",".join(new_roots[:8]))
        reasons.append("企業初CPC=" + ",".join(new_roots[:8]))
    return clamp(text_novelty + code_bonus), reasons


def materiality_score(record: PatentRecord, company: Company | None, metadata: float, novelty: float) -> tuple[float, list[str]]:
    if not company:
        return 0.0, ["上場企業未一致"]
    market_cap = company.market_cap_jpy
    if market_cap <= 0:
        size_factor, size_label = 1.0, "時価総額不明（中立）"
    elif market_cap < 100_000_000_000:
        size_factor, size_label = 1.25, "1000億円未満"
    elif market_cap < 1_000_000_000_000:
        size_factor, size_label = 1.0, "1000億-1兆円"
    elif market_cap < 5_000_000_000_000:
        size_factor, size_label = 0.75, "1-5兆円"
    else:
        size_factor, size_label = 0.55, "5兆円以上"
    applicant_share = 1.0 / max(1, len(record.applicants))
    base = 0.55 * metadata + 0.45 * novelty
    score = base * size_factor * (0.65 + 0.35 * applicant_share)
    return clamp(score), [f"企業規模={size_label}", f"共同出願人={len(record.applicants)}", f"規模係数={size_factor:.2f}"]


def percentile(value: float, history: list[float]) -> float:
    if not history:
        return 50.0
    return 100.0 * sum(item <= value for item in history) / len(history)


def stable_percentile(
    value: float,
    company_history: list[float],
    global_history: list[float],
    company_minimum: int,
    global_minimum: int,
) -> tuple[float, str, int]:
    """Avoid a one-observation history becoming an automatic 100th percentile."""
    if len(company_history) >= max(1, company_minimum):
        return percentile(value, company_history), "company_history", len(company_history)
    if len(global_history) >= max(1, global_minimum):
        return percentile(value, global_history), "global_history", len(global_history)
    return 50.0, "neutral_insufficient_history", max(len(company_history), len(global_history))


class GeminiReviewer:
    RETIRED_MODEL_PREFIXES = ("gemini-2.0-",)
    RETIRED_MODEL_IDS = {"gemini-2.5-flash-lite"}
    SCHEMA = {
        "type": "object",
        "properties": {
            "importance_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "materiality_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "short_term_market_impact_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "long_term_business_value_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "novelty_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "decision": {"type": "string", "enum": ["reject", "watch", "important", "urgent"]},
            "technology_summary": {"type": "string"},
            "business_impact": {"type": "string"},
            "market_impact_reason": {"type": "string"},
            "expected_time_horizon": {"type": "string"},
            "key_evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "risks_and_uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "email_summary": {"type": "string"},
        },
        "required": [
            "importance_score", "materiality_score", "short_term_market_impact_score",
            "long_term_business_value_score", "novelty_score", "decision", "technology_summary",
            "business_impact", "market_impact_reason", "expected_time_horizon", "key_evidence",
            "risks_and_uncertainty", "email_summary",
        ],
    }

    def __init__(self, api_key: str, model: str, timeout: int = 45):
        self.api_key = api_key
        self.model = clean_text(model) or "auto"
        self.resolved_model = ""
        self.unavailable_models: set[str] = set()
        self.timeout = timeout

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        body = clean_text(body)[:1200]
        detail = f"HTTP {exc.code} {exc.reason}"
        if body:
            detail += f": {body}"
        return RuntimeError(detail)

    def list_models(self) -> list[dict[str, Any]]:
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        request = urllib.request.Request(endpoint, headers={"x-goog-api-key": self.api_key})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        return [
            model for model in body.get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        ]

    @staticmethod
    def _model_id(model: dict[str, Any]) -> str:
        return clean_text(model.get("baseModelId")) or clean_text(model.get("name")).removeprefix("models/")

    @staticmethod
    def _fallback_rank(model_id: str) -> tuple[int, int, str]:
        name = model_id.lower()
        excluded = any(word in name for word in ("image", "tts", "audio", "live", "embedding", "computer-use"))
        excluded = excluded or name.startswith(GeminiReviewer.RETIRED_MODEL_PREFIXES)
        excluded = excluded or name in GeminiReviewer.RETIRED_MODEL_IDS
        if excluded or "gemini" not in name:
            return (9, 9, name)
        if name == "gemini-3.1-flash-lite":
            tier = 0
        else:
            tier = 1 if "flash-lite" in name else 2 if "flash" in name else 5
        preview = 1 if any(word in name for word in ("preview", "experimental", "exp")) else 0
        return (tier, preview, name)

    def resolve_model(self, refresh: bool = False) -> str:
        if self.resolved_model and not refresh:
            return self.resolved_model
        available = {
            self._model_id(item): item
            for item in self.list_models()
            if self._model_id(item) and self._model_id(item) not in self.unavailable_models
        }
        requested = self.model.removeprefix("models/")
        requested_is_retired = (
            requested.lower().startswith(self.RETIRED_MODEL_PREFIXES)
            or requested.lower() in self.RETIRED_MODEL_IDS
        )
        if requested.lower() != "auto" and requested in available and not requested_is_retired:
            self.resolved_model = requested
            return requested
        candidates = sorted(available, key=self._fallback_rank)
        candidates = [name for name in candidates if self._fallback_rank(name)[0] < 9]
        if not candidates:
            raise RuntimeError("このAPIキーでgenerateContentを利用できるGeminiモデルが見つかりません")
        self.resolved_model = candidates[0]
        return self.resolved_model

    def probe(self) -> dict[str, Any]:
        models = self.list_models()
        resolved = self.resolve_model(refresh=True)
        return {
            "requested_model": self.model,
            "resolved_model": resolved,
            "generate_content_model_count": len(models),
        }

    @staticmethod
    def _build_prompt_v1(item: ScoredPatent, company: Company | None) -> str:
        patent = item.patent
        return (
            "あなたは上場企業の特許材料性を保守的に一次審査するアナリストです。"
            "特許の技術的新規性と、企業価値に影響し得る材料性を分けて評価してください。"
            "推測を事実として書かず、共同出願・企業規模・製品化までの不確実性を考慮してください。\n\n"
            f"企業: {(company.company_name if company else item.match.company_name) or '不明'}\n"
            f"時価総額円: {company.market_cap_jpy if company else 0:.0f}\n"
            f"公開番号: {patent.publication_number}\n公開日: {patent.publication_date}\n"
            f"発明名称: {patent.title}\n出願人: {' / '.join(patent.applicants)}\n"
            f"CPC/IPC: {' / '.join([*patent.cpc_codes, *patent.ipc_codes])}\n"
            f"要約: {patent.abstract[:7000]}\n請求項抜粋: {patent.claims[:5000]}\n"
            f"事前評価: 技術={item.technology_score:.1f}, メタ={item.metadata_score:.1f}, "
            f"新規性={item.novelty_score:.1f}, 材料性={item.materiality_score:.1f}, 総合={item.final_score:.1f}\n"
            f"事前根拠: {json.dumps({'technology': item.technology_reasons, 'novelty': item.novelty_reasons, 'materiality': item.materiality_reasons}, ensure_ascii=False)}"
        )

    @staticmethod
    def build_prompt(item: ScoredPatent, company: Company | None) -> str:
        """Build an independent second-stage review prompt without local-score anchoring."""
        patent = item.patent
        company_name = (company.company_name if company else item.match.company_name) or "不明"
        market_cap = company.market_cap_jpy if company else 0
        return (
            "あなたは上場企業の新規特許を評価する独立したアナリストです。"
            "前段の機械判定は候補を絞るためだけに使われており、誤っている可能性があります。"
            "前段の点数を推測したり追随したりせず、以下の一次情報だけから再評価してください。\n\n"
            "重要: 短期の株価材料性と、長期の事業価値を分けて評価してください。"
            "規制承認や量産まで時間がかかっても、発表時の意外性・注目度・市場規模が大きければ"
            "短期材料性は高くなり得ます。一方で、実施可能性、権利範囲、競争優位、収益化までの距離を"
            "長期事業価値に反映してください。推測を事実として書かず、不明点はリスクに記載してください。\n\n"
            f"企業: {company_name}\n"
            f"時価総額(円、不明は0): {market_cap:.0f}\n"
            f"公開番号: {patent.publication_number}\n"
            f"公開日: {patent.publication_date}\n"
            f"発明名称: {patent.title}\n"
            f"出願人: {' / '.join(patent.applicants)}\n"
            f"CPC/IPC: {' / '.join([*patent.cpc_codes, *patent.ipc_codes])}\n"
            f"要約: {patent.abstract[:7000]}\n"
            f"請求項抜粋: {patent.claims[:5000]}\n\n"
            "採点定義:\n"
            "- short_term_market_impact_score: 公開直後から数週間の株価材料性。意外性、注目度、企業規模比を重視。\n"
            "- long_term_business_value_score: 数年単位の事業価値。実施可能性、競争優位、収益寄与を重視。\n"
            "- materiality_score: long_term_business_value_scoreと同じ値を入れ、旧形式との互換性を保つ。\n"
            "- importance_score: 短期材料性と長期価値を総合した監視優先度。\n"
            "- decision: reject/watch/important/urgent。urgentは即時通知に値する場合だけ。"
        )

    def review(self, item: ScoredPatent, company: Company | None) -> tuple[dict[str, Any], int, int]:
        prompt = self.build_prompt(item, company)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
                "responseSchema": self.SCHEMA,
            },
        }
        body: dict[str, Any] | None = None
        last_error: RuntimeError | None = None
        for attempt in range(2):
            model = self.resolve_model(refresh=attempt > 0)
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error = self._http_error(exc)
                last_error = error
                can_fallback = exc.code == 404 or (exc.code == 429 and "limit: 0" in str(error))
                if attempt == 0 and can_fallback:
                    self.unavailable_models.add(model)
                    self.resolved_model = ""
                    self.model = "auto"
                    continue
                raise error from exc
        if body is None:
            raise last_error or RuntimeError("Geminiから応答を取得できませんでした")
        output = body["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(output)
        result["materiality_score"] = result.get(
            "long_term_business_value_score", result.get("materiality_score", 0)
        )
        if result.get("decision") not in {"reject", "watch", "important", "urgent"}:
            raise ValueError("Geminiのdecisionが不正です")
        usage = body.get("usageMetadata", {})
        input_tokens = int(usage.get("promptTokenCount") or max(1, len(prompt)))
        total_tokens = int(usage.get("totalTokenCount") or 0)
        output_tokens = int(
            usage.get("candidatesTokenCount")
            or (max(0, total_tokens - input_tokens) if total_tokens else max(1, len(output)))
        )
        return result, input_tokens, output_tokens


class OpenAIReviewer:
    """Low-cost first-stage patent reviewer using an OpenAI structured response."""

    def __init__(self, api_key: str, model: str = "gpt-5-nano", timeout: int = 45):
        self.api_key = clean_text(api_key)
        self.model = clean_text(model) or "gpt-5-nano"
        self.timeout = timeout

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> RuntimeError:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        try:
            error = json.loads(body).get("error", {})
        except Exception:
            error = {}
        code = clean_text(error.get("code"))
        if exc.code == 429 and code == "insufficient_quota":
            return RuntimeError(
                "OpenAI APIの利用枠がありません。ChatGPTの契約とは別に、"
                "OpenAI PlatformでAPI課金またはクレジットを設定してください"
            )
        if exc.code == 401:
            return RuntimeError("OpenAI APIキーが無効です。キーと対象プロジェクトを確認してください")
        detail = f"HTTP {exc.code} {exc.reason}"
        if body:
            detail += f": {clean_text(body)[:1200]}"
        return RuntimeError(detail)

    @staticmethod
    def _strict_schema() -> dict[str, Any]:
        schema = json.loads(json.dumps(GeminiReviewer.SCHEMA))
        schema["additionalProperties"] = False
        return schema

    def probe(self) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "接続確認です。JSONでokをtrueにしてください。"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "connection_test",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
            },
            "reasoning_effort": "minimal",
            "max_completion_tokens": 512,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        output = body["choices"][0]["message"].get("content", "")
        if not output or not json.loads(output).get("ok"):
            choice = body.get("choices", [{}])[0]
            usage = body.get("usage", {})
            raise RuntimeError(
                "OpenAIから接続確認本文を取得できませんでした。"
                f"終了理由={choice.get('finish_reason', '不明')} / "
                f"出力token={usage.get('completion_tokens', '不明')}"
            )
        return {"requested_model": self.model, "resolved_model": body.get("model", self.model)}

    def review(self, item: ScoredPatent, company: Company | None) -> tuple[dict[str, Any], int, int]:
        prompt = GeminiReviewer.build_prompt(item, company)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "あなたは上場企業の特許を一次審査するアナリストです。"
                        "推測を事実として書かず、与えられた情報だけで保守的に評価してください。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "patent_materiality_review",
                    "strict": True,
                    "schema": self._strict_schema(),
                },
            },
            "reasoning_effort": "minimal",
            "max_completion_tokens": 3000,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        output = body["choices"][0]["message"].get("content", "")
        if not output:
            choice = body.get("choices", [{}])[0]
            usage = body.get("usage", {})
            raise RuntimeError(
                "GPT-5 nanoの評価本文が空です。"
                f"終了理由={choice.get('finish_reason', '不明')} / "
                f"出力token={usage.get('completion_tokens', '不明')}"
            )
        result = json.loads(output)
        result["materiality_score"] = result.get(
            "long_term_business_value_score", result.get("materiality_score", 0)
        )
        if result.get("decision") not in {"reject", "watch", "important", "urgent"}:
            raise ValueError("GPT-5 nanoのdecisionが不正です")
        usage = body.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt)))
        output_tokens = int(usage.get("completion_tokens") or max(1, len(output)))
        return result, input_tokens, output_tokens


class EPOOPSTokenExpired(RuntimeError):
    pass


class EPOOPSProvider:
    """Small EPO OPS search adapter. Credentials are never persisted by this class."""

    BASE = "https://ops.epo.org/3.2"

    def __init__(self, consumer_key: str, consumer_secret: str, requests_per_minute: int = 10, timeout: int = 45):
        self.key = consumer_key
        self.secret = consumer_secret
        self.requests_per_minute = max(1, requests_per_minute)
        self.interval = 60.0 / self.requests_per_minute
        self.timeout = timeout
        self.last_request = 0.0
        self.last_company_search_stats: dict[str, Any] = {}
        self.detail_cache_path: Path | None = None
        self.throttle_pause_seconds = 60.0
        self.raw_payload_callback: Callable[[dict[str, Any], bytes], None] | None = None
        self.request_context: dict[str, Any] = {}
        self.last_response_metadata: dict[str, Any] = {}

    def set_raw_payload_callback(
        self,
        callback: Callable[[dict[str, Any], bytes], None] | None,
    ) -> None:
        self.raw_payload_callback = callback

    def _capture_payload(self, content: bytes, source_endpoint: str, **extra: Any) -> dict[str, Any]:
        metadata = {
            **self.request_context,
            **extra,
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_endpoint": source_endpoint,
            "payload_hash": hashlib.sha256(content).hexdigest(),
            "parser_version": PARSER_VERSION,
            "response_size": len(content),
        }
        self.last_response_metadata = metadata
        if self.raw_payload_callback:
            self.raw_payload_callback(metadata, content)
        return metadata

    def reduce_rate_after_throttle(self, target_requests_per_minute: int = 8) -> int:
        self.requests_per_minute = max(1, min(self.requests_per_minute - 1, target_requests_per_minute))
        self.interval = 60.0 / self.requests_per_minute
        return self.requests_per_minute

    def set_detail_cache(self, path: str | Path | None) -> None:
        self.detail_cache_path = Path(path) if path else None
        if self.detail_cache_path:
            self.detail_cache_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.detail_cache_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS epo_detail_cache (
                        publication_number TEXT NOT NULL,
                        constituent TEXT NOT NULL,
                        content BLOB NOT NULL,
                        created_at TEXT NOT NULL,
                        compression TEXT NOT NULL DEFAULT 'gzip',
                        response_size INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (publication_number, constituent)
                    )
                    """
                )
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(epo_detail_cache)")
                }
                if "compression" not in columns:
                    connection.execute(
                        "ALTER TABLE epo_detail_cache ADD COLUMN compression TEXT NOT NULL DEFAULT ''"
                    )
                if "response_size" not in columns:
                    connection.execute(
                        "ALTER TABLE epo_detail_cache ADD COLUMN response_size INTEGER NOT NULL DEFAULT 0"
                    )
                legacy_rows = connection.execute(
                    "SELECT publication_number,constituent,content FROM epo_detail_cache "
                    "WHERE compression = ''"
                ).fetchall()
                for publication_number, constituent, stored_content in legacy_rows:
                    raw_content = bytes(stored_content)
                    connection.execute(
                        """
                        UPDATE epo_detail_cache
                        SET content = ?, compression = 'gzip', response_size = ?
                        WHERE publication_number = ? AND constituent = ?
                        """,
                        (
                            gzip.compress(raw_content, compresslevel=6),
                            len(raw_content),
                            publication_number,
                            constituent,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

    def _cache_key(self, publication_number: str) -> str:
        return re.sub(r"[^0-9A-Z]", "", publication_number.upper())

    def _cached_published_data(self, publication_number: str, constituent: str) -> bytes | None:
        if not self.detail_cache_path:
            return None
        connection = sqlite3.connect(self.detail_cache_path)
        try:
            row = connection.execute(
                "SELECT content,created_at,compression,response_size FROM epo_detail_cache "
                "WHERE publication_number = ? AND constituent = ?",
                (self._cache_key(publication_number), constituent),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        stored_content = bytes(row[0])
        try:
            content = gzip.decompress(stored_content) if row[2] == "gzip" else stored_content
        except (OSError, EOFError):
            return None
        if int(row[3] or 0) not in {0, len(content)}:
            return None
        self.last_response_metadata = {
            **self.request_context,
            "retrieved_at": clean_text(row[1]),
            "source_endpoint": f"cache/{constituent}",
            "publication_number": publication_number,
            "constituent": constituent,
            "payload_hash": hashlib.sha256(content).hexdigest(),
            "parser_version": PARSER_VERSION,
            "response_size": len(content),
            "cache_hit": True,
        }
        return content

    def _store_published_data_cache(self, publication_number: str, constituent: str, content: bytes) -> None:
        if not self.detail_cache_path:
            return
        compressed = gzip.compress(content, compresslevel=6)
        connection = sqlite3.connect(self.detail_cache_path)
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO epo_detail_cache
                    (publication_number, constituent, content, created_at, compression, response_size)
                VALUES (?, ?, ?, ?, 'gzip', ?)
                """,
                (
                    self._cache_key(publication_number),
                    constituent,
                    compressed,
                    datetime.now().isoformat(timespec="seconds"),
                    len(content),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _wait(self) -> None:
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _http_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _empty_search_response() -> bytes:
        return b'<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents /></world-patent-data>'

    @staticmethod
    def _is_expired_token(exc: urllib.error.HTTPError, body: str) -> bool:
        return exc.code in {400, 401} and "invalid_access_token" in body and "expired" in body.lower()

    @staticmethod
    def _http_error(stage: str, exc: urllib.error.HTTPError, body: str | None = None) -> RuntimeError:
        if body is None:
            body = EPOOPSProvider._http_error_body(exc)
        body = clean_text(body)[:1600]
        diagnostics = []
        for name in ("Retry-After", "X-Throttling-Control", "X-IndividualQuotaPerHour-Used", "X-RegisteredQuotaPerWeek-Used"):
            value = exc.headers.get(name) if exc.headers else None
            if value:
                diagnostics.append(f"{name}={value}")
        detail = f"EPO OPS {stage}: HTTP {exc.code} {exc.reason}"
        if diagnostics:
            detail += " / " + ", ".join(diagnostics)
        if body:
            detail += f" / response={body}"
        if exc.code == 403 and not body:
            detail += " / 認証情報、OPS利用契約の有効化、APIアプリの状態、または利用上限を確認してください"
        return RuntimeError(detail)

    @staticmethod
    def _should_pause_search(error: str) -> bool:
        text = error.lower()
        return (
            "client.robotdetected" in text
            or "search=black:0" in text
            or "retry-after=" in text
            or "http 429" in text
        )

    def _token(self) -> str:
        auth = base64.b64encode(f"{self.key}:{self.secret}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            self.BASE + "/auth/accesstoken",
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))["access_token"]
        except urllib.error.HTTPError as exc:
            raise self._http_error("トークン取得", exc) from exc

    def _search_page(self, token: str, query: str, start: int, end: int) -> bytes:
        url = self.BASE + "/rest-services/published-data/search/biblio?" + urllib.parse.urlencode({"q": query})
        for attempt in range(4):
            self._wait()
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}", "Range": f"{start}-{end}", "Accept": "application/xml"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    content = response.read()
                self._capture_payload(
                    content,
                    "published-data/search/biblio",
                    query=query,
                    range_start=start,
                    range_end=end,
                )
                return content
            except urllib.error.HTTPError as exc:
                body = self._http_error_body(exc)
                if body:
                    self._capture_payload(
                        body.encode("utf-8", errors="replace"),
                        "published-data/search/biblio/error",
                        query=query,
                        range_start=start,
                        range_end=end,
                        http_status=exc.code,
                    )
                if self._is_expired_token(exc, body):
                    raise EPOOPSTokenExpired("EPO OPS access token expired") from exc
                if exc.code == 404 and ("SERVER.EntityNotFound" in body or "No results found" in body):
                    return self._empty_search_response()
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == 3:
                    raise self._http_error("特許検索", exc, body) from exc
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = max(self.interval, float(retry_after))
                except ValueError:
                    delay = max(self.interval, 2 ** attempt)
                time.sleep(min(delay, 120.0))
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"EPO OPS 特許検索接続失敗: {exc}") from exc
                time.sleep(max(self.interval, 2 ** attempt))
            finally:
                # Failed calls also consume OPS quota; preserve the global request interval.
                self.last_request = time.monotonic()
        raise AssertionError("unreachable")

    def _published_data(self, token: str, publication_number: str, constituent: str) -> bytes:
        normalized = re.sub(r"[^0-9A-Z]", "", publication_number.upper())
        identifiers = [normalized]
        match = re.fullmatch(r"([A-Z]{2})(\d+)([A-Z]\d?)", normalized)
        if match:
            identifiers.append(match.group(1) + match.group(2))
        last_error: RuntimeError | None = None
        for identifier_index, identifier in enumerate(dict.fromkeys(identifiers)):
            document = urllib.parse.quote(identifier, safe="")
            url = self.BASE + f"/rest-services/published-data/publication/epodoc/{document}/{constituent}"
            for attempt in range(4):
                self._wait()
                request = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/xml",
                        "Accept-Language": "en",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        content = response.read()
                    self._capture_payload(
                        content,
                        f"published-data/publication/epodoc/{constituent}",
                        publication_number=publication_number,
                        constituent=constituent,
                    )
                    return content
                except urllib.error.HTTPError as exc:
                    body = self._http_error_body(exc)
                    if body:
                        self._capture_payload(
                            body.encode("utf-8", errors="replace"),
                            f"published-data/publication/epodoc/{constituent}/error",
                            publication_number=publication_number,
                            constituent=constituent,
                            http_status=exc.code,
                        )
                    if self._is_expired_token(exc, body):
                        raise EPOOPSTokenExpired("EPO OPS access token expired") from exc
                    retryable = exc.code == 429 or 500 <= exc.code < 600
                    has_fallback = identifier_index + 1 < len(identifiers)
                    if exc.code in {400, 404} and has_fallback:
                        last_error = self._http_error(f"{constituent}詳細取得", exc, body)
                        break
                    if not retryable or attempt == 3:
                        raise self._http_error(f"{constituent}詳細取得", exc, body) from exc
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        delay = max(self.interval, float(retry_after))
                    except ValueError:
                        delay = max(self.interval, 2 ** attempt)
                    time.sleep(min(delay, 120.0))
                except (TimeoutError, urllib.error.URLError) as exc:
                    if attempt == 3:
                        raise RuntimeError(f"EPO OPS {constituent}詳細取得失敗: {exc}") from exc
                    time.sleep(max(self.interval, 2 ** attempt))
                finally:
                    self.last_request = time.monotonic()
        if last_error:
            raise last_error
        raise RuntimeError(f"EPO OPS {constituent}詳細取得: 公開番号を解釈できません: {publication_number}")

    def _family_data(self, token: str, publication_number: str) -> bytes:
        normalized = re.sub(r"[^0-9A-Z]", "", publication_number.upper())
        match = re.fullmatch(r"([A-Z]{2})(\d+)([A-Z]\d?)", normalized)
        identifier = f"{match.group(1)}.{match.group(2)}.{match.group(3)}" if match else normalized
        document = urllib.parse.quote(identifier, safe="")
        endpoint = "family/publication/docdb/biblio,legal"
        url = self.BASE + f"/rest-services/family/publication/docdb/{document}/biblio,legal"
        for attempt in range(4):
            self._wait()
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                    "Accept-Language": "en",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    content = response.read()
                self._capture_payload(
                    content,
                    endpoint,
                    publication_number=publication_number,
                    constituent="family-biblio,legal",
                )
                return content
            except urllib.error.HTTPError as exc:
                body = self._http_error_body(exc)
                if body:
                    self._capture_payload(
                        body.encode("utf-8", errors="replace"),
                        endpoint + "/error",
                        publication_number=publication_number,
                        constituent="family-biblio,legal",
                        http_status=exc.code,
                    )
                if self._is_expired_token(exc, body):
                    raise EPOOPSTokenExpired("EPO OPS access token expired") from exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == 3:
                    raise self._http_error("family biblio,legal", exc, body) from exc
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = max(self.interval, float(retry_after))
                except ValueError:
                    delay = max(self.interval, 2 ** attempt)
                time.sleep(min(delay, 120.0))
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"EPO OPS family biblio,legal connection failed: {exc}") from exc
                time.sleep(max(self.interval, 2 ** attempt))
            finally:
                self.last_request = time.monotonic()
        raise AssertionError("unreachable")

    def probe(self, query: str) -> dict[str, Any]:
        token = self._token()
        try:
            content = self._search_page(token, query, 1, 1)
        except EPOOPSTokenExpired:
            token = self._token()
            content = self._search_page(token, query, 1, 1)
        records = self._parse(content, self.last_response_metadata)
        return {"token_ok": True, "search_ok": True, "sample_count": len(records), "query": query}

    def search(self, query: str, max_records: int = 100, progress: Callable[[str], None] | None = None) -> list[PatentRecord]:
        token = self._token()
        records: list[PatentRecord] = []
        unlimited = max_records <= 0
        limit_label = "unlimited" if unlimited else str(max_records)
        start = 1
        while unlimited or start <= max_records:
            end = start + 99 if unlimited else min(max_records, start + 99)
            try:
                content = self._search_page(token, query, start, end)
            except EPOOPSTokenExpired:
                if progress:
                    progress("EPO OPSアクセストークン期限切れ。再取得して検索を継続します")
                token = self._token()
                content = self._search_page(token, query, start, end)
            page = self._parse(content, self.last_response_metadata)
            records.extend(page)
            if progress:
                progress(f"EPO OPS取得 {len(records)}/{limit_label}件")
            if len(page) < end - start + 1:
                break
            start += 100
        return records if unlimited else records[:max_records]

    def search_companies(
        self, companies: list[Company], start_date: str, end_date: str,
        max_per_company: int = 100, progress: Callable[[str], None] | None = None,
        company_result: Callable[[Company, bool, str], None] | None = None,
        max_applicant_names: int = 8,
        deadline_monotonic: float | None = None,
        checkpoint_path: str | Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
        completed_company_ids: set[str] | None = None,
        company_records_checkpoint: Callable[[Company, list[PatentRecord]], None] | None = None,
    ) -> list[PatentRecord]:
        """Search each monitored applicant independently to avoid global-result truncation."""
        stop_requested = stop_requested or (lambda: False)
        token = self._token()
        found: dict[str, PatentRecord] = {}
        failures = 0
        start = start_date.replace("-", "")
        end = end_date.replace("-", "")
        completed_company_ids = completed_company_ids or set()
        all_targets = [company for company in companies if company.target]
        targets = [company for company in all_targets if company.company_id not in completed_company_ids]
        stats: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "max_per_company": max_per_company,
            "max_applicant_names": max_applicant_names,
            "companies": [],
            "capped_companies": [],
            "skipped_companies": [],
            "failure_count": 0,
            "stopped_by_deadline": False,
            "stopped_by_request": False,
            "stopped_by_throttle": False,
            "throttle_error": "",
            "throttle_pause_count": 0,
            "current_requests_per_minute": self.requests_per_minute,
            "unique_records": 0,
            "total_target_companies": len(all_targets),
            "resumed_company_count": len(all_targets) - len(targets),
        }
        for index, company in enumerate(targets, 1):
            if stop_requested():
                stats["stopped_by_request"] = True
                stats["skipped_companies"].extend(
                    {"company_id": item.company_id, "company_name": item.company_name}
                    for item in targets[index - 1 :]
                )
                if progress:
                    progress("EPO company search stopped by user request; remaining companies queued for retry.")
                break
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                stats["stopped_by_deadline"] = True
                stats["skipped_companies"].extend(
                    {"company_id": item.company_id, "company_name": item.company_name}
                    for item in targets[index - 1 :]
                )
                if progress:
                    progress("EPO company search stopped by runtime guard; remaining companies queued for retry.")
                break
            name_limit = max(1, max_applicant_names)
            applicant_names = company.search_names(name_limit)
            applicant_clause = " or ".join(f'pa="{name}"' for name in applicant_names)
            query = f'pd within "{start} {end}" and ({applicant_clause})'
            self.request_context = {
                "query_window_start": start_date,
                "query_window_end": end_date,
                "company_id": company.company_id,
                "company_name": company.company_name,
                "searched_names": applicant_names,
            }
            company_records: list[PatentRecord] = []
            capped = False
            skip_current_reason = ""
            try:
                unlimited = max_per_company <= 0
                page_start = 1
                while unlimited or page_start <= max_per_company:
                    if stop_requested():
                        stats["stopped_by_request"] = True
                        break
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        skip_current_reason = "skipped_stalled: company search exceeded per-company deadline"
                        stats["skipped_companies"].append({
                            "company_id": company.company_id,
                            "company_name": company.company_name,
                            "status": "skipped_stalled",
                            "reason": skip_current_reason,
                            "record_count": len(company_records),
                            "capped": False,
                            "searched_names": applicant_names,
                        })
                        if company_result:
                            company_result(company, False, skip_current_reason)
                        if progress:
                            progress(f"EPO企業別検索を時間超過でスキップ {company.company_name}: {skip_current_reason}")
                        break
                    page_end = page_start + 99 if unlimited else min(max_per_company, page_start + 99)
                    try:
                        content = self._search_page(token, query, page_start, page_end)
                    except EPOOPSTokenExpired:
                        if progress:
                            progress("EPO OPSアクセストークン期限切れ。再取得して企業別検索を継続します")
                        token = self._token()
                        content = self._search_page(token, query, page_start, page_end)
                    except RuntimeError as exc:
                        if not self._should_pause_search(str(exc)):
                            raise
                        stats["throttle_error"] = str(exc)
                        stats["throttle_pause_count"] += 1
                        new_rate = self.reduce_rate_after_throttle(8)
                        stats["current_requests_per_minute"] = new_rate
                        if progress:
                            progress(
                                "EPOスロットル/robot検知。60秒待機して "
                                f"{new_rate} req/min に下げ、同じ位置から再開します。"
                            )
                        if self.throttle_pause_seconds > 0:
                            time.sleep(self.throttle_pause_seconds)
                        token = self._token()
                        continue
                    page = self._parse(content, self.last_response_metadata)
                    company_records.extend(page)
                    if len(page) < page_end - page_start + 1:
                        break
                    if not unlimited and page_end >= max_per_company:
                        capped = True
                        break
                    page_start += 100
                if skip_current_reason:
                    continue
                if stats["stopped_by_request"]:
                    stats["skipped_companies"].extend(
                        {"company_id": item.company_id, "company_name": item.company_name}
                        for item in targets[index - 1 :]
                    )
                    if progress:
                        progress("EPO company search stopped by user request; remaining companies queued for retry.")
                    break
            except RuntimeError as exc:
                if self._should_pause_search(str(exc)):
                    stats["stopped_by_throttle"] = True
                    stats["throttle_error"] = str(exc)
                    stats["skipped_companies"].extend(
                        {"company_id": item.company_id, "company_name": item.company_name}
                        for item in targets[index - 1 :]
                    )
                    if progress:
                        progress(
                            "EPO search paused by throttle/robot detection; "
                            "remaining companies queued for retry."
                        )
                    break
                failures += 1
                stats["failure_count"] = failures
                stats["companies"].append({
                    "company_id": company.company_id,
                    "company_name": company.company_name,
                    "success": False,
                    "record_count": len(company_records),
                    "capped": False,
                    "searched_names": applicant_names,
                    "error": str(exc),
                })
                if company_result:
                    company_result(company, False, str(exc))
                if progress:
                    progress(f"EPO企業別検索失敗 {company.company_name}: {exc}")
                continue
            for record in company_records:
                found[record.identity] = record
            if company_records_checkpoint:
                company_records_checkpoint(company, company_records)
            if company_result:
                company_result(company, True, "")
            company_stat = {
                "company_id": company.company_id,
                "company_name": company.company_name,
                "success": True,
                "record_count": len(company_records),
                "capped": capped,
                "searched_names": applicant_names,
                "error": "",
            }
            stats["companies"].append(company_stat)
            if capped:
                stats["capped_companies"].append(company_stat)
            if progress:
                progress(
                    f"EPO企業別検索 {index}/{len(targets)}社: {company.company_name} "
                    f"新規候補={len(company_records)} 累計={len(found)} 失敗={failures}"
                )
            if checkpoint_path:
                stats["unique_records"] = len(found)
                write_json_atomic(checkpoint_path, stats)
            if stats["stopped_by_request"]:
                stats["skipped_companies"].extend(
                    {"company_id": item.company_id, "company_name": item.company_name}
                    for item in targets[index:]
                )
                break
        stats["unique_records"] = len(found)
        stats["failure_count"] = failures
        self.last_company_search_stats = stats
        return list(found.values())

    def search_technologies(
        self,
        queries: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        max_records_per_query: int = 10,
        deadline_monotonic: float | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[PatentRecord], dict[str, Any]]:
        """Search same-day patents by configured title/abstract technology queries."""
        stats: dict[str, Any] = {
            "configured_query_count": len(queries),
            "executed_query_count": 0,
            "successful_query_count": 0,
            "error_count": 0,
            "stopped_by_deadline": False,
            "unique_records": 0,
            "queries": [],
        }
        if not queries or max_records_per_query <= 0:
            return [], stats
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            stats["stopped_by_deadline"] = True
            return [], stats
        token = self._token()
        found: dict[str, PatentRecord] = {}
        for index, specification in enumerate(queries, 1):
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                stats["stopped_by_deadline"] = True
                break
            name = clean_text(specification.get("name")) or f"technology-{index}"
            clause = clean_text(specification.get("query"))
            if not clause:
                stats["error_count"] += 1
                stats["queries"].append({"name": name, "status": "invalid", "record_count": 0})
                continue
            query = f'pd within "{start_date} {end_date}" and ({clause})'
            self.request_context = {
                "query_window_start": start_date,
                "query_window_end": end_date,
                "query_kind": "technology_discovery",
                "technology_category": name,
            }
            stats["executed_query_count"] += 1
            try:
                content = self._search_page(token, query, 1, min(100, max_records_per_query))
            except EPOOPSTokenExpired:
                token = self._token()
                content = self._search_page(token, query, 1, min(100, max_records_per_query))
            except RuntimeError as exc:
                stats["error_count"] += 1
                stats["queries"].append({
                    "name": name, "status": "error", "record_count": 0, "error": str(exc),
                })
                if progress:
                    progress(f"EPO技術別検索失敗 {name}: {exc}")
                continue
            records = self._parse(content, self.last_response_metadata)[:max_records_per_query]
            for record in records:
                record.raw["technology_discovery"] = True
                categories = record.raw.setdefault("technology_categories", [])
                if name not in categories:
                    categories.append(name)
                existing = found.get(record.identity)
                if existing is None:
                    found[record.identity] = record
                else:
                    merged = existing.raw.setdefault("technology_categories", [])
                    for category in categories:
                        if category not in merged:
                            merged.append(category)
            stats["successful_query_count"] += 1
            stats["queries"].append({"name": name, "status": "completed", "record_count": len(records)})
            if progress:
                progress(f"EPO技術別検索 {index}/{len(queries)}: {name} 候補={len(records)}")
        stats["unique_records"] = len(found)
        return list(found.values()), stats

    def enrich_records(
        self,
        records: list[PatentRecord],
        companies: list[Company],
        max_records: int = 200,
        match_threshold: float = 0.90,
        match_margin: float = 0.05,
        progress: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        enrich_claims: bool = True,
        claims_top_rate: float = 1.0,
        enrich_family_legal: bool = True,
        enrich_forward_citations: bool = True,
        forward_citation_max_records: int = 100,
        include_technology_discovery: bool = False,
    ) -> dict[str, Any]:
        """Add classifications and an English abstract only to monitored-company candidates.

        OPS search responses are intentionally compact and often omit CPC/IPC and abstracts.
        Fetching details for every global result would waste the quota, so matching is done first.
        Failures are recorded per patent and do not abort the daily batch.
        """
        stop_requested = stop_requested or (lambda: False)
        matcher = CompanyMatcher(companies, threshold=match_threshold, margin=match_margin)
        candidates = []
        for record in records:
            match = matcher.match(record.applicants)
            monitored_company = bool(match.company_id) and not match.review_required
            technology_discovery = bool(record.raw.get("technology_discovery"))
            if monitored_company or (include_technology_discovery and technology_discovery):
                candidates.append(record)
        if max_records > 0:
            candidates = candidates[:max_records]
        summary = {
            "candidate_count": len(candidates),
            "enriched_count": 0,
            "classification_filled_count": 0,
            "abstract_filled_count": 0,
            "claims_filled_count": 0,
            "claims_candidate_count": 0,
            "claims_top_rate": claims_top_rate,
            "family_filled_count": 0,
            "legal_event_count": 0,
            "forward_citation_count": 0,
            "error_count": 0,
            "error_detail_count": 0,
            "error_kind_counts": {},
            "warning_count": 0,
            "warning_detail_count": 0,
            "warning_kind_counts": {},
            "cache_hit_count": 0,
            "cache_write_count": 0,
            "stopped_by_request": False,
            "deferred_count": 0,
        }
        if not candidates:
            return summary
        if stop_requested():
            summary["stopped_by_request"] = True
            summary["deferred_count"] = len(candidates)
            if progress:
                progress("EPO詳細補完を停止要求により中断します")
            return summary
        token = self._token()
        for index, record in enumerate(candidates, 1):
            if stop_requested():
                summary["stopped_by_request"] = True
                if progress:
                    progress("EPO詳細補完を停止要求により中断します")
                break
            if progress:
                progress(f"EPO詳細補完 開始 {index}/{len(candidates)}件: {record.publication_number}")
            record_match = matcher.match(record.applicants)
            self.request_context = {
                "query_window_start": record.query_window_start,
                "query_window_end": record.query_window_end,
                "company_id": record_match.company_id,
                "company_name": record_match.company_name,
            }
            before_codes = len(record.cpc_codes) + len(record.ipc_codes)
            before_abstract = bool(record.abstract)
            try:
                try:
                    detail_content = self._cached_published_data(record.publication_number, "biblio")
                    if detail_content is not None:
                        summary["cache_hit_count"] += 1
                    else:
                        detail_content = self._published_data(token, record.publication_number, "biblio")
                        self._store_published_data_cache(record.publication_number, "biblio", detail_content)
                        summary["cache_write_count"] += 1
                except EPOOPSTokenExpired:
                    if progress:
                        progress("EPO OPSアクセストークン期限切れ。再取得して詳細補完を継続します")
                    token = self._token()
                    detail_content = self._published_data(token, record.publication_number, "biblio")
                    self._store_published_data_cache(record.publication_number, "biblio", detail_content)
                    summary["cache_write_count"] += 1
                detail_records = self._parse(detail_content, self.last_response_metadata)
                if detail_records:
                    self._merge_record(record, detail_records[0])
            except RuntimeError as exc:
                _record_epo_detail_issue(record, "biblio", exc, summary)
            if not record.abstract:
                if stop_requested():
                    summary["stopped_by_request"] = True
                    break
                try:
                    try:
                        abstract_content = self._cached_published_data(record.publication_number, "abstract")
                        if abstract_content is not None:
                            summary["cache_hit_count"] += 1
                        else:
                            abstract_content = self._published_data(token, record.publication_number, "abstract")
                            self._store_published_data_cache(record.publication_number, "abstract", abstract_content)
                            summary["cache_write_count"] += 1
                    except EPOOPSTokenExpired:
                        if progress:
                            progress("EPO OPSアクセストークン期限切れ。再取得して要約補完を継続します")
                        token = self._token()
                        abstract_content = self._published_data(token, record.publication_number, "abstract")
                        self._store_published_data_cache(record.publication_number, "abstract", abstract_content)
                        summary["cache_write_count"] += 1
                    abstract_records = self._parse(abstract_content, self.last_response_metadata)
                    if abstract_records:
                        self._merge_record(record, abstract_records[0])
                except RuntimeError as exc:
                    _record_epo_detail_issue(record, "abstract", exc, summary)
            if enrich_claims and claims_top_rate >= 1.0 and not record.claims and not stop_requested():
                try:
                    claims_content = self._cached_published_data(record.publication_number, "claims")
                    if claims_content is not None:
                        summary["cache_hit_count"] += 1
                    else:
                        claims_content = self._published_data(token, record.publication_number, "claims")
                        self._store_published_data_cache(record.publication_number, "claims", claims_content)
                        summary["cache_write_count"] += 1
                    claims_records = self._parse(claims_content, self.last_response_metadata)
                    if claims_records:
                        before_claims = bool(record.claims)
                        self._merge_record(record, claims_records[0])
                        if not before_claims and record.claims:
                            summary["claims_filled_count"] += 1
                except EPOOPSTokenExpired:
                    token = self._token()
                    try:
                        claims_content = self._published_data(token, record.publication_number, "claims")
                        self._store_published_data_cache(record.publication_number, "claims", claims_content)
                        claims_records = self._parse(claims_content, self.last_response_metadata)
                        if claims_records:
                            self._merge_record(record, claims_records[0])
                    except RuntimeError as exc:
                        _record_epo_detail_issue(record, "claims", exc, summary)
                except RuntimeError as exc:
                    _record_epo_detail_issue(record, "claims", exc, summary)

            if enrich_family_legal and not stop_requested():
                try:
                    family_content = self._cached_published_data(record.publication_number, "family-biblio,legal")
                    if family_content is not None:
                        summary["cache_hit_count"] += 1
                    else:
                        family_content = self._family_data(token, record.publication_number)
                        self._store_published_data_cache(
                            record.publication_number, "family-biblio,legal", family_content
                        )
                        summary["cache_write_count"] += 1
                    family = self._parse_family_metadata(family_content)
                    record.family_id = family["family_id"] or record.family_id
                    record.family_members = list(
                        dict.fromkeys([*record.family_members, *family["family_members"]])
                    )
                    record.country_codes = sorted(set([*record.country_codes, *family["country_codes"]]))
                    record.family_size = max(record.family_size, family["family_size"])
                    record.legal_events = family["legal_events"]
                    record.ownership_events = family["ownership_events"]
                    record.legal_status = family["legal_status"] or record.legal_status
                    family_hash = hashlib.sha256(family_content).hexdigest()
                    record.raw.setdefault("payload_hashes", []).append(family_hash)
                    summary["family_filled_count"] += int(bool(record.family_members))
                    summary["legal_event_count"] += len(record.legal_events)
                except EPOOPSTokenExpired:
                    token = self._token()
                    try:
                        family_content = self._family_data(token, record.publication_number)
                        self._store_published_data_cache(
                            record.publication_number, "family-biblio,legal", family_content
                        )
                        family = self._parse_family_metadata(family_content)
                        record.family_id = family["family_id"] or record.family_id
                        record.family_members = family["family_members"]
                        record.country_codes = sorted(set([*record.country_codes, *family["country_codes"]]))
                        record.family_size = max(record.family_size, family["family_size"])
                        record.legal_events = family["legal_events"]
                        record.ownership_events = family["ownership_events"]
                        record.legal_status = family["legal_status"] or record.legal_status
                    except RuntimeError as exc:
                        _record_epo_detail_issue(record, "family_legal", exc, summary)
                except RuntimeError as exc:
                    _record_epo_detail_issue(record, "family_legal", exc, summary)

            if enrich_forward_citations and not stop_requested():
                try:
                    cited_content = self._cached_published_data(record.publication_number, "cited-by")
                    if cited_content is not None:
                        summary["cache_hit_count"] += 1
                    else:
                        normalized = re.sub(r"[^0-9A-Z]", "", record.publication_number.upper())
                        citation_limit = max(1, forward_citation_max_records)
                        self.request_context = {
                            **self.request_context,
                            "publication_number": record.publication_number,
                            "query_kind": "forward_citations",
                        }
                        cited_content = self._search_page(
                            token, f'ct="{normalized}"', 1, min(100, citation_limit)
                        )
                        self._store_published_data_cache(record.publication_number, "cited-by", cited_content)
                        summary["cache_write_count"] += 1
                    cited_records = self._parse(cited_content, self.last_response_metadata)
                    record.cited_by = list(dict.fromkeys(item.publication_number for item in cited_records))
                    record.citation_count = max(self._search_total_count(cited_content), len(record.cited_by))
                    record.raw["cited_by_total"] = record.citation_count
                    record.raw.setdefault("payload_hashes", []).append(hashlib.sha256(cited_content).hexdigest())
                    summary["forward_citation_count"] += record.citation_count
                except EPOOPSTokenExpired:
                    token = self._token()
                    _record_epo_detail_issue(
                        record,
                        "forward_citations",
                        RuntimeError("token expired; retry on next enrichment"),
                        summary,
                    )
                except RuntimeError as exc:
                    _record_epo_detail_issue(record, "forward_citations", exc, summary)

            record.raw["epo_detail_enriched"] = bool(
                record.abstract
                or record.claims
                or record.cpc_codes
                or record.ipc_codes
                or record.family_members
                or record.legal_events
            )
            if record.detail_enriched:
                summary["enriched_count"] += 1
            if before_codes == 0 and record.cpc_codes + record.ipc_codes:
                summary["classification_filled_count"] += 1
            if not before_abstract and record.abstract:
                summary["abstract_filled_count"] += 1
            if progress:
                progress(
                    f"EPO詳細補完 {index}/{len(candidates)}件: {record.publication_number} "
                    f"分類={len(record.cpc_codes) + len(record.ipc_codes)} 要約={'あり' if record.abstract else 'なし'}"
                )
        if enrich_claims and 0.0 < claims_top_rate < 1.0 and not summary["stopped_by_request"]:
            pending_claims = [record for record in candidates if not record.claims]
            claims_limit = max(1, math.ceil(len(candidates) * claims_top_rate)) if pending_claims else 0
            claims_candidates = sorted(
                pending_claims,
                key=lambda item: (
                    metadata_score(item)[0],
                    item.citation_count,
                    item.family_size,
                    len(item.cpc_codes) + len(item.ipc_codes),
                    item.publication_date,
                ),
                reverse=True,
            )[:claims_limit]
            summary["claims_candidate_count"] = len(claims_candidates)
            for claim_index, record in enumerate(claims_candidates, 1):
                if stop_requested():
                    summary["stopped_by_request"] = True
                    if progress:
                        progress("EPO請求項補完を停止要求により中断します")
                    break
                try:
                    claims_content = self._cached_published_data(record.publication_number, "claims")
                    if claims_content is not None:
                        summary["cache_hit_count"] += 1
                    else:
                        claims_content = self._published_data(token, record.publication_number, "claims")
                        self._store_published_data_cache(record.publication_number, "claims", claims_content)
                        summary["cache_write_count"] += 1
                    claims_records = self._parse(claims_content, self.last_response_metadata)
                    if claims_records:
                        before_claims = bool(record.claims)
                        self._merge_record(record, claims_records[0])
                        if not before_claims and record.claims:
                            summary["claims_filled_count"] += 1
                except EPOOPSTokenExpired:
                    token = self._token()
                    try:
                        claims_content = self._published_data(token, record.publication_number, "claims")
                        self._store_published_data_cache(record.publication_number, "claims", claims_content)
                        claims_records = self._parse(claims_content, self.last_response_metadata)
                        if claims_records:
                            before_claims = bool(record.claims)
                            self._merge_record(record, claims_records[0])
                            if not before_claims and record.claims:
                                summary["claims_filled_count"] += 1
                    except RuntimeError as exc:
                        _record_epo_detail_issue(record, "claims", exc, summary)
                except RuntimeError as exc:
                    _record_epo_detail_issue(record, "claims", exc, summary)
                if progress:
                    progress(
                        f"EPO請求項補完 {claim_index}/{len(claims_candidates)}件: "
                        f"{record.publication_number}"
                    )
        if summary["stopped_by_request"]:
            summary["deferred_count"] = sum(not record.detail_enriched for record in candidates)
        return summary

    @staticmethod
    def _merge_record(target: PatentRecord, detail: PatentRecord) -> None:
        # _parse() has already selected English where available.
        if detail.title:
            target.title = detail.title
        if len(detail.abstract) > len(target.abstract):
            target.abstract = detail.abstract
        if len(detail.claims) > len(target.claims):
            target.claims = detail.claims
        target.applicants = list(dict.fromkeys([*target.applicants, *detail.applicants]))
        target.inventors = list(dict.fromkeys([*target.inventors, *detail.inventors]))
        target.cpc_codes = sorted(set([*target.cpc_codes, *detail.cpc_codes]))
        target.ipc_codes = sorted(set([*target.ipc_codes, *detail.ipc_codes]))
        target.country_codes = sorted(set([*target.country_codes, *detail.country_codes]))
        target.application_number = target.application_number or detail.application_number
        target.priority_number = target.priority_number or detail.priority_number
        target.priority_numbers = list(dict.fromkeys([*target.priority_numbers, *detail.priority_numbers]))
        target.filing_date = target.filing_date or detail.filing_date
        target.priority_date = target.priority_date or detail.priority_date
        target.publication_date = target.publication_date or detail.publication_date
        target.family_id = target.family_id or detail.family_id
        target.family_members = list(dict.fromkeys([*target.family_members, *detail.family_members]))
        target.family_size = max(target.family_size, detail.family_size, len(target.family_members))
        target.citations = list(dict.fromkeys([*target.citations, *detail.citations]))
        target.cited_by = list(dict.fromkeys([*target.cited_by, *detail.cited_by]))
        target.citation_count = max(target.citation_count, detail.citation_count, len(target.cited_by))
        target.non_patent_citations = list(
            dict.fromkeys([*target.non_patent_citations, *detail.non_patent_citations])
        )
        target.claim_count = max(target.claim_count, detail.claim_count)
        target.legal_status = detail.legal_status or target.legal_status
        target.legal_events = [*target.legal_events, *detail.legal_events]
        target.ownership_events = [*target.ownership_events, *detail.ownership_events]
        retrieval_times = [value for value in (target.retrieved_at, detail.retrieved_at) if value]
        target.retrieved_at = min(retrieval_times) if retrieval_times else ""
        target.source_endpoint = target.source_endpoint or detail.source_endpoint
        target.payload_hash = target.payload_hash or detail.payload_hash
        target.parser_version = detail.parser_version or target.parser_version
        payload_hashes = list(
            dict.fromkeys(
                [
                    *(target.raw.get("payload_hashes") or []),
                    *(detail.raw.get("payload_hashes") or []),
                ]
            )
        )
        target.raw.update(detail.raw)
        target.raw["payload_hashes"] = payload_hashes

    @staticmethod
    def _normalize_date(value: str) -> str:
        digits = re.sub(r"[^0-9]", "", clean_text(value))
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if len(digits) == 6:
            return f"{digits[:4]}-{digits[4:6]}"
        return clean_text(value)

    @staticmethod
    def _reference_document(reference: ET.Element | None) -> tuple[str, str]:
        if reference is None:
            return "", ""
        document_ids = reference.findall("./{*}document-id")
        ordered = sorted(
            document_ids,
            key=lambda node: 0 if node.attrib.get("document-id-type") == "docdb" else 1,
        )
        fallback_date = ""
        for document_id in ordered:
            country = clean_text(document_id.findtext("./{*}country"))
            number = clean_text(document_id.findtext("./{*}doc-number"))
            kind = clean_text(document_id.findtext("./{*}kind"))
            date_value = EPOOPSProvider._normalize_date(document_id.findtext("./{*}date") or "")
            fallback_date = fallback_date or date_value
            if number:
                value = number if not country or number.upper().startswith(country.upper()) else f"{country}{number}{kind}"
                return re.sub(r"[^0-9A-Z]", "", value.upper()), date_value
        return "", fallback_date

    @staticmethod
    def _parse(content: bytes, metadata: dict[str, Any] | None = None) -> list[PatentRecord]:
        metadata = metadata or {}
        root = ET.fromstring(content)
        result = []
        for exchange in root.findall(".//{*}exchange-document"):
            country = exchange.attrib.get("country", "")
            doc_number = exchange.attrib.get("doc-number", "")
            kind = exchange.attrib.get("kind", "")
            title_nodes = exchange.findall(".//{*}invention-title")
            titles = EPOOPSProvider._preferred_language_texts(title_nodes)
            abstract_nodes = exchange.findall(".//{*}abstract")
            abstracts = EPOOPSProvider._preferred_language_texts(abstract_nodes)
            claim_nodes = exchange.findall(".//{*}claims")
            claims = EPOOPSProvider._preferred_language_texts(claim_nodes)
            applicants = [clean_text(node.text) for node in exchange.findall(".//{*}applicant//{*}name") if clean_text(node.text)]
            inventors = [clean_text(node.text) for node in exchange.findall(".//{*}inventor//{*}name") if clean_text(node.text)]
            ipc_codes, cpc_codes = EPOOPSProvider._classifications(exchange)
            publication_ref = exchange.find(".//{*}publication-reference")
            _, publication_date = EPOOPSProvider._reference_document(publication_ref)
            application_ref = exchange.find(".//{*}application-reference")
            application_number, filing_date = EPOOPSProvider._reference_document(application_ref)
            priorities: list[tuple[str, str]] = []
            for priority in exchange.findall(".//{*}priority-claim"):
                number, priority_date = EPOOPSProvider._reference_document(priority)
                if number:
                    priorities.append((number, priority_date))
            cited_documents: list[str] = []
            non_patent_citations: list[str] = []
            for citation in exchange.findall(".//{*}references-cited/{*}citation"):
                patent_citation = citation.find(".//{*}patcit")
                if patent_citation is not None:
                    number, _ = EPOOPSProvider._reference_document(patent_citation)
                    if not number:
                        number = re.sub(
                            r"[^0-9A-Z]", "", clean_text(patent_citation.attrib.get("dnum")).upper()
                        )
                    if number:
                        cited_documents.append(number)
                non_patent = citation.find(".//{*}nplcit")
                if non_patent is not None:
                    value = clean_text(" ".join(non_patent.itertext()))
                    if value:
                        non_patent_citations.append(value)
            individual_claims = exchange.findall(".//{*}claims/{*}claim")
            family_id = clean_text(
                exchange.attrib.get("family-id")
                or exchange.attrib.get("docdb-family-id")
                or exchange.attrib.get("inpadoc-family-id")
            )
            publication_number = f"{country}{doc_number}{kind}"
            result.append(
                PatentRecord(
                    source="epo_ops",
                    publication_number=publication_number,
                    title=titles[0] if titles else "",
                    abstract=" ".join(abstracts),
                    claims=" ".join(claims),
                    applicants=list(dict.fromkeys(applicants)),
                    inventors=list(dict.fromkeys(inventors)),
                    ipc_codes=ipc_codes,
                    cpc_codes=cpc_codes,
                    application_number=application_number,
                    family_id=family_id,
                    priority_number=priorities[0][0] if priorities else "",
                    priority_numbers=list(dict.fromkeys(number for number, _ in priorities)),
                    filing_date=filing_date,
                    publication_date=publication_date,
                    priority_date=min((value for _, value in priorities if value), default=""),
                    country_codes=[country] if country else [],
                    citations=list(dict.fromkeys(cited_documents)),
                    non_patent_citations=list(dict.fromkeys(non_patent_citations)),
                    claim_count=len(individual_claims),
                    retrieved_at=clean_text(metadata.get("retrieved_at")),
                    source_endpoint=clean_text(metadata.get("source_endpoint")),
                    query_window_start=clean_text(metadata.get("query_window_start")),
                    query_window_end=clean_text(metadata.get("query_window_end")),
                    payload_hash=clean_text(metadata.get("payload_hash")) or hashlib.sha256(content).hexdigest(),
                    parser_version=clean_text(metadata.get("parser_version")) or PARSER_VERSION,
                    source_url=f"https://worldwide.espacenet.com/patent/search?q=pn%3D{publication_number}",
                    raw={
                        "exchange_attributes": dict(exchange.attrib),
                        "payload_hashes": [clean_text(metadata.get("payload_hash")) or hashlib.sha256(content).hexdigest()],
                    },
                )
            )
        return result

    @staticmethod
    def _parse_family_metadata(content: bytes) -> dict[str, Any]:
        root = ET.fromstring(content)
        members: list[str] = []
        countries: list[str] = []
        for member in root.findall(".//{*}family-member"):
            publication_ref = member.find("./{*}publication-reference")
            number, _ = EPOOPSProvider._reference_document(publication_ref)
            if number:
                members.append(number)
                if len(number) >= 2 and number[:2].isalpha():
                    countries.append(number[:2])
        legal_events: list[dict[str, Any]] = []
        ownership_events: list[dict[str, Any]] = []
        for legal in root.findall(".//{*}legal"):
            fields = {
                re.sub(r"[^0-9A-Z]", "", child.tag.split("}")[-1].upper()): clean_text(" ".join(child.itertext()))
                for child in list(legal)
                if clean_text(" ".join(child.itertext()))
            }
            description = clean_text(legal.attrib.get("desc") or fields.get("L002EP") or fields.get("PRE"))
            event = {
                "code": clean_text(legal.attrib.get("code") or fields.get("L001EP") or fields.get("L008EP")),
                "description": description,
                "date": EPOOPSProvider._normalize_date(
                    legal.attrib.get("dateMigr") or fields.get("L007EP") or fields.get("L018EP") or ""
                ),
                "impact": clean_text(legal.attrib.get("infl")),
                "fields": fields,
            }
            if any(event.values()):
                legal_events.append(event)
            ownership_text = f"{event['code']} {description}".upper()
            if any(
                term in ownership_text
                for term in ("ASSIGN", "TRANSFER", "OWNER", "PROPRIETOR", "APPLICANT", "CHANGE OF NAME")
            ):
                ownership_events.append(event)
        status_text = " ".join(
            f"{item.get('code', '')} {item.get('description', '')}" for item in legal_events
        ).upper()
        if any(term in status_text for term in ("REVOK", "LAPSE", "CEASE", "EXPIRE", "WITHDRAW", "REFUS")):
            legal_status = "inactive"
        elif any(term in status_text for term in ("GRANT", "PATENTED")):
            legal_status = "granted"
        elif legal_events:
            legal_status = "active_or_unknown"
        else:
            legal_status = ""
        unique_members = list(dict.fromkeys(members))
        family_material = "|".join(sorted(unique_members))
        return {
            "family_id": "INPADOC-" + hashlib.sha256(family_material.encode("utf-8")).hexdigest()[:20]
            if family_material else "",
            "family_members": unique_members,
            "country_codes": sorted(set(countries)),
            "family_size": max(1, len(unique_members)),
            "legal_events": legal_events,
            "ownership_events": ownership_events,
            "legal_status": legal_status,
        }

    @staticmethod
    def _search_total_count(content: bytes) -> int:
        root = ET.fromstring(content)
        for node in root.iter():
            for key in ("total-result-count", "totalResultCount"):
                value = node.attrib.get(key)
                if value and str(value).isdigit():
                    return int(value)
        return 0

    @staticmethod
    def _preferred_language_texts(nodes: Iterable[ET.Element]) -> list[str]:
        values: list[tuple[str, str]] = []
        for node in nodes:
            text = clean_text(" ".join(node.itertext()))
            if not text:
                continue
            language = clean_text(
                node.attrib.get("lang")
                or node.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
            ).lower()
            values.append((language, text))
        english = [text for language, text in values if language.startswith("en")]
        return english or [text for _language, text in values]

    @staticmethod
    def _classifications(exchange: ET.Element) -> tuple[list[str], list[str]]:
        def normalized(value: str) -> str:
            return re.sub(r"\s+", "", clean_text(value).upper())

        ipc: set[str] = set()
        cpc: set[str] = set()
        for container_name in ("classification-ipcr", "classification-ipc"):
            for container in exchange.findall(f".//{{*}}{container_name}"):
                for node in container.findall(".//{*}text"):
                    code = normalized("".join(node.itertext()))
                    if code:
                        ipc.add(code)
                for node in container.findall(".//{*}classification-symbol"):
                    code = normalized("".join(node.itertext()))
                    if code:
                        ipc.add(code)

        for classification in exchange.findall(".//{*}patent-classification"):
            parts = {}
            for name in ("section", "class", "subclass", "main-group", "subgroup"):
                node = classification.find(f"{{*}}{name}")
                parts[name] = clean_text(node.text) if node is not None else ""
            code = normalized(
                f"{parts['section']}{parts['class']}{parts['subclass']}"
                + (f"{parts['main-group']}/{parts['subgroup']}" if parts["main-group"] else "")
            )
            scheme_node = classification.find("{*}classification-scheme")
            scheme = ""
            if scheme_node is not None:
                scheme = clean_text(scheme_node.attrib.get("scheme") or scheme_node.attrib.get("office")).upper()
            if code:
                (cpc if "CPC" in scheme or scheme == "EP" else ipc).add(code)

        generic = {
            normalized("".join(node.itertext()))
            for node in exchange.findall(".//{*}classification-symbol")
            if normalized("".join(node.itertext()))
        }
        ipc.update(generic - cpc)
        return sorted(ipc), sorted(cpc)


class PatentPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        companies: list[Company],
        database_path: str | Path,
        output_dir: str | Path,
        openai_api_key: str = "",
        gemini_api_key: str = "",
        progress: Callable[[str, float], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        item_scored: Callable[[str, ScoredPatent], None] | None = None,
    ):
        self.config = config
        self.companies = companies
        self.company_by_id = {company.company_id: company for company in companies}
        self.database = PatentDatabase(database_path)
        self.adaptive_scores = self.database.recent_gpt_peak_scores(config.adaptive_escalation_window)
        self.adaptive_new_samples = 0
        self.initial_escalation_threshold = config.gemini_escalation_threshold
        from .market_feedback import load_latest_model
        self.learning_model = load_latest_model(database_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.matcher = CompanyMatcher(companies, config.fuzzy_match_threshold, config.fuzzy_match_margin)
        self.gpt = OpenAIReviewer(openai_api_key, config.gpt_model, config.request_timeout_seconds) if openai_api_key else None
        self.gemini = GeminiReviewer(gemini_api_key, config.gemini_model, config.request_timeout_seconds) if gemini_api_key else None
        self.progress = progress or (lambda _message, _ratio: None)
        self.stop_requested = stop_requested or (lambda: False)
        self.item_scored = item_scored
        self.ai_budget_stopped = False
        self.ai_budget_stop_reason = ""

    def _route(self, item: ScoredPatent) -> None:
        if item.match.review_required:
            item.route = "company_review"
            item.route_reason = "企業名寄せが曖昧"
            return
        if not item.match.company_id:
            item.route = "unmatched"
            item.route_reason = "対象上場企業に一致しない"
            return
        if self.config.force_two_stage_review:
            item.route, item.route_reason = "gpt", "検証モードによる二段審査"
            return
        new_cpc = any(
            reason.startswith(("企業初CPC=", "new_classification="))
            for reason in item.novelty_reasons
        )
        if item.final_score >= self.config.gemini_threshold:
            item.route, item.route_reason = "gpt", "総合閾値以上"
        elif item.company_percentile >= self.config.company_percentile_threshold:
            item.route, item.route_reason = "gpt", "企業内上位パーセンタイル"
        elif (
            item.technology_score >= self.config.broad_technology_gemini_threshold
            and item.materiality_score >= self.config.broad_materiality_minimum
        ):
            item.route, item.route_reason = "gpt", "広域先端技術ルート"
        elif new_cpc and item.technology_score >= 45:
            item.route, item.route_reason = "gpt", "対象技術かつ企業初CPC"
        elif random.random() < self.config.random_reject_audit_rate:
            item.route, item.route_reason = "gpt_audit", "棄却標本監査"
        else:
            item.route, item.route_reason = "rejected", "事前評価閾値未満"

    def _should_escalate_to_gemini(self, result: dict[str, Any]) -> tuple[bool, str]:
        if self.config.force_two_stage_review:
            return True, "検証モードによるGemini再審査"
        threshold = self.config.gemini_escalation_threshold
        scores = {
            "重要度": float(result.get("importance_score", 0) or 0),
            "材料性": float(result.get("materiality_score", 0) or 0),
            "短期市場影響": float(result.get("short_term_market_impact_score", 0) or 0),
            "長期事業価値": float(result.get("long_term_business_value_score", 0) or 0),
            "新規性": float(result.get("novelty_score", 0) or 0),
        }
        decision = clean_text(result.get("decision")).lower()
        if decision in {"important", "urgent"}:
            return True, f"GPT判定={decision}"
        qualifying = [name for name, score in scores.items() if score >= threshold]
        if qualifying:
            return True, f"GPT上位スコア({','.join(qualifying)})"
        return False, "GPT一次審査で再審査不要"

    def _update_adaptive_escalation(self, result: dict[str, Any]) -> None:
        if not self.config.adaptive_gemini_escalation or self.config.force_two_stage_review:
            return
        peak = max(float(result.get(key, 0) or 0) for key in (
            "importance_score", "materiality_score", "short_term_market_impact_score",
            "long_term_business_value_score", "novelty_score",
        ))
        self.adaptive_scores.append(peak)
        self.adaptive_new_samples += 1
        window = max(1, self.config.adaptive_escalation_window)
        self.adaptive_scores = self.adaptive_scores[-window:]

    def _finalize_adaptive_escalation(self) -> None:
        if not self.config.adaptive_gemini_escalation or self.config.force_two_stage_review:
            return
        if self.adaptive_new_samples == 0:
            return
        if len(self.adaptive_scores) < self.config.adaptive_escalation_min_samples:
            return
        ordered = sorted(self.adaptive_scores)
        target_rate = max(0.01, min(0.50, self.config.gemini_target_escalation_rate))
        index = max(0, min(len(ordered) - 1, math.ceil((1.0 - target_rate) * len(ordered)) - 1))
        target_threshold = max(50.0, min(98.0, ordered[index]))
        current = self.config.gemini_escalation_threshold
        proposed = current + 0.20 * (target_threshold - current)
        max_step = max(0.1, self.config.adaptive_escalation_max_step)
        updated = max(current - max_step, min(current + max_step, proposed))
        self.config.gemini_escalation_threshold = round(max(50.0, min(98.0, updated)), 2)
        self.database.set_adaptive_value(
            "gemini_escalation_threshold", self.config.gemini_escalation_threshold
        )

    @staticmethod
    def _gpt_peak_score(item: ScoredPatent) -> float:
        result = item.gpt_result or {}
        return max(
            float(result.get(key, 0) or 0)
            for key in (
                "importance_score",
                "materiality_score",
                "short_term_market_impact_score",
                "long_term_business_value_score",
                "novelty_score",
            )
        )

    def _assign_ranked_ai_routes(self, items: list[ScoredPatent], source: str) -> None:
        eligible: list[ScoredPatent] = []
        technology_eligible: list[ScoredPatent] = []
        for item in items:
            if item.match.review_required:
                item.route = "company_review"
                item.route_reason = "company match requires review"
            elif item.patent.raw.get("technology_discovery") and not item.match.company_id:
                technology_eligible.append(item)
            elif not item.match.company_id:
                item.route = "unmatched"
                item.route_reason = "no monitored company match"
            else:
                eligible.append(item)

        ordered = sorted(
            eligible,
            key=lambda item: (-item.final_score, item.patent.identity),
        )
        top_rate = max(0.0, min(1.0, self.config.ranked_gpt_top_rate))
        top_count = math.ceil(len(ordered) * top_rate) if ordered and top_rate else 0
        top_ids = {item.patent.identity for item in ordered[:top_count]}
        rejected = [item for item in ordered if item.patent.identity not in top_ids]

        audit_rate = max(0.0, min(1.0, self.config.ranked_gpt_audit_rate))
        audit_count = math.ceil(len(rejected) * audit_rate) if rejected and audit_rate else 0
        audit_seed = hashlib.sha256(source.encode("utf-8")).hexdigest()
        audit_order = sorted(
            rejected,
            key=lambda item: hashlib.sha256(
                f"{audit_seed}|{item.patent.identity}".encode("utf-8")
            ).hexdigest(),
        )
        audit_ids = {item.patent.identity for item in audit_order[:audit_count]}

        for rank, item in enumerate(ordered, 1):
            if item.patent.identity in top_ids:
                item.route = "gpt_ranked"
                item.route_reason = f"algorithm rank {rank}/{len(ordered)}; top {top_rate:.1%}"
            elif item.patent.identity in audit_ids:
                item.route = "gpt_audit"
                item.route_reason = f"deterministic random audit from rejected pool {audit_rate:.1%}"
            else:
                item.route = "rejected"
                item.route_reason = f"below algorithm top {top_rate:.1%}"

        technology_ordered = sorted(
            technology_eligible,
            key=lambda item: (-item.final_score, item.patent.identity),
        )
        technology_rate = max(
            0.0, min(1.0, self.config.technology_discovery_gpt_top_rate)
        )
        technology_count = (
            math.ceil(len(technology_ordered) * technology_rate)
            if technology_ordered and technology_rate else 0
        )
        for rank, item in enumerate(technology_ordered, 1):
            if rank <= technology_count:
                item.route = "gpt_technology"
                item.route_reason = (
                    f"technology discovery rank {rank}/{len(technology_ordered)}; "
                    f"top {technology_rate:.1%}"
                )
            else:
                item.route = "technology_discovery"
                item.route_reason = (
                    f"technology discovery below GPT top {technology_rate:.1%}"
                )

    def _estimated_ai_call_ceiling_jpy(self, service: str) -> float:
        # Prompts are sliced to 12,000 source characters. 64k input tokens is a
        # deliberately conservative ceiling that also covers schema and framing.
        input_tokens = 64_000
        if service == "openai":
            input_rate = self.config.openai_input_usd_per_million
            output_rate = self.config.openai_output_usd_per_million
            output_tokens = 3_000
        else:
            input_rate = self.config.gemini_input_usd_per_million
            output_rate = self.config.gemini_output_usd_per_million
            output_tokens = 2_048
        return (
            input_tokens * input_rate + output_tokens * output_rate
        ) / 1_000_000 * self.config.ai_usd_jpy_rate

    def _budget_allows_call(self, run_id: str, service: str) -> bool:
        usage_year = datetime.now().strftime("%Y")
        spent = self.database.annual_ai_spend_jpy(usage_year)
        budget = max(0.0, self.config.annual_ai_budget_jpy)
        reserve = max(
            0.0,
            self.config.ai_budget_call_reserve_jpy,
            self._estimated_ai_call_ceiling_jpy(service),
        )
        if self.ai_budget_stopped or spent + reserve > budget:
            if not self.ai_budget_stopped:
                self.ai_budget_stopped = True
                self.ai_budget_stop_reason = (
                    f"annual AI budget guard: spent={spent:.4f} JPY, "
                    f"reserve={reserve:.2f} JPY, budget={budget:.2f} JPY"
                )
                self.database.record_ai_budget_event(
                    usage_year,
                    run_id,
                    service,
                    spent,
                    budget,
                    self.ai_budget_stop_reason,
                )
                self.progress(f"AI_BUDGET_STOP {self.ai_budget_stop_reason}", 1.0)
            return False
        return True

    def _record_ranked_ai_cost(
        self,
        run_id: str,
        service: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        if service == "openai":
            input_rate = self.config.openai_input_usd_per_million
            output_rate = self.config.openai_output_usd_per_million
        else:
            input_rate = self.config.gemini_input_usd_per_million
            output_rate = self.config.gemini_output_usd_per_million
        usage_year = datetime.now().strftime("%Y")
        return self.database.add_ai_cost(
            usage_year,
            service,
            model,
            run_id,
            input_tokens,
            output_tokens,
            input_rate,
            output_rate,
            self.config.ai_usd_jpy_rate,
        )

    def _select_gemini_candidates(self, items: list[ScoredPatent]) -> list[ScoredPatent]:
        reviewed = [item for item in items if item.gpt_result]
        rate = max(0.0, min(1.0, self.config.ranked_gemini_top_rate))
        count = math.ceil(len(reviewed) * rate) if reviewed and rate else 0
        return sorted(
            reviewed,
            key=lambda item: (-self._gpt_peak_score(item), -item.final_score, item.patent.identity),
        )[:count]

    @staticmethod
    def _ranked_gpt_candidates(items: list[ScoredPatent]) -> list[ScoredPatent]:
        """Spend a constrained daily quota on the strongest ranked candidates first."""
        return sorted(
            (item for item in items if item.route.startswith("gpt")),
            key=lambda item: (
                0 if item.route == "gpt_ranked" else 1,
                0 if item.route == "gpt_technology" else 1,
                -item.final_score,
                item.patent.identity,
            ),
        )

    @staticmethod
    def _ai_input_hash(item: ScoredPatent, company: Company | None) -> str:
        prompt = GeminiReviewer.build_prompt(item, company)
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _run_ranked_ai(self, records: list[PatentRecord], source: str) -> dict[str, Any]:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + hashlib.sha1(os.urandom(12)).hexdigest()[:6]
        started_at = datetime.now().isoformat(timespec="seconds")
        self.database.save_run(run_id, source, self.config, started_at)
        families = consolidate_families(records)
        families.sort(key=lambda pair: pair[0].publication_date or pair[0].filing_date or "")
        items: list[ScoredPatent] = []
        errors = 0
        interrupted = False
        global_score_history = self.database.prior_scores_global()
        company_score_histories = {
            company.company_id: self.database.prior_scores(company.company_id)
            for company in self.companies
        }
        total = max(1, len(families))

        for index, (patent, members) in enumerate(families, 1):
            if self.stop_requested():
                interrupted = True
                break
            self.progress(f"algorithm scoring {index}/{len(families)}: {patent.title[:45]}", (index - 1) / total)
            match = self.matcher.match(patent.applicants)
            company = self.company_by_id.get(match.company_id)
            tech, tech_reasons = technology_score(patent, company, self.config)
            meta, meta_reasons = metadata_score(patent)
            novelty, novelty_reasons = novelty_score(patent, match, self.database, self.config.history_lookback)
            materiality, materiality_reasons = materiality_score(patent, company, meta, novelty)
            final = clamp(
                tech * self.config.technology_weight
                + meta * self.config.metadata_weight
                + novelty * self.config.novelty_weight
                + materiality * self.config.materiality_weight
            )
            percentile_value, percentile_basis, percentile_samples = stable_percentile(
                final,
                company_score_histories.get(match.company_id, []),
                global_score_history,
                self.config.company_percentile_min_history,
                self.config.global_percentile_min_history,
            )
            items.append(
                ScoredPatent(
                    patent=patent,
                    match=match,
                    duplicate_count=len(members),
                    family_members=members,
                    technology_score=round(tech, 3),
                    technology_reasons=tech_reasons,
                    metadata_score=round(meta, 3),
                    metadata_reasons=meta_reasons,
                    novelty_score=round(novelty, 3),
                    novelty_reasons=novelty_reasons,
                    materiality_score=round(materiality, 3),
                    materiality_reasons=materiality_reasons,
                    final_score=round(final, 3),
                    company_percentile=round(percentile_value, 3),
                    percentile_basis=percentile_basis,
                    percentile_sample_count=percentile_samples,
                )
            )

        if not interrupted:
            self._assign_ranked_ai_routes(items, source)

        month = datetime.now().strftime("%Y-%m")
        usage_date = datetime.now().strftime("%Y-%m-%d")
        current_gpt_usage = self.database.usage("openai", month)
        current_daily_gpt_usage = self.database.ai_calls_on_date("openai", usage_date)
        gpt_count = 0
        gpt_cache_count = 0
        for item in self._ranked_gpt_candidates(items):
            if self.stop_requested():
                interrupted = True
                break
            company = self.company_by_id.get(item.match.company_id)
            input_hash = self._ai_input_hash(item, company)
            cached = self.database.cached_ai_review(
                "openai", item.patent.identity, self.config.gpt_model, input_hash
            )
            if cached is not None:
                item.gpt_result = cached
                item.route = "gpt_cached"
                gpt_cache_count += 1
            elif current_daily_gpt_usage + gpt_count >= self.config.daily_gpt_limit:
                item.route = "gpt_daily_limit"
                item.route_reason += " / daily GPT call limit"
            elif current_gpt_usage + gpt_count >= self.config.monthly_gpt_limit:
                item.route = "gpt_limit"
                item.route_reason += " / monthly GPT call limit"
            elif not self._budget_allows_call(run_id, "openai"):
                item.route = "ai_budget_stopped"
                item.route_reason += f" / {self.ai_budget_stop_reason}"
            elif not self.gpt:
                item.route = "gpt_pending"
                item.route_reason += " / OpenAI API key missing"
            else:
                try:
                    result, input_tokens, output_tokens = self.gpt.review(item, company)
                    item.gpt_result = result
                    self.database.store_ai_review(
                        "openai", item.patent.identity, self.gpt.model, input_hash, result
                    )
                    self.database.add_usage("openai", input_tokens, output_tokens)
                    self._record_ranked_ai_cost(
                        run_id, "openai", self.gpt.model, input_tokens, output_tokens
                    )
                    gpt_count += 1
                    item.route = "gpt_reviewed"
                    self._update_adaptive_escalation(result)
                except Exception as exc:
                    item.error = f"gpt:{type(exc).__name__}:{exc}"
                    item.route = "gpt_error"
                    errors += 1

        gpt_reviewed = [item for item in items if item.gpt_result]
        gemini_rate = max(0.0, min(1.0, self.config.ranked_gemini_top_rate))
        gemini_ranked = self._select_gemini_candidates(items)
        for rank, item in enumerate(gemini_ranked, 1):
            item.route = "gemini"
            item.route_reason += f" / GPT rank {rank}/{len(gpt_reviewed)}; top {gemini_rate:.1%}"

        current_gemini_usage = self.database.usage("gemini", month)
        current_daily_gemini_usage = self.database.ai_calls_on_date("gemini", usage_date)
        gemini_count = 0
        gemini_cache_count = 0
        for item in gemini_ranked:
            if self.stop_requested():
                interrupted = True
                break
            company = self.company_by_id.get(item.match.company_id)
            input_hash = self._ai_input_hash(item, company)
            cache_model = self.config.gemini_model
            cached = self.database.cached_ai_review(
                "gemini", item.patent.identity, cache_model, input_hash
            )
            if cached is not None:
                item.gemini_result = cached
                item.route = "gemini_cached"
                gemini_cache_count += 1
            elif current_daily_gemini_usage + gemini_count >= self.config.daily_gemini_limit:
                item.route = "gemini_daily_limit"
                item.route_reason += " / daily Gemini call limit"
            elif current_gemini_usage + gemini_count >= self.config.monthly_gemini_limit:
                item.route = "gemini_limit"
                item.route_reason += " / monthly Gemini call limit"
            elif not self._budget_allows_call(run_id, "gemini"):
                item.route = "ai_budget_stopped"
                item.route_reason += f" / {self.ai_budget_stop_reason}"
            elif not self.gemini:
                item.route = "gemini_pending"
                item.route_reason += " / Gemini API key missing"
            else:
                try:
                    result, input_tokens, output_tokens = self.gemini.review(item, company)
                    item.gemini_result = result
                    self.database.add_usage("gemini", input_tokens, output_tokens)
                    model = self.gemini.resolved_model or self.config.gemini_model
                    self.database.store_ai_review(
                        "gemini", item.patent.identity, cache_model, input_hash, result
                    )
                    self._record_ranked_ai_cost(
                        run_id, "gemini", model, input_tokens, output_tokens
                    )
                    gemini_count += 1
                    item.route = "gemini_reviewed"
                except Exception as exc:
                    item.error = f"gemini:{type(exc).__name__}:{exc}"
                    item.route = "gemini_error"
                    errors += 1

        saved_items: list[ScoredPatent] = []
        for item in items:
            if self.stop_requested() and interrupted:
                break
            company = self.company_by_id.get(item.match.company_id)
            if self.learning_model:
                from .market_feedback import predict
                gpt = item.gpt_result or {}
                gemini = item.gemini_result or {}
                values = [
                    item.technology_score,
                    item.metadata_score,
                    item.novelty_score,
                    item.materiality_score,
                    item.final_score,
                    float(gpt.get("importance_score", 0) or 0),
                    float(gpt.get("short_term_market_impact_score", 0) or 0),
                    float(gpt.get("long_term_business_value_score", 0) or 0),
                    float(gpt.get("novelty_score", 0) or 0),
                    float(gemini.get("importance_score", 0) or 0),
                    float(gemini.get("short_term_market_impact_score", 0) or 0),
                    float(gemini.get("long_term_business_value_score", 0) or 0),
                    float(gemini.get("novelty_score", 0) or 0),
                    math.log10(max(1.0, company.market_cap_jpy if company else 0.0)),
                ]
                item.learned_upside_probability = round(predict(self.learning_model, values), 6)
                item.learned_model_samples = int(self.learning_model.get("sample_count", 0))
                item.learned_model_created_at = str(self.learning_model.get("created_at", ""))
            used_gpt_model = self.gpt.model if self.gpt else self.config.gpt_model
            used_gemini_model = (
                self.gemini.resolved_model
                if self.gemini and self.gemini.resolved_model
                else self.config.gemini_model
            )
            self.database.save_scored(run_id, item, used_gpt_model, used_gemini_model)
            if self.item_scored:
                self.item_scored(run_id, item)
            saved_items.append(item)

        self._finalize_adaptive_escalation()
        self.database.finish_run(run_id, len(records), len(families), gpt_count, gemini_count, errors)
        output = self._export(run_id, saved_items)
        annual_spend = self.database.annual_ai_spend_jpy(datetime.now().strftime("%Y"))
        self.progress("完了", 1.0)
        self.database.close()
        return {
            "run_id": run_id,
            "input_count": len(records),
            "family_count": len(families),
            "scored_count": len(saved_items),
            "gpt_count": gpt_count,
            "gpt_cache_count": gpt_cache_count,
            "gemini_count": gemini_count,
            "gemini_cache_count": gemini_cache_count,
            "review_count": sum(item.match.review_required for item in saved_items),
            "unmatched_count": sum(not item.match.company_id for item in saved_items),
            "error_count": errors,
            "gpt_model": self.config.gpt_model,
            "gemini_requested_model": self.config.gemini_model,
            "gemini_resolved_model": self.gemini.resolved_model if self.gemini else "",
            "ranked_ai_enabled": True,
            "ranked_gpt_top_rate": self.config.ranked_gpt_top_rate,
            "ranked_gpt_audit_rate": self.config.ranked_gpt_audit_rate,
            "ranked_gemini_top_rate": self.config.ranked_gemini_top_rate,
            "annual_ai_spend_jpy": round(annual_spend, 6),
            "annual_ai_budget_jpy": self.config.annual_ai_budget_jpy,
            "ai_budget_stopped": self.ai_budget_stopped,
            "ai_budget_stop_reason": self.ai_budget_stop_reason,
            "interrupted": interrupted,
            **output,
        }

    def run(self, records: list[PatentRecord], source: str = "csv") -> dict[str, Any]:
        if self.config.ranked_ai_enabled:
            return self._run_ranked_ai(records, source)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + hashlib.sha1(os.urandom(12)).hexdigest()[:6]
        started_at = datetime.now().isoformat(timespec="seconds")
        self.database.save_run(run_id, source, self.config, started_at)
        families = consolidate_families(records)
        families.sort(key=lambda pair: pair[0].publication_date or pair[0].filing_date or "")
        scored: list[ScoredPatent] = []
        gpt_count = 0
        gemini_count = 0
        errors = 0
        month = datetime.now().strftime("%Y-%m")
        current_gpt_usage = self.database.usage("openai", month)
        current_gemini_usage = self.database.usage("gemini", month)
        # Freeze percentile baselines before this run.  Otherwise processing order
        # changes percentiles because earlier rows in the same batch are saved first.
        global_score_history = self.database.prior_scores_global()
        company_score_histories = {
            company.company_id: self.database.prior_scores(company.company_id)
            for company in self.companies
        }
        total = max(1, len(families))
        for index, (patent, members) in enumerate(families, 1):
            if self.stop_requested():
                break
            self.progress(f"事前評価 {index}/{len(families)}: {patent.title[:45]}", (index - 1) / total)
            match = self.matcher.match(patent.applicants)
            company = self.company_by_id.get(match.company_id)
            tech, tech_reasons = technology_score(patent, company, self.config)
            meta, meta_reasons = metadata_score(patent)
            novelty, novelty_reasons = novelty_score(patent, match, self.database, self.config.history_lookback)
            materiality, materiality_reasons = materiality_score(patent, company, meta, novelty)
            final = clamp(
                tech * self.config.technology_weight
                + meta * self.config.metadata_weight
                + novelty * self.config.novelty_weight
                + materiality * self.config.materiality_weight
            )
            percentile_value, percentile_basis, percentile_samples = stable_percentile(
                final,
                company_score_histories.get(match.company_id, []),
                global_score_history,
                self.config.company_percentile_min_history,
                self.config.global_percentile_min_history,
            )
            item = ScoredPatent(
                patent=patent,
                match=match,
                duplicate_count=len(members),
                family_members=members,
                technology_score=round(tech, 3),
                technology_reasons=tech_reasons,
                metadata_score=round(meta, 3),
                metadata_reasons=meta_reasons,
                novelty_score=round(novelty, 3),
                novelty_reasons=novelty_reasons,
                materiality_score=round(materiality, 3),
                materiality_reasons=materiality_reasons,
                final_score=round(final, 3),
                company_percentile=round(percentile_value, 3),
                percentile_basis=percentile_basis,
                percentile_sample_count=percentile_samples,
            )
            self._route(item)
            if item.route.startswith("gpt"):
                if current_gpt_usage + gpt_count >= self.config.monthly_gpt_limit:
                    item.route = "gpt_limit"
                    item.route_reason = "月間GPT上限到達"
                elif not self.gpt:
                    item.route = "gpt_pending"
                    item.route_reason += " / OpenAI APIキー未設定"
                else:
                    try:
                        self.progress(f"GPT-5 nano一次審査 {gpt_count + 1}件目: {patent.title[:45]}", (index - 0.65) / total)
                        result, input_chars, output_chars = self.gpt.review(item, company)
                        item.gpt_result = result
                        self.database.add_usage("openai", input_chars, output_chars)
                        gpt_count += 1
                        escalate, reason = self._should_escalate_to_gemini(result)
                        self._update_adaptive_escalation(result)
                        if escalate:
                            item.route = "gemini"
                            item.route_reason += f" / {reason}"
                        else:
                            item.route = "gpt_reviewed"
                            item.route_reason += f" / {reason}"
                    except Exception as exc:  # keep the batch alive and retain the cause
                        item.error = f"gpt:{type(exc).__name__}:{exc}"
                        item.route = "gpt_error"
                        errors += 1
            if item.route == "gemini":
                if current_gemini_usage + gemini_count >= self.config.monthly_gemini_limit:
                    item.route = "gemini_limit"
                    item.route_reason += " / 月間Gemini上限到達"
                elif not self.gemini:
                    item.route = "gemini_pending"
                    item.route_reason += " / Gemini APIキー未設定"
                else:
                    try:
                        self.progress(f"Gemini再審査 {gemini_count + 1}件目: {patent.title[:45]}", (index - 0.35) / total)
                        result, input_chars, output_chars = self.gemini.review(item, company)
                        item.gemini_result = result
                        self.database.add_usage("gemini", input_chars, output_chars)
                        gemini_count += 1
                        item.route = "gemini_reviewed"
                    except Exception as exc:  # keep the batch alive and retain the cause
                        item.error = f"gemini:{type(exc).__name__}:{exc}"
                        item.route = "gemini_error"
                        errors += 1
            if self.learning_model:
                from .market_feedback import predict
                gpt = item.gpt_result or {}
                gemini = item.gemini_result or {}
                values = [
                    item.technology_score, item.metadata_score, item.novelty_score,
                    item.materiality_score, item.final_score,
                    float(gpt.get("importance_score", 0) or 0),
                    float(gpt.get("short_term_market_impact_score", 0) or 0),
                    float(gpt.get("long_term_business_value_score", 0) or 0),
                    float(gpt.get("novelty_score", 0) or 0),
                    float(gemini.get("importance_score", 0) or 0),
                    float(gemini.get("short_term_market_impact_score", 0) or 0),
                    float(gemini.get("long_term_business_value_score", 0) or 0),
                    float(gemini.get("novelty_score", 0) or 0),
                    math.log10(max(1.0, company.market_cap_jpy if company else 0.0)),
                ]
                item.learned_upside_probability = round(predict(self.learning_model, values), 6)
                item.learned_model_samples = int(self.learning_model.get("sample_count", 0))
                item.learned_model_created_at = str(self.learning_model.get("created_at", ""))
            used_gpt_model = self.gpt.model if self.gpt else self.config.gpt_model
            used_gemini_model = self.gemini.resolved_model if self.gemini and self.gemini.resolved_model else self.config.gemini_model
            self.database.save_scored(run_id, item, used_gpt_model, used_gemini_model)
            if self.item_scored:
                try:
                    self.item_scored(run_id, item)
                except Exception as exc:
                    self.progress(
                        f"即時通知処理警告: {type(exc).__name__}: {exc}",
                        index / total,
                    )
            scored.append(item)
        self._finalize_adaptive_escalation()
        self.database.finish_run(run_id, len(records), len(families), gpt_count, gemini_count, errors)
        output = self._export(run_id, scored)
        self.database.close()
        self.progress("完了", 1.0)
        return {
            "run_id": run_id,
            "input_count": len(records),
            "family_count": len(families),
            "scored_count": len(scored),
            "gpt_count": gpt_count,
            "gemini_count": gemini_count,
            "review_count": sum(item.match.review_required for item in scored),
            "unmatched_count": sum(not item.match.company_id for item in scored),
            "error_count": errors,
            "gpt_model": self.config.gpt_model,
            "gemini_requested_model": self.config.gemini_model,
            "gemini_resolved_model": self.gemini.resolved_model if self.gemini else "",
            "adaptive_escalation_enabled": self.config.adaptive_gemini_escalation,
            "escalation_threshold_initial": self.initial_escalation_threshold,
            "escalation_threshold_final": self.config.gemini_escalation_threshold,
            "actual_escalation_rate": (gemini_count / gpt_count) if gpt_count else 0.0,
            "adaptive_score_samples": len(self.adaptive_scores),
            **output,
        }

    def _export(self, run_id: str, items: list[ScoredPatent]) -> dict[str, str]:
        run_dir = self.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        columns = [
            "run_id", "publication_number", "application_number", "family_key", "family_id",
            "duplicate_count", "publication_date", "filing_date", "priority_date", "priority_numbers",
            "family_members", "family_size", "country_codes", "claim_count", "citation_count",
            "citations", "cited_by", "non_patent_citations", "legal_status", "legal_events_json",
            "ownership_events_json", "retrieved_at", "source_endpoint", "query_window_start",
            "query_window_end", "payload_hash", "parser_version", "title",
            "applicants", "company_id", "company_name", "match_method", "match_confidence", "review_required",
            "cpc_codes", "ipc_codes", "technology_score", "metadata_score", "novelty_score",
            "materiality_score", "final_score", "company_percentile", "percentile_basis",
            "percentile_sample_count", "epo_detail_enriched", "epo_detail_warnings", "epo_detail_errors", "route", "route_reason",
            "technology_discovery", "technology_categories",
            "candidate_reasons_json", "reject_reasons_json",
            "gpt_model", "gpt_decision", "gpt_importance", "gpt_short_term_market_impact",
            "gpt_long_term_business_value", "gpt_materiality", "gpt_novelty",
            "gpt_market_impact_reason", "gpt_expected_time_horizon", "gpt_summary",
            "gemini_model", "gemini_decision", "gemini_importance", "gemini_short_term_market_impact",
            "gemini_long_term_business_value", "gemini_materiality", "gemini_novelty",
            "gemini_market_impact_reason", "gemini_expected_time_horizon", "gemini_summary",
            "learned_upside_probability", "learned_model_samples", "learned_model_created_at",
            "source_url", "error",
        ]
        rows = []
        for item in sorted(items, key=lambda value: value.final_score, reverse=True):
            gpt = item.gpt_result or {}
            gemini = item.gemini_result or {}
            rows.append({
                "run_id": run_id,
                "publication_number": item.patent.publication_number,
                "application_number": item.patent.application_number,
                "family_key": item.patent.family_key,
                "family_id": item.patent.family_id,
                "duplicate_count": item.duplicate_count,
                "publication_date": item.patent.publication_date,
                "filing_date": item.patent.filing_date,
                "priority_date": item.patent.priority_date,
                "priority_numbers": " | ".join(item.patent.priority_numbers),
                "family_members": " | ".join(item.patent.family_members),
                "family_size": item.patent.family_size,
                "country_codes": " | ".join(item.patent.country_codes),
                "claim_count": item.patent.claim_count,
                "citation_count": item.patent.citation_count,
                "citations": " | ".join(item.patent.citations),
                "cited_by": " | ".join(item.patent.cited_by),
                "non_patent_citations": " | ".join(item.patent.non_patent_citations),
                "legal_status": item.patent.legal_status,
                "legal_events_json": json.dumps(item.patent.legal_events, ensure_ascii=False),
                "ownership_events_json": json.dumps(item.patent.ownership_events, ensure_ascii=False),
                "retrieved_at": item.patent.retrieved_at,
                "source_endpoint": item.patent.source_endpoint,
                "query_window_start": item.patent.query_window_start,
                "query_window_end": item.patent.query_window_end,
                "payload_hash": item.patent.payload_hash,
                "parser_version": item.patent.parser_version,
                "title": item.patent.title,
                "applicants": " | ".join(item.patent.applicants),
                "company_id": item.match.company_id,
                "company_name": item.match.company_name,
                "match_method": item.match.method,
                "match_confidence": item.match.confidence,
                "review_required": int(item.match.review_required),
                "cpc_codes": " | ".join(item.patent.cpc_codes),
                "ipc_codes": " | ".join(item.patent.ipc_codes),
                "technology_score": item.technology_score,
                "metadata_score": item.metadata_score,
                "novelty_score": item.novelty_score,
                "materiality_score": item.materiality_score,
                "final_score": item.final_score,
                "company_percentile": item.company_percentile,
                "percentile_basis": item.percentile_basis,
                "percentile_sample_count": item.percentile_sample_count,
                "epo_detail_enriched": int(item.patent.detail_enriched),
                "epo_detail_warnings": " | ".join(item.patent.raw.get("epo_detail_warnings", [])),
                "epo_detail_errors": " | ".join(item.patent.raw.get("epo_detail_errors", [])),
                "route": item.route,
                "route_reason": item.route_reason,
                "technology_discovery": int(bool(item.patent.raw.get("technology_discovery"))),
                "technology_categories": " | ".join(
                    item.patent.raw.get("technology_categories", [])
                ),
                "candidate_reasons_json": json.dumps({
                    "technology": item.technology_reasons,
                    "metadata": item.metadata_reasons,
                    "novelty": item.novelty_reasons,
                    "materiality": item.materiality_reasons,
                    "percentile_basis": item.percentile_basis,
                    "percentile_sample_count": item.percentile_sample_count,
                }, ensure_ascii=False),
                "reject_reasons_json": json.dumps({
                    "route": item.route,
                    "route_reason": item.route_reason,
                    "score": item.final_score,
                    "threshold": self.config.gemini_threshold,
                    "matched_company": bool(item.match.company_id),
                    "review_required": item.match.review_required,
                }, ensure_ascii=False) if item.route in {"rejected", "unmatched"} else "",
                "gpt_model": self.gpt.model if self.gpt else "",
                "gpt_decision": gpt.get("decision", ""),
                "gpt_importance": gpt.get("importance_score", ""),
                "gpt_short_term_market_impact": gpt.get("short_term_market_impact_score", ""),
                "gpt_long_term_business_value": gpt.get("long_term_business_value_score", ""),
                "gpt_materiality": gpt.get("materiality_score", ""),
                "gpt_novelty": gpt.get("novelty_score", ""),
                "gpt_market_impact_reason": gpt.get("market_impact_reason", ""),
                "gpt_expected_time_horizon": gpt.get("expected_time_horizon", ""),
                "gpt_summary": gpt.get("email_summary", ""),
                "gemini_model": self.gemini.resolved_model if self.gemini else "",
                "gemini_decision": gemini.get("decision", ""),
                "gemini_importance": gemini.get("importance_score", ""),
                "gemini_short_term_market_impact": gemini.get("short_term_market_impact_score", ""),
                "gemini_long_term_business_value": gemini.get("long_term_business_value_score", ""),
                "gemini_materiality": gemini.get("materiality_score", ""),
                "gemini_novelty": gemini.get("novelty_score", ""),
                "gemini_market_impact_reason": gemini.get("market_impact_reason", ""),
                "gemini_expected_time_horizon": gemini.get("expected_time_horizon", ""),
                "gemini_summary": gemini.get("email_summary", ""),
                "learned_upside_probability": item.learned_upside_probability if item.learned_model_samples else "",
                "learned_model_samples": item.learned_model_samples if item.learned_model_samples else "",
                "learned_model_created_at": item.learned_model_created_at,
                "source_url": item.patent.source_url,
                "error": item.error,
            })
        all_path = run_dir / "patent_evaluations.csv"
        with all_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        gpt_path = run_dir / "gpt_reviewed_or_pending.csv"
        with gpt_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(
                row for row in rows
                if row["gpt_decision"] or str(row["route"]).startswith(("gpt", "gemini"))
            )
        important_path = run_dir / "gemini_reviewed_or_pending.csv"
        with important_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(row for row in rows if str(row["route"]).startswith("gemini"))
        review_path = run_dir / "company_match_review.csv"
        with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(row for row in rows if row["review_required"] or not row["company_id"])
        summary_path = run_dir / "run_summary.json"
        summary_path.write_text(
            json.dumps({
                "run_id": run_id,
                "app_version": APP_VERSION,
                "prompt_version": PROMPT_VERSION,
                "gpt_model": self.config.gpt_model,
                "gemini_requested_model": self.config.gemini_model,
                "gemini_resolved_model": self.gemini.resolved_model if self.gemini else "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "counts": dict(Counter(item.route for item in items)),
                "route_reason_counts": dict(Counter(item.route_reason for item in items)),
                "adaptive_escalation": {
                    "enabled": self.config.adaptive_gemini_escalation,
                    "target_rate": self.config.gemini_target_escalation_rate,
                    "threshold_initial": self.initial_escalation_threshold,
                    "threshold_next_run": self.config.gemini_escalation_threshold,
                    "score_sample_count": len(self.adaptive_scores),
                    "gpt_reviewed_count": sum(item.gpt_result is not None for item in items),
                    "gemini_reviewed_count": sum(item.gemini_result is not None for item in items),
                    "actual_rate": (
                        sum(item.gemini_result is not None for item in items)
                        / sum(item.gpt_result is not None for item in items)
                    ) if any(item.gpt_result is not None for item in items) else 0.0,
                },
                "broad_technology_candidate_count": sum(
                    item.technology_score >= self.config.broad_technology_gemini_threshold
                    for item in items
                ),
                "data_quality": {
                    "detail_enriched_count": sum(item.patent.detail_enriched for item in items),
                    "detail_warning_count": sum(bool(item.patent.raw.get("epo_detail_warnings")) for item in items),
                    "detail_error_count": sum(bool(item.patent.raw.get("epo_detail_errors")) for item in items),
                    "classification_present_count": sum(bool(item.patent.cpc_codes or item.patent.ipc_codes) for item in items),
                    "abstract_present_count": sum(bool(item.patent.abstract) for item in items),
                    "matched_company_count": sum(bool(item.match.company_id) for item in items),
                    "neutral_percentile_count": sum(item.percentile_basis == "neutral_insufficient_history" for item in items),
                },
                "score_mean": statistics.fmean(item.final_score for item in items) if items else 0,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "evaluation_csv": str(all_path.resolve()),
            "gpt_csv": str(gpt_path.resolve()),
            "gemini_csv": str(important_path.resolve()),
            "match_review_csv": str(review_path.resolve()),
            "summary_json": str(summary_path.resolve()),
        }


def mock_patents() -> list[PatentRecord]:
    """Return a deterministic, API-free fixture covering the production pipeline."""
    return [
        PatentRecord(
            source="mock", publication_number="JP2026900001A", family_id="MOCK-FAM-SEMICONDUCTOR",
            title="次世代パワー半導体の低損失ゲート構造",
            abstract="炭化ケイ素半導体におけるオン抵抗と製造ばらつきを低減するゲート構造。",
            claims="基板、絶縁膜および電極を含む半導体製造装置。", applicants=["東京エレクトロン株式会社"],
            inventors=["検証太郎"], cpc_codes=["H10D30/60"], ipc_codes=["H01L29/78"],
            publication_date="2026-07-10", priority_date="2025-01-10", country_codes=["JP", "US", "EP"],
            citation_count=12, family_size=3, claim_count=20, legal_status="granted",
            source_url="https://example.invalid/mock/JP2026900001A",
        ),
        PatentRecord(
            source="mock", publication_number="US2026900001A1", family_id="MOCK-FAM-SEMICONDUCTOR",
            title="Low-loss gate structure for power semiconductors",
            abstract="US family member of the silicon carbide semiconductor manufacturing invention.",
            applicants=["TOKYO ELECTRON LIMITED"], cpc_codes=["H10D30/60"],
            publication_date="2026-07-11", priority_date="2025-01-10", country_codes=["US"], family_size=3,
            source_url="https://example.invalid/mock/US2026900001A1",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900002A", family_id="MOCK-FAM-BATTERY",
            title="全固体電池用多孔質複合電解質",
            abstract="高出力な全固体電池の界面抵抗を低減する先端材料。",
            claims="無機電解質と高分子支持体を含む複合電解質。", applicants=["TORAY INDUSTRIES INC"],
            cpc_codes=["H01M10/0562", "Y02E60/10"], ipc_codes=["H01M10/0562"],
            publication_date="2026-07-12", country_codes=["JP", "US", "EP", "CN"],
            citation_count=6, family_size=6, claim_count=24, legal_status="published",
            source_url="https://example.invalid/mock/JP2026900002A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900003A", family_id="MOCK-FAM-MOBILITY",
            title="自動運転車両の協調制御装置", abstract="機械学習により複数車両の走行軌道を協調制御する。",
            claims="車載センサと学習器を備える自動運転制御装置。", applicants=["TOYOTA MOTOR CORPORATION"],
            cpc_codes=["B60L3/00", "G06N20/00"], ipc_codes=["B60L3/00"], publication_date="2026-07-13",
            country_codes=["JP", "US"], citation_count=4, family_size=4, claim_count=18, legal_status="published",
            source_url="https://example.invalid/mock/JP2026900003A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900004A", family_id="MOCK-FAM-AI",
            title="人工知能による産業ロボットの自律動作計画",
            abstract="機械学習とAI・ソフトウェアにより工場設備を点検するロボット。",
            claims="画像センサ、学習器および動作計画部を備える点検ロボット。",
            applicants=["株式会社ＰＫＳＨＡ Ｔｅｃｈｎｏｌｏｇｙ"],
            cpc_codes=["G06N10/00", "G06N20/00"], ipc_codes=["G06N10/00"],
            publication_date="2026-07-13", country_codes=["JP", "US", "EP", "CN", "KR"],
            citation_count=20, family_size=8, claim_count=30, legal_status="granted",
            source_url="https://example.invalid/mock/JP2026900004A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900005A", family_id="MOCK-FAM-QUANTUM",
            title="量子誤り訂正用の光集積回路", abstract="光量子ビットの誤りを低消費電力で訂正する集積回路。",
            claims="光導波路と量子状態測定器を含む光集積回路。", applicants=["SONY GROUP CORPORATION"],
            cpc_codes=["G06N10/40", "H04B10/70"], publication_date="2026-07-14",
            country_codes=["JP", "US", "EP", "CN"], citation_count=9, family_size=7, claim_count=28,
            legal_status="granted", source_url="https://example.invalid/mock/JP2026900005A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900006A", family_id="MOCK-FAM-EXCLUDED",
            title="社員食堂予約システム", abstract="予約情報と広告配信に基づいて食数を管理する。",
            claims="予約受付部を備える情報処理装置。", applicants=["富士通株式会社"],
            cpc_codes=["G06Q10/02"], publication_date="2026-07-14", country_codes=["JP"], claim_count=5,
            legal_status="published", source_url="https://example.invalid/mock/JP2026900006A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900007A", family_id="MOCK-FAM-UNMATCHED",
            title="核融合炉用高温超電導磁石", abstract="核融合プラズマ閉じ込め用の磁場を発生させる。",
            claims="超電導コイルを含む核融合装置。", applicants=["未上場未来エネルギー研究所"],
            cpc_codes=["G21B1/05", "Y02E30/10"], publication_date="2026-07-14", country_codes=["JP"],
            citation_count=1, family_size=2, claim_count=12, legal_status="published",
            source_url="https://example.invalid/mock/JP2026900007A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900008A", family_id="MOCK-FAM-GENERIC",
            title="画像形成装置の搬送ローラ", abstract="紙詰まりを低減する搬送ローラの支持構造。",
            claims="ローラと軸受を備える画像形成装置。", applicants=["CANON INC"],
            cpc_codes=["G03G15/00"], publication_date="2026-07-14", country_codes=["JP"],
            family_size=1, claim_count=7, legal_status="published",
            source_url="https://example.invalid/mock/JP2026900008A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900009A", family_id="MOCK-FAM-TESTER",
            title="AIによる半導体試験条件最適化", abstract="機械学習により半導体試験の条件を自動最適化する。",
            claims="試験結果を学習する推論器を備える半導体試験装置。", applicants=["ADVANTEST CORPORATION"],
            cpc_codes=["H10D84/00", "G06N20/00"], publication_date="2026-07-14",
            country_codes=["JP", "US", "EP"], citation_count=5, family_size=4, claim_count=19,
            legal_status="granted", source_url="https://example.invalid/mock/JP2026900009A",
        ),
        PatentRecord(
            source="mock", publication_number="JP2026900010A", family_id="MOCK-FAM-SEMICONDUCTOR-2",
            title="パワー半導体製造装置のゲート形成方法",
            abstract="炭化ケイ素半導体のゲート形成工程を安定化する製造方法。",
            claims="成膜部と熱処理部を備える半導体製造装置。", applicants=["TOKYO ELECTRON LIMITED"],
            cpc_codes=["H10D30/60"], ipc_codes=["H01L21/00"], publication_date="2026-07-15",
            country_codes=["JP", "US"], citation_count=1, family_size=2, claim_count=12,
            legal_status="published", source_url="https://example.invalid/mock/JP2026900010A",
        ),
        PatentRecord(
            source="mock_historical", publication_number="JP7301490B2", application_number="JP2020207080",
            family_id="HISTORICAL-NITTO-SEIKO-7301490", title="生分解性医療器具",
            abstract=(
                "純度99.95%以上の高純度マグネシウムを用い、骨折治療中は必要な強度を保ち、"
                "骨接合後には生体内で緩やかに溶解・吸収される期間を制御する医療用インプラント材料。"
                "骨接合後の抜去手術を不要にし、患者負担、入院期間および医療資源の削減を目指す。"
            ),
            applicants=["日東精工株式会社", "京都府公立大学法人", "国立大学法人富山大学"],
            ipc_codes=["A61L27/04", "A61L27/50", "A61L27/58", "A61L31/02", "A61L31/14"],
            publication_date="2023-07-03", priority_date="2020-12-14", country_codes=["JP"],
            family_size=1, legal_status="granted",
            source_url="https://www.nittoseiko.co.jp/news/news_2023/magnesium.html",
            raw={
                "validation_case": "stock_price_impact",
                "known_market_reaction_hidden_from_gemini": "next_business_day_plus_11.6_percent",
                "source_note": "Company release and independent patent-market event study",
            },
        ),
    ]
