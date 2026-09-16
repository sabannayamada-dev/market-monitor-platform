from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .ai import _extract_json, _post_json
from .collector import stable_id
from .database import NewsDatabase


TOPICS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    ("戦争・外交", 38, re.compile(r"\b(?:war|invasion|ceasefire|peace deal|military strike|missile|sanctions?|summit)\b|戦争|侵攻|停戦|和平|空爆|ミサイル|制裁|首脳会談", re.I)),
    ("政治", 30, re.compile(r"\b(?:president|prime minister|election|referendum|coup|government collapses?|impeach|assassinat)\w*\b|大統領|首相|選挙|国民投票|クーデター|政権崩壊|弾劾|暗殺", re.I)),
    ("災害", 38, re.compile(r"\b(?:earthquake|tsunami|hurricane|typhoon|cyclone|eruption|wildfire|floods?|disaster)\b|地震|津波|ハリケーン|台風|噴火|山火事|洪水|災害", re.I)),
    ("経済", 28, re.compile(r"\b(?:central bank|interest rates?|inflation|recession|default|bank crisis|trade war|tariffs?)\b|中央銀行|政策金利|インフレ|景気後退|債務不履行|金融危機|貿易戦争|関税", re.I)),
    ("保健", 32, re.compile(r"\b(?:pandemic|outbreak|epidemic|public health emergency|new virus|vaccine)\b|パンデミック|感染拡大|流行|公衆衛生上の緊急事態|新型ウイルス|ワクチン", re.I)),
    ("科学・技術", 20, re.compile(r"\b(?:artificial intelligence|nuclear fusion|space mission|moon landing|quantum computing|major breakthrough)\b|人工知能|核融合|宇宙探査|月面着陸|量子コンピュータ|重大な発見", re.I)),
    ("気候・環境", 24, re.compile(r"\b(?:climate agreement|climate crisis|record heat|mass extinction|oil spill)\b|気候合意|気候危機|記録的猛暑|大量絶滅|原油流出", re.I)),
)

IMPACT = re.compile(
    r"\b(?:historic|unprecedented|major|massive|deadly|nationwide|global|international|emergency|"
    r"declares?|signed|agreed|approved|resigns?|dies|dead|killed|hospitalized)\b|"
    r"歴史的|前例のない|大規模|多数死亡|全国的|世界的|国際的|緊急|宣言|署名|合意|承認|辞任|死亡|入院",
    re.I,
)
LOW_VALUE = re.compile(
    r"\b(?:sports?|football|soccer|basketball|baseball|celebrity|movie|music|recipe|horoscope|"
    r"lottery|opinion|podcast|preview|live updates?|photos?|video)\b|"
    r"スポーツ|サッカー|野球|芸能|映画|音楽|レシピ|占い|宝くじ|社説|動画",
    re.I,
)
PREMIUM_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bloomberg.com", "ft.com", "nytimes.com",
    "washingtonpost.com", "theguardian.com", "aljazeera.com", "cnn.com", "politico.com",
    "politico.eu", "npr.org", "dw.com", "france24.com", "nhk.or.jp", "cnbc.com",
}

EVENT_STOP_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is", "of", "on",
    "the", "to", "with", "after", "amid", "new", "news", "update", "updates", "briefing",
    "major", "massive", "latest", "says", "say", "over", "war",
}

EVENT_TOKEN_ALIASES = {
    "russian": "russia", "russians": "russia", "ukrainian": "ukraine",
    "strikes": "strike", "striking": "strike", "attacks": "attack", "attacked": "attack",
    "killed": "kill", "kills": "kill", "deaths": "death", "died": "death",
    "wounded": "injure", "injured": "injure", "injuries": "injure",
    "drones": "drone", "missiles": "missile", "floods": "flood",
}

GLOBAL_NEWS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array", "minItems": 2, "maxItems": 2,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "candidate_id": {"type": "string"},
                    "japanese_title": {"type": "string"},
                    "summary": {"type": "string"},
                    "importance": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_id", "japanese_title", "summary", "importance", "reason"],
            },
        },
    },
    "required": ["items"],
}


