from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .collector import stable_id
from .database import NewsDatabase
from .global_news import prune_global_news, rank_toc_records
from .ngram_collector import (
    DEFAULT_ROOT,
    NgramCollectorError,
    discover_files,
    load_toc,
)


STATE_KEY = "emergency_next_scan_at"
DEFAULT_CHOKEPOINTS = {
    "strait_of_hormuz": ["strait of hormuz", "hormuz strait", "ホルムズ海峡"],
    "bab_el_mandeb": ["bab el mandeb", "bab al mandab", "バブエルマンデブ海峡"],
    "strait_of_malacca": ["strait of malacca", "malacca strait", "マラッカ海峡"],
    "taiwan_strait": ["taiwan strait", "台湾海峡"],
    "bosporus": ["bosporus", "bosphorus", "ボスポラス海峡"],
    "dardanelles": ["dardanelles", "ダーダネルス海峡"],
    "suez_canal": ["suez canal", "スエズ運河"],
    "panama_canal": ["panama canal", "パナマ運河"],
    "strait_of_gibraltar": ["strait of gibraltar", "ジブラルタル海峡"],
}
DISPLAY_NAMES = {
    "strait_of_hormuz": "ホルムズ海峡",
    "bab_el_mandeb": "バブ・エル・マンデブ海峡",
    "strait_of_malacca": "マラッカ海峡",
    "taiwan_strait": "台湾海峡",
    "bosporus": "ボスポラス海峡",
    "dardanelles": "ダーダネルス海峡",
    "suez_canal": "スエズ運河",
    "panama_canal": "パナマ運河",
    "strait_of_gibraltar": "ジブラルタル海峡",
    "global": "国家間",
}
HYPOTHETICAL = re.compile(
    r"\b(?:may|might|could|would|threatens?|warns?|considers?|plans?|urges?|if|"
    r"possible|possibly|proposal|proposed|talks?|discuss(?:es|ed|ion)?|seeks?|"
    r"rumou?rs?|unconfirmed|reportedly)\b|"
    r"(?:可能性|恐れ|警告|検討|計画|示唆)"
)
CLOSED = re.compile(
    r"\b(?:closed|closes|closure|shut|shuts|shutdown|blocked|blockade|sealed)\b|"
    r"\b(?:shipping|navigation|traffic|transit|passage)\s+(?:halted|stopped|suspended|blocked)\b|"
    r"(?:閉鎖|封鎖|航行停止|通航停止)"
)
REOPENED = re.compile(
    r"\b(?:reopened|reopens|unblocked)\b|"
    r"\b(?:shipping|navigation|traffic|transit|passage)\s+(?:resumes|resumed|restored)\b|"
    r"\bblockade\s+(?:lifted|ended)\b|(?:再開|封鎖解除|閉鎖解除|航行再開|通航再開)"
)
WAR_DECLARED = re.compile(
    r"\b(?:formally\s+)?declares?\s+war\s+(?:on|against)\b|"
    r"\bformal\s+declaration\s+of\s+war\b|"
    r"\bstate\s+of\s+war\s+(?:is\s+)?declared\b|"
    r"(?:宣戦布告|戦争状態を宣言)"
)
HEAD_OF_STATE_FALSE_CONTEXT = re.compile(
    r"\b(?:former|ex|vice|deput|acting|interim|president[- ]elect|prime minister[- ]designate|"
    r"candidate|opposition)\s+(?:president|prime minister|king|queen|emir|supreme leader)\b|"
    r"\b(?:president|prime minister|king|queen|emir|supreme leader|head of state)(?:'s| s)?\b"
    r".{0,30}\b(?:wife|husband|son|daughter|aide|minister|opponent|protester|journalist)\b"
    r".{0,30}\b(?:dies|died|dead|killed|assassinated|arrested|detained|captured)\b"
)

EXTENDED_EVENT_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    (
        "nuclear_weapon_event", "used_or_tested", "核兵器使用・核実験",
        re.compile(
            r"\b(?:uses?|used|detonates?|detonated|conducts?|conducted|carries? out|carried out)\b"
            r".{0,45}\b(?:nuclear weapon|nuclear bomb|nuclear warhead|nuclear device|nuclear test)\b|"
            r"\b(?:nuclear weapon|nuclear bomb|nuclear warhead|nuclear device|nuclear test)\b"
            r".{0,45}\b(?:used|detonated|conducted|confirmed)\b"
        ),
    ),
    (
        "nuclear_facility_attack", "attacked", "原子力施設への重大攻撃",
        re.compile(
            r"\b(?:nuclear power plant|nuclear plant|nuclear facility|reactor)\b.{0,55}"
            r"\b(?:attacked|struck|hit|bombed|shelled|severely damaged)\b|"
            r"\b(?:attacks?|strikes?|bombs?|shells?)\b.{0,55}"
            r"\b(?:nuclear power plant|nuclear plant|nuclear facility|reactor)\b"
        ),
    ),
    (
        "head_of_state", "dead_or_detained", "国家元首の死亡・拘束",
        re.compile(
            r"\b(?:president|prime minister|king|queen|emir|supreme leader|head of state)\b"
            r".{0,55}\b(?:dies|died|dead|killed|assassinated|arrested|detained|captured)\b|"
            r"\b(?:death|assassination|arrest|detention|capture)\b.{0,55}"
            r"\b(?:president|prime minister|king|queen|emir|supreme leader|head of state)\b"
        ),
    ),
    (
        "coup", "succeeded", "クーデター成功",
        re.compile(
            r"\b(?:coup|military)\b.{0,60}\b(?:seizes|seized|takes|took|assumes|assumed) power\b|"
            r"\bgovernment\b.{0,40}\b(?:overthrown|ousted)\b|"
            r"\bpresident\b.{0,40}\bousted in (?:a )?coup\b"
        ),
    ),
    (
        "ceasefire", "effective", "大規模停戦の正式成立",
        re.compile(
            r"\bceasefire\b.{0,55}\b(?:signed|agreed|takes effect|enters into force|begins)\b|"
            r"\b(?:signs?|agrees? to)\b.{0,45}\bceasefire\b"
        ),
    ),
    (
        "ceasefire", "collapsed", "大規模停戦の破棄・崩壊",
        re.compile(
            r"\bceasefire\b.{0,55}\b(?:ends|ended|collapses|collapsed|terminated|broken|abandoned)\b|"
            r"\b(?:withdraws?|withdrew) from\b.{0,35}\bceasefire\b"
        ),
    ),
    (
        "major_transport_hub", "closed", "主要港・国際空港の全面閉鎖",
        re.compile(
            r"\b(?:international airport|major airport|major port|shipping port|container port)\b"
            r".{0,55}\b(?:fully closed|closed indefinitely|shut down|suspends all operations|all operations suspended)\b|"
            r"\b(?:fully closes|shuts down)\b.{0,45}"
            r"\b(?:international airport|major airport|major port|shipping port|container port)\b"
        ),
    ),
    (
        "financial_sanctions", "imposed", "大規模金融制裁・SWIFT排除",
        re.compile(
            r"\b(?:excluded|removed|cut off|disconnected|banned)\b.{0,50}\bSWIFT\b|"
            r"\bSWIFT\b.{0,50}\b(?:exclusion|ban|disconnect(?:ed|ion)?|cut off)\b|"
            r"\b(?:imposes?|imposed|announces?|announced)\b.{0,40}\b(?:sweeping|comprehensive) financial sanctions\b"
        ),
    ),
    (
        "systemic_financial_event", "effective", "資本規制・銀行休業・国家債務不履行",
        re.compile(
            r"\b(?:imposes?|imposed|introduces?|introduced)\b.{0,40}\bcapital controls\b|"
            r"\b(?:orders?|ordered|declares?|declared)\b.{0,40}\b(?:bank holiday|banks? closed)\b|"
            r"\b(?:sovereign|government|country)\b.{0,45}\b(?:defaults?|defaulted|debt default)\b"
        ),
    ),
    (
        "major_disaster", "infrastructure_disrupted", "巨大災害による広域インフラ停止",
        re.compile(
            r"\b(?:major|massive|powerful|devastating)\b.{0,25}\b(?:earthquake|tsunami|eruption)\b"
            r".{0,70}\b(?:widespread outage|infrastructure failure|transport halted|power grid down|ports? closed|airports? closed)\b|"
            r"\b(?:earthquake|tsunami|eruption)\b.{0,70}\b(?:widespread outage|infrastructure failure|power grid down)\b"
        ),
    ),
    (
        "major_exchange", "trading_halted", "大手取引所の全面売買停止",
        re.compile(
            r"\b(?:nyse|nasdaq|london stock exchange|tokyo stock exchange|tse|hkex|euronext|cme)\b"
            r".{0,55}\b(?:halts all trading|trading halted|suspends all trading|market-wide halt)\b|"
            r"\b(?:halts all trading|suspends all trading|market-wide halt)\b.{0,55}"
            r"\b(?:nyse|nasdaq|london stock exchange|tokyo stock exchange|tse|hkex|euronext|cme)\b"
        ),
    ),
    (
        "critical_facility", "long_term_shutdown", "重要半導体・エネルギー施設の長期停止",
        re.compile(
            r"\b(?:semiconductor fab|chip plant|lng terminal|major refinery|oil field|gas field|power plant)\b"
            r".{0,65}\b(?:shut down indefinitely|closed indefinitely|production halted for weeks|extended shutdown|long-term shutdown)\b"
        ),
    ),
]