def _normalized_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", " ", text).split())


def _domain(url: str) -> str:
    value = urlsplit(url).netloc.casefold().split(":", 1)[0]
    return value[4:] if value.startswith("www.") else value


def _trusted_domain(domain: str) -> bool:
    return domain in PREMIUM_DOMAINS or any(domain.endswith("." + item) for item in PREMIUM_DOMAINS)


def _eligible_candidate(item: dict[str, Any], settings: dict[str, Any]) -> bool:
    title = " ".join(str(item.get("title") or "").split())
    maximum = max(80, int(settings.get("maximum_title_characters", 240)))
    if len(title) < 18 or len(title) > maximum:
        return False
    domain = str(item.get("domain") or _domain(str(item.get("url") or "")))
    return not settings.get("require_trusted_source", False) or _trusted_domain(domain)


def _event_tokens(title: str) -> set[str]:
    tokens: set[str] = set()
    for token in _normalized_title(title).split():
        token = EVENT_TOKEN_ALIASES.get(token, token)
        if token in EVENT_STOP_WORDS or len(token) < 3:
            continue
        tokens.add(token)
    return tokens


def _same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left.get("title_key") or "") == str(right.get("title_key") or ""):
        return True
    left_tokens = _event_tokens(str(left.get("title") or ""))
    right_tokens = _event_tokens(str(right.get("title") or ""))
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    return len(shared) >= 3 and len(shared) / min(len(left_tokens), len(right_tokens)) >= 0.33


def _diverse(candidates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for require_new_domain in (True, False):
        for item in candidates:
            candidate_id = str(item.get("candidate_id") or "")
            if not candidate_id or candidate_id in used_ids:
                continue
            if any(_same_event(item, existing) for existing in selected):
                continue
            if require_new_domain and selected and any(
                item.get("domain") == existing.get("domain") for existing in selected
            ):
                continue
            selected.append(item)
            used_ids.add(candidate_id)
            if len(selected) >= count:
                return selected
    return selected


def score_world_headline(record: dict[str, Any]) -> tuple[float, str]:
    title = str(record.get("title") or "").strip()
    if len(title) < 18 or LOW_VALUE.search(title):
        return 0.0, ""
    matches = [(label, points) for label, points, pattern in TOPICS if pattern.search(title)]
    if not matches:
        return 0.0, ""
    topic, score = max(matches, key=lambda item: item[1])
    url = str(record.get("url") or "")
    domain = _domain(url)
    if _trusted_domain(domain):
        score += 22
    if IMPACT.search(title):
        score += 18
    if len(matches) >= 2:
        score += 8
    return min(100.0, float(score)), topic


def rank_toc_records(
    records: list[dict[str, Any]] | Any,
    stamp: str,
    now: datetime,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    if not settings.get("enabled", False):
        return []
    minimum = float(settings.get("minimum_rule_score", 25))
    ranked: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for record in records:
        title = " ".join(str(record.get("title") or "").split())
        if not _eligible_candidate({**record, "title": title}, settings):
            continue
        title_key = _normalized_title(title)
        if not title_key or title_key in seen_titles:
            continue
        score, topic = score_world_headline(record)
        if score < minimum:
            continue
        seen_titles.add(title_key)
        url = str(record.get("url") or "").strip()
        domain = _domain(url)
        ranked.append({
            "candidate_id": stable_id("global-news", title_key, domain),
            "title_key": title_key,
            "title": title,
            "url": url,
            "domain": domain,
            "source_language": str(record.get("lang") or ""),
            "seen_date": str(record.get("date") or stamp),
            "rule_score": score,
            "topic": topic,
            "ngram_stamp": stamp,
            "discovered_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        })
    ranked.sort(key=lambda item: (-item["rule_score"], item["title_key"]))
    return ranked[: max(1, int(settings.get("candidates_per_file", 6)))]


def _fallback(candidates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    return [
        {**item, "japanese_title": "", "summary": "", "importance": item["rule_score"],
         "reason": "見出しの重大性と情報源を基にしたルール選定", "selection_method": "rule"}
        for item in _diverse(candidates, count)
    ]


def select_daily_world_news(
    database: NewsDatabase,
    settings: dict[str, Any],
    since: str,
    digest_date: str,
) -> list[dict[str, Any]]:
    if not settings.get("enabled", False):
        return []
    cached = database.global_news_digest(digest_date)
    if cached is not None:
        return json.loads(cached["selected_json"])
    count = max(1, int(settings.get("item_count", 2)))
    candidates = [
        item for item in database.global_news_candidates_since(
            since, max(2, int(settings.get("max_ai_candidates", 30)) * 3)
        )
        if _eligible_candidate(item, settings)
    ][:max(2, int(settings.get("max_ai_candidates", 30)))]
    fallback = _fallback(candidates, count)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if len(candidates) < count or not api_key or not settings.get("ai_enabled", True):
        database.record_global_news_digest(digest_date, fallback, "rule", "fallback")
        return fallback
    model = str(settings.get("model", "gpt-5-nano"))
    supplied = {item["candidate_id"]: item for item in candidates}
    prompt = (
        "以下は今日のGDELT見出し候補です。株価との関係を評価条件にせず、世界の人々への影響、"
        "地理的な広がり、歴史的重要性、緊急性を総合し、その日の最重要ニュースをちょうど2件選んでください。"
        "同じ出来事や同じ事件の別見出しを2件選ばず、可能なら異なる報道機関から選んでください。"
        "スポーツ・芸能・地域限定の小事件は避けてください。記事本文はないため、"
        "見出しにない数字や事実を加えないでください。candidate_idは入力の値をそのまま返してください。"
        "JSON形式は {\"items\":[{\"candidate_id\":\"...\",\"japanese_title\":\"...\","
        "\"summary\":\"...\",\"importance\":0,\"reason\":\"...\"}]} としてください。\n候補:\n"
        + json.dumps(candidates, ensure_ascii=False)
    )
    try:
        payload = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "reasoning_effort": str(settings.get("reasoning_effort", "minimal")),
                "max_completion_tokens": int(settings.get("max_completion_tokens", 700)),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "daily_world_news", "strict": True, "schema": GLOBAL_NEWS_SCHEMA},
                },
            },
            {"Authorization": f"Bearer {api_key}"},
            timeout=int(settings.get("request_timeout_seconds", 45)),
        )
        response = _extract_json(payload["choices"][0]["message"]["content"])
        selected: list[dict[str, Any]] = []
        for choice in response.get("items", []):
            candidate_id = str(choice.get("candidate_id") or "")
            if candidate_id not in supplied or any(item["candidate_id"] == candidate_id for item in selected):
                continue
            japanese_title = str(choice.get("japanese_title") or "").strip()
            if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", japanese_title):
                japanese_title = ""
            importance = float(choice.get("importance", 0))
            if importance <= 0:
                importance = float(supplied[candidate_id]["rule_score"])
            ai_item = {
                **supplied[candidate_id],
                "japanese_title": japanese_title,
                "summary": " ".join(str(choice.get("summary") or "").split())[:300],
                "importance": max(0.0, min(100.0, importance)),
                "reason": " ".join(str(choice.get("reason") or "").split())[:300],
                "selection_method": "ai",
            }
            if any(_same_event(ai_item, existing) for existing in selected):
                continue
            selected.append(ai_item)
            if len(selected) == count:
                break
        used = {item["candidate_id"] for item in selected}
        completion_pool = [
            *selected,
            *(item for item in fallback if item["candidate_id"] not in used),
        ]
        selected = _diverse(completion_pool, count)
        status = (
            "completed"
            if len(selected) == count and all(item["selection_method"] == "ai" for item in selected)
            else "completed_with_fallback"
        )
        database.record_global_news_digest(digest_date, selected, model, status)
        return selected
    except Exception:
        database.record_global_news_digest(digest_date, fallback, model, "fallback")
        return fallback


def prune_global_news(database: NewsDatabase, settings: dict[str, Any], now: datetime) -> None:
    days = max(2, int(settings.get("retention_days", 30)))
    database.delete_global_news_before((now - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds"))