@dataclass
class EmergencyScoutResult:
    status: str
    head_requests: int
    discovered_files: int
    scanned_files: int
    downloaded_bytes: int
    candidates_found: int
    ready_alerts: list[dict[str, Any]]
    errors: list[str]
    next_scan_at: str


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", " ", value).split())


def _domain(value: str) -> str:
    domain = urlsplit(value).netloc.casefold().split(":", 1)[0]
    return domain[4:] if domain.startswith("www.") else domain


def classify_title(record: dict[str, Any], stamp: str, now: datetime) -> list[dict[str, Any]]:
    title = str(record.get("title") or "").strip()
    if not title:
        return []
    text = _normalized(title)
    if not text or HYPOTHETICAL.search(text):
        return []
    matches: list[tuple[str, str, str, str]] = []
    for entity_key, aliases in DEFAULT_CHOKEPOINTS.items():
        if not any(_normalized(alias) in text for alias in aliases):
            continue
        if REOPENED.search(text):
            matches.append((f"chokepoint:{entity_key}", "chokepoint_status", "reopened", DISPLAY_NAMES.get(entity_key, entity_key)))
        elif CLOSED.search(text):
            matches.append((f"chokepoint:{entity_key}", "chokepoint_status", "closed", DISPLAY_NAMES.get(entity_key, entity_key)))
    if WAR_DECLARED.search(text):
        matches.append(("declaration_of_war:global", "declaration_of_war", "declared", "正式な宣戦布告"))
    for event_type, event_state, entity, pattern in EXTENDED_EVENT_RULES:
        if event_type == "head_of_state" and HEAD_OF_STATE_FALSE_CONTEXT.search(text):
            continue
        if pattern.search(text):
            matches.append((f"{event_type}:global", event_type, event_state, entity))

    url = str(record.get("url") or "").strip()
    title_key = stable_id("emergency-title", text)
    output: list[dict[str, Any]] = []
    for event_key, event_type, event_state, entity in matches:
        candidate_id = stable_id(event_key, event_state, url or title_key)
        output.append(
            {
                "candidate_id": candidate_id,
                "event_key": event_key,
                "event_type": event_type,
                "event_state": event_state,
                "entity": entity,
                "title": title,
                "title_key": title_key,
                "url": url,
                "domain": _domain(url),
                "source_language": str(record.get("lang") or ""),
                "seen_date": str(record.get("date") or stamp),
                "ngram_stamp": stamp,
                "discovered_at": _iso(now),
            }
        )
    return output


def _trusted(domain: str, trusted_domains: list[str]) -> bool:
    domain = domain.casefold().strip(".")
    return any(domain == item or domain.endswith("." + item) for item in trusted_domains)


def ready_alerts(
    database: NewsDatabase,
    settings: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    corroboration_minutes = int(settings.get("corroboration_window_minutes", 90))
    cooldown_minutes = int(settings.get("alert_cooldown_minutes", 360))
    required_domains = int(settings.get("minimum_independent_domains", 2))
    required_titles = int(settings.get("minimum_independent_titles", 2))
    trusted_domains = [
        str(item).casefold() for item in settings.get(
            "trusted_domains",
            ["reuters.com", "apnews.com", "bbc.com", "aljazeera.com"],
        )
    ]
    rows = database.emergency_candidates_since(
        _iso(now - timedelta(minutes=corroboration_minutes))
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["event_key"], row["event_state"])].append(row)
    alerts: list[dict[str, Any]] = []
    cooldown_since = _iso(now - timedelta(minutes=cooldown_minutes))
    for (event_key, event_state), evidence in grouped.items():
        if database.emergency_alert_exists_since(event_key, event_state, cooldown_since):
            continue
        domains = {row["domain"] for row in evidence if row["domain"]}
        title_keys = {row["title_key"] for row in evidence if row["title_key"]}
        trusted_evidence = any(_trusted(row["domain"], trusted_domains) for row in evidence)
        independently_confirmed = (
            len(domains) >= required_domains and len(title_keys) >= required_titles
        )
        if not trusted_evidence and not independently_confirmed:
            continue
        latest = evidence[-1]
        alerts.append(
            {
                "alert_id": stable_id(event_key, event_state, _iso(now)),
                "event_key": event_key,
                "event_type": latest["event_type"],
                "event_state": event_state,
                "entity": latest["entity"],
                "confirmation": "trusted_source" if trusted_evidence else "independent_sources",
                "evidence": [
                    {
                        "title": row["title"], "url": row["url"], "domain": row["domain"],
                        "seen_date": row["seen_date"], "title_key": row["title_key"],
                    }
                    for row in evidence[-5:]
                ],
            }
        )
    return alerts


def _cleanup_cache(cache_dir: Path, retention_hours: int) -> None:
    if not cache_dir.is_dir():
        return
    cutoff = time.time() - max(1, retention_hours) * 3600
    for path in cache_dir.glob("*.toc.json.gz"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()


def _record_scout_result(
    database: NewsDatabase,
    result: EmergencyScoutResult,
    started_at: str,
) -> None:
    database.record_emergency_scout_run(
        {
            "run_id": stable_id("emergency-scout", started_at),
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": result.status,
            "head_requests": result.head_requests,
            "discovered_files": result.discovered_files,
            "scanned_files": result.scanned_files,
            "downloaded_bytes": result.downloaded_bytes,
            "candidates_found": result.candidates_found,
            "ready_alerts": len(result.ready_alerts),
            "errors": result.errors,
        }
    )


def run_emergency_scout(
    database: NewsDatabase,
    settings: dict[str, Any],
    collector_settings: dict[str, Any],
    now: datetime,
    cache_dir: Path,
    global_news_settings: dict[str, Any] | None = None,
) -> EmergencyScoutResult:
    started_at = _iso(now)
    if not settings.get("enabled", False):
        return EmergencyScoutResult("disabled", 0, 0, 0, 0, 0, [], [], "")
    raw_next = database.get_state(STATE_KEY)
    if raw_next:
        next_at = datetime.fromisoformat(raw_next)
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=timezone.utc)
        if now < next_at:
            return EmergencyScoutResult(
                "schedule_skipped", 0, 0, 0, 0, 0, [], [], _iso(next_at)
            )

    interval = int(settings.get("interval_minutes", 15))
    retry = int(settings.get("retry_interval_minutes", 5))
    lookback = int(settings.get("lookback_minutes", 30))
    errors: list[str] = []
    processed = database.scanned_toc_stamps_since(
        (now - timedelta(minutes=lookback)).strftime("%Y%m%d%H%M00")
    )
    try:
        files, head_requests = discover_files(
            now - timedelta(minutes=lookback),
            now,
            root=str(collector_settings.get("web_ngrams_root") or DEFAULT_ROOT),
            timeout_seconds=int(collector_settings.get("request_timeout_seconds", 30)),
            publication_lag_minutes=int(collector_settings.get("publication_lag_minutes", 5)),
            request_spacing_seconds=float(settings.get("head_request_spacing_seconds", 0.1)),
            skip_stamps=processed,
        )
    except NgramCollectorError as exc:
        next_scan = now + timedelta(minutes=retry)
        database.set_state(STATE_KEY, _iso(next_scan), _iso(now))
        result = EmergencyScoutResult(
            "failed", 0, 0, 0, 0, 0, [], [str(exc)], _iso(next_scan)
        )
        _record_scout_result(database, result, started_at)
        return result

    pending = [file for file in files if file.stamp not in processed]
    downloaded_bytes = 0
    candidate_count = 0
    scanned_files = 0
    for index, file in enumerate(pending):
        try:
            toc, file_bytes = load_toc(
                file,
                int(collector_settings.get("request_timeout_seconds", 30)),
                cache_dir,
            )
            downloaded_bytes += file_bytes
            world_settings = global_news_settings or {}
            database.record_global_news_candidates(
                rank_toc_records(list(toc.values()), file.stamp, now, world_settings)
            )
            found = 0
            for record in toc.values():
                for candidate in classify_title(record, file.stamp, now):
                    if database.record_emergency_candidate(candidate):
                        candidate_count += 1
                    found += 1
            database.record_toc_file(
                file.stamp, _iso(now), len(toc), found, file_bytes
            )
            scanned_files += 1
        except (NgramCollectorError, OSError, ValueError) as exc:
            errors.append(f"{file.stamp}: {exc}")
        if index < len(pending) - 1:
            spacing = float(settings.get("toc_download_spacing_seconds", 0.5))
            if spacing > 0:
                time.sleep(spacing)

    next_scan = now + timedelta(minutes=retry if errors else interval)
    database.set_state(STATE_KEY, _iso(next_scan), _iso(now))
    prune_global_news(database, global_news_settings or {}, now)
    _cleanup_cache(cache_dir, int(settings.get("toc_cache_retention_hours", 72)))
    alerts = ready_alerts(database, settings, now)
    result = EmergencyScoutResult(
        "completed_with_errors" if errors else "completed",
        head_requests,
        len(files),
        scanned_files,
        downloaded_bytes,
        candidate_count,
        alerts,
        errors,
        _iso(next_scan),
    )
    _record_scout_result(database, result, started_at)
    return result
