from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .collector import stable_id
from .database import NewsDatabase


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI response did not contain JSON")
    return json.loads(text[start : end + 1])


def review_candidates(
    database: NewsDatabase,
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, int]:
    result = {"gpt_sent": 0, "gpt_passed": 0, "gemini_sent": 0}
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not openai_key:
        return result
    ai_config = config.get("ai", {})
    gpt_threshold = float(ai_config.get("gpt_score_threshold", 60))
    gpt_limit = int(ai_config.get("daily_gpt_limit", 20))
    gemini_threshold = float(ai_config.get("gemini_score_threshold", 90))
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    day_start = datetime(now_jst.year, now_jst.month, now_jst.day, tzinfo=now_jst.tzinfo).astimezone(timezone.utc).isoformat(timespec="seconds")
    used_gpt = _provider_count_since(database, "openai", day_start)
    used_gemini = _provider_count_since(database, "gemini", day_start)
    remaining_gpt = max(0, gpt_limit - used_gpt)
    remaining_gemini = max(0, int(ai_config.get("daily_gemini_limit", 5)) - used_gemini)
    for candidate in [c for c in candidates if c["score"] >= gpt_threshold][:remaining_gpt]:
        existing = _review_exists(database, candidate["article_id"], "openai")
        if existing:
            continue
        prompt = _prompt(candidate)
        payload = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": str(ai_config.get("gpt_model", "gpt-5-nano")),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {openai_key}"},
        )
        review = _extract_json(payload["choices"][0]["message"]["content"])
        _store_review(database, candidate["article_id"], "openai", str(ai_config.get("gpt_model", "gpt-5-nano")), review)
        result["gpt_sent"] += 1
        importance = float(review.get("importance", 0))
        if importance >= gpt_threshold:
            result["gpt_passed"] += 1
        if gemini_key and importance >= gemini_threshold and result["gemini_sent"] < remaining_gemini:
            gemini_model = str(ai_config.get("gemini_model", "gemini-3.1-flash-lite"))
            gp = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}",
                {"contents": [{"parts": [{"text": prompt + "\nOpenAI一次判定:\n" + json.dumps(review, ensure_ascii=False)}]}]},
                {},
            )
            greview = _extract_json(gp["candidates"][0]["content"]["parts"][0]["text"])
            _store_review(database, candidate["article_id"], "gemini", gemini_model, greview)
            result["gemini_sent"] += 1
    return result


def _prompt(candidate: dict[str, Any]) -> str:
    return """あなたは日本株ニュースの慎重な審査担当です。記事本文は未取得なので、タイトルだけで断定しないでください。
JSONのみで importance(0-100), direction(positive/negative/neutral/unknown), summary, risk を返してください。
誤った企業紐付けや未確認情報の可能性をriskに明記してください。
候補: """ + json.dumps(candidate, ensure_ascii=False)


def _review_exists(database: NewsDatabase, article_id: str, provider: str) -> bool:
    with database.connection() as connection:
        return connection.execute(
            "SELECT 1 FROM ai_reviews WHERE article_id=? AND provider=?", (article_id, provider)
        ).fetchone() is not None


def _provider_count_since(database: NewsDatabase, provider: str, since: str) -> int:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) count FROM ai_reviews WHERE provider=? AND reviewed_at>=?",
            (provider, since),
        ).fetchone()
    return int(row["count"] or 0)


def _store_review(database: NewsDatabase, article_id: str, provider: str, model: str, review: dict[str, Any]) -> None:
    with database.connection() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO ai_reviews(
               article_id,provider,model,status,importance,direction,summary,risk,raw_json,reviewed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                article_id, provider, model, "completed", float(review.get("importance", 0)),
                str(review.get("direction", "unknown")), str(review.get("summary", "")),
                str(review.get("risk", "")), json.dumps(review, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


EMERGENCY_REVIEW_VERSION = "v2-score-100"
JEV_EMERGENCY_REVIEW_VERSION = "v1-five-gates"
EMERGENCY_CATEGORIES = (
    "nuclear_weapon_event", "nuclear_facility_attack", "head_of_state", "coup",
    "ceasefire", "major_transport_hub", "financial_sanctions",
    "systemic_financial_event", "major_disaster", "major_exchange",
    "critical_facility", "chokepoint_status", "declaration_of_war", "other",
)


EMERGENCY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "event_confirmed": {"type": "boolean"},
        "same_event": {"type": "boolean"},
        "market_impact": {"type": "integer", "minimum": 0, "maximum": 100},
        "urgency": {"type": "integer", "minimum": 0, "maximum": 100},
        "is_speculation": {"type": "boolean"},
        "event_category": {"type": "string", "enum": list(EMERGENCY_CATEGORIES)},
        "affected_assets": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "japanese_summary": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "event_confirmed", "same_event", "market_impact", "urgency", "is_speculation",
        "event_category", "affected_assets", "japanese_summary", "reason",
    ],
}


def review_emergency_alerts(
    database: NewsDatabase,
    alerts: list[dict[str, Any]],
    settings: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    stats = {"sent": 0, "approved": 0, "cached": 0, "budget_skipped": 0}
    errors: list[str] = []
    if not alerts:
        return [], stats, errors
    ai_settings = settings.get("ai_review", {})
    if not ai_settings.get("enabled", True):
        return [], stats, ["emergency AI review is disabled; alerts held"]
    self_test_state = database.get_state("emergency_ai_self_test_v3")
    if self_test_state:
        try:
            self_test_status = str(json.loads(self_test_state).get("status", "unknown"))
        except (TypeError, ValueError):
            self_test_status = "invalid"
        if self_test_status != "completed":
            return [], stats, [f"emergency AI self-test is not healthy ({self_test_status}); alerts held"]
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return [], stats, ["OPENAI_API_KEY is not configured; alerts held"]

    now_jst = now.astimezone(ZoneInfo("Asia/Tokyo"))
    day_start = datetime(
        now_jst.year, now_jst.month, now_jst.day, tzinfo=now_jst.tzinfo
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    month_start = datetime(
        now_jst.year, now_jst.month, 1, tzinfo=now_jst.tzinfo
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    daily_usage = database.emergency_ai_usage_since(day_start)
    monthly_usage = database.emergency_ai_usage_since(month_start)
    daily_limit = int(ai_settings.get("daily_request_limit", 500))
    monthly_limit = int(ai_settings.get("monthly_request_limit", 30000))
    monthly_budget = float(ai_settings.get("monthly_budget_usd", 7.0))
    model = str(ai_settings.get("model", "gpt-5-nano"))
    impact_threshold = float(ai_settings.get("market_impact_threshold", 85))
    urgency_threshold = float(ai_settings.get("urgency_threshold", 85))
    approved: list[dict[str, Any]] = []

    for alert in alerts:
        evidence_key = emergency_evidence_key(alert)
        existing = database.emergency_ai_review(evidence_key)
        if existing is not None and existing.get("status") == "completed":
            stats["cached"] += 1
            review = {
                "event_confirmed": bool(existing["event_confirmed"]),
                "same_event": bool(existing["same_event"]),
                "market_impact": float(existing["market_impact"]),
                "urgency": float(existing["urgency"]),
                "is_speculation": bool(existing["is_speculation"]),
                "event_category": existing["event_category"],
                "affected_assets": json.loads(existing["affected_assets_json"]),
                "japanese_summary": existing["japanese_summary"],
                "reason": existing["reason"],
            }
        else:
            if (
                daily_usage["requests"] + stats["sent"] >= daily_limit
                or monthly_usage["requests"] + stats["sent"] >= monthly_limit
                or monthly_usage["cost_usd"] >= monthly_budget
            ):
                stats["budget_skipped"] += 1
                continue
            try:
                payload = _post_json(
                    "https://api.openai.com/v1/chat/completions",
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": _emergency_prompt(alert)}],
                        "reasoning_effort": str(ai_settings.get("reasoning_effort", "minimal")),
                        "max_completion_tokens": int(ai_settings.get("max_completion_tokens", 500)),
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "emergency_market_event_review",
                                "strict": True,
                                "schema": EMERGENCY_SCHEMA,
                            },
                        },
                    },
                    {"Authorization": f"Bearer {api_key}"},
                    timeout=int(ai_settings.get("request_timeout_seconds", 45)),
                )
                review = _validated_emergency_review(
                    _extract_json(payload["choices"][0]["message"]["content"]),
                    str(alert.get("event_type", "other")),
                )
                usage = payload.get("usage", {})
                input_tokens = int(usage.get("prompt_tokens", 0))
                output_tokens = int(usage.get("completion_tokens", 0))
                estimated_cost = (
                    input_tokens * float(ai_settings.get("input_usd_per_million", 0.05))
                    + output_tokens * float(ai_settings.get("output_usd_per_million", 0.40))
                ) / 1_000_000
                database.record_emergency_ai_review(
                    {
                        "review_id": stable_id("emergency-ai", evidence_key),
                        "evidence_key": evidence_key,
                        "event_key": alert["event_key"],
                        "event_state": alert["event_state"],
                        "model": model,
                        "status": "completed",
                        **review,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "estimated_cost_usd": estimated_cost,
                        "raw": payload,
                        "reviewed_at": now.isoformat(timespec="seconds"),
                    }
                )
                stats["sent"] += 1
            except Exception as exc:
                errors.append(f"{alert['event_key']}: {type(exc).__name__}: {exc}")
                continue

        passes = (
            bool(review.get("event_confirmed"))
            and bool(review.get("same_event"))
            and not bool(review.get("is_speculation", True))
            and float(review.get("market_impact", 0)) >= impact_threshold
            and float(review.get("urgency", 0)) >= urgency_threshold
        )
        if passes:
            enriched = dict(alert)
            enriched["ai_review"] = review
            enriched["entity"] = str(review.get("japanese_summary") or alert.get("entity", ""))
            approved.append(enriched)
            stats["approved"] += 1
    return approved, stats, errors


def _emergency_prompt(alert: dict[str, Any]) -> str:
    categories = ", ".join(EMERGENCY_CATEGORIES)
    return (
        "You are the final gate for an immediate Japanese market-alert system. "
        "Judge only from the supplied headlines and source metadata; do not add outside facts. "
        "Approve factual events that have already happened or are formally effective and can materially "
        "affect global markets, logistics, finance, energy, or critical technology within hours. "
        "Mark event_confirmed=false or is_speculation=true for warnings, threats, possibilities, plans, "
        "talks, proposals, exercises, historical references, opinion, or ambiguous wording. "
        "If multiple headlines refer to different incidents, set same_event=false. For one headline, "
        "same_event means the headline is internally about one coherent event. "
        "market_impact and urgency MUST each be an integer on a 0-100 scale, never a 0-5 or 0-10 scale. "
        "Use these anchors: 0=no impact/urgency, 50=material but not an immediate global alert, "
        "85=minimum for an immediate market alert, and 100=an exceptional global shock requiring action now. "
        f"event_category MUST be exactly one of: {categories}. Prefer the supplied event_type when it fits. "
        "Return a concise Japanese summary and reason. Input:\n" + json.dumps(alert, ensure_ascii=False)
    )


def _validated_emergency_review(review: dict[str, Any], expected_category: str) -> dict[str, Any]:
    validated = dict(review)
    for name in ("market_impact", "urgency"):
        value = validated.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number on the 0-100 scale")
        if value < 0 or value > 100:
            raise ValueError(f"{name} is outside the 0-100 scale")
    category = str(validated.get("event_category", ""))
    if category not in EMERGENCY_CATEGORIES:
        category = expected_category if expected_category in EMERGENCY_CATEGORIES else "other"
    validated["event_category"] = category
    return validated


def emergency_evidence_key(alert: dict[str, Any]) -> str:
    evidence_parts = sorted(
        str(item.get("title_key") or item.get("url") or item.get("title") or "")
        for item in alert.get("evidence", [])
    )
    return stable_id(
        EMERGENCY_REVIEW_VERSION, alert["event_key"], alert["event_state"], *evidence_parts
    )


def _jev_questions() -> dict[str, Any]:
    return {
        "event_confirmed": {
            "type": "noul",
            "instructions": (
                "Do the supplied headlines report that the event has already happened or is formally "
                "in effect? Judge only the supplied state."
            ),
            "criteria": {
                "true": "A completed or formally effective event is explicitly reported.",
                "false": "Only a warning, threat, plan, proposal, exercise, discussion, or ambiguity is reported.",
            },
        },
        "same_event": {
            "type": "noul",
            "instructions": "Do all supplied headlines describe the same underlying event and state?",
            "criteria": {
                "true": "They describe one coherent event; a single headline is internally coherent.",
                "false": "They mix different incidents, states, places, or unrelated references.",
            },
        },
        "non_speculation": {
            "type": "noul",
            "instructions": "Is the reported event factual rather than speculative or hypothetical?",
            "criteria": {
                "true": "The wording reports a fact or formal action.",
                "false": "The wording is rumor, prediction, possibility, opinion, or hypothetical.",
            },
        },
        "market_impact": {
            "type": "noul",
            "instructions": (
                "Can this event materially affect global financial markets, logistics, finance, energy, "
                "or critical technology within hours?"
            ),
            "criteria": {
                "true": "The event plausibly creates immediate and material cross-market or global disruption.",
                "false": "Its likely effects are local, routine, minor, slow, or not market-relevant.",
            },
        },
        "urgent": {
            "type": "noul",
            "instructions": "Does this event warrant an immediate breaking alert rather than the daily digest?",
            "criteria": {
                "true": "A market observer should be interrupted now.",
                "false": "The daily digest is timely enough or the event is insufficiently consequential.",
            },
        },
        "event_category": {
            "type": "choice",
            "instructions": "Choose the single category that best matches the supplied event.",
            "criteria": {category: None for category in EMERGENCY_CATEGORIES},
        },
    }


def _post_jev_json(
    payload: dict[str, Any], api_key: str, timeout: int, max_retries: int,
    initial_backoff_seconds: float, max_backoff_seconds: float,
) -> dict[str, Any]:
    for attempt in range(max_retries + 1):
        try:
            return _post_json(
                "https://api.typesafe.ai/v1/systemone",
                payload,
                {"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 529) or attempt >= max_retries:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else initial_backoff_seconds * (2 ** attempt)
            except (TypeError, ValueError):
                delay = initial_backoff_seconds * (2 ** attempt)
            time.sleep(min(max_backoff_seconds, max(0.0, delay)))
    raise RuntimeError("unreachable Jev retry state")


def _parse_jev_review(payload: dict[str, Any], expected_category: str) -> dict[str, Any]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Jev response did not contain answers")

    def probability(name: str) -> float:
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise ValueError(f"Jev answer {name} was not a noul")
        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"Jev answer {name} was outside 0-1")
        return float(value)

    category_answer = answers.get("event_category")
    if not isinstance(category_answer, dict) or category_answer.get("type") != "choice":
        raise ValueError("Jev event_category was not a choice")
    category = str(category_answer.get("choice", ""))
    if category not in EMERGENCY_CATEGORIES:
        category = expected_category if expected_category in EMERGENCY_CATEGORIES else "other"
    confidence = category_answer.get("confidence", 0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0
    return {
        "event_confirmed_probability": probability("event_confirmed"),
        "same_event_probability": probability("same_event"),
        "non_speculation_probability": probability("non_speculation"),
        "market_impact_probability": probability("market_impact"),
        "urgency_probability": probability("urgent"),
        "event_category": category,
        "category_confidence": min(1.0, max(0.0, float(confidence))),
    }


def review_emergency_alerts_with_jev(
    database: NewsDatabase,
    alerts: list[dict[str, Any]],
    settings: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    stats = {"sent": 0, "approved": 0, "cached": 0, "budget_skipped": 0}
    errors: list[str] = []
    jev_settings = settings.get("jev_review", {})
    if not alerts or not jev_settings.get("enabled", True):
        return [], stats, errors
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return [], stats, errors

    now_jst = now.astimezone(ZoneInfo("Asia/Tokyo"))
    day_start = datetime(
        now_jst.year, now_jst.month, now_jst.day, tzinfo=now_jst.tzinfo
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    month_start = datetime(
        now_jst.year, now_jst.month, 1, tzinfo=now_jst.tzinfo
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    daily_usage = database.emergency_jev_usage_since(day_start)
    monthly_usage = database.emergency_jev_usage_since(month_start)
    daily_limit = int(jev_settings.get("daily_request_limit", 500))
    monthly_limit = int(jev_settings.get("monthly_request_limit", 30000))
    monthly_budget = float(jev_settings.get("monthly_budget_usd", 1.0))
    threshold = float(jev_settings.get("minimum_probability", 0.75))
    model = str(jev_settings.get("model", "jev-latest"))
    approved: list[dict[str, Any]] = []

    for alert in alerts:
        evidence_key = stable_id(JEV_EMERGENCY_REVIEW_VERSION, emergency_evidence_key(alert))
        existing = database.emergency_jev_review(evidence_key)
        if existing is not None and existing.get("status") == "completed":
            stats["cached"] += 1
            review = {
                name: float(existing[name])
                for name in (
                    "event_confirmed_probability", "same_event_probability",
                    "non_speculation_probability", "market_impact_probability",
                    "urgency_probability", "category_confidence",
                )
            }
            review["event_category"] = str(existing["event_category"])
            passes = bool(existing["approved"])
        else:
            if (
                daily_usage["requests"] + stats["sent"] >= daily_limit
                or monthly_usage["requests"] + stats["sent"] >= monthly_limit
                or monthly_usage["cost_usd"] >= monthly_budget
            ):
                stats["budget_skipped"] += 1
                continue
            try:
                state = {
                    "event_type": alert.get("event_type"),
                    "event_state": alert.get("event_state"),
                    "entity": alert.get("entity"),
                    "confirmation": alert.get("confirmation"),
                    "evidence": [
                        {
                            "title": item.get("title"),
                            "domain": item.get("domain"),
                            "seen_date": item.get("seen_date"),
                        }
                        for item in alert.get("evidence", [])
                    ],
                }
                payload = _post_jev_json(
                    {"state": state, "model": model, "questions": _jev_questions()},
                    api_key,
                    timeout=int(jev_settings.get("request_timeout_seconds", 15)),
                    max_retries=int(jev_settings.get("max_retries", 2)),
                    initial_backoff_seconds=float(jev_settings.get("initial_backoff_seconds", 1.0)),
                    max_backoff_seconds=float(jev_settings.get("max_backoff_seconds", 10.0)),
                )
                review = _parse_jev_review(payload, str(alert.get("event_type", "other")))
                passes = all(
                    review[name] >= threshold
                    for name in (
                        "event_confirmed_probability", "same_event_probability",
                        "non_speculation_probability", "market_impact_probability",
                        "urgency_probability",
                    )
                )
                usage = payload.get("usage", {})
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                estimated_cost = (
                    input_tokens * float(jev_settings.get("input_usd_per_million", 0.042))
                    + output_tokens * float(jev_settings.get("output_usd_per_million", 0.0))
                ) / 1_000_000
                database.record_emergency_jev_review({
                    "review_id": stable_id("emergency-jev", JEV_EMERGENCY_REVIEW_VERSION, evidence_key),
                    "evidence_key": evidence_key,
                    "event_key": alert["event_key"],
                    "event_state": alert["event_state"],
                    "model": str(payload.get("model") or model),
                    "status": "completed",
                    **review,
                    "approved": passes,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "estimated_cost_usd": estimated_cost,
                    "raw": payload,
                    "reviewed_at": now.isoformat(timespec="seconds"),
                })
                stats["sent"] += 1
            except Exception as exc:
                errors.append(f"{alert['event_key']}: Jev {type(exc).__name__}: {exc}")
                continue
        if passes:
            enriched = dict(alert)
            enriched["jev_review"] = review
            approved.append(enriched)
            stats["approved"] += 1
    return approved, stats, errors


def run_emergency_ai_self_test(
    database: NewsDatabase,
    settings: dict[str, Any],
    now: datetime,
) -> str:
    state_key = "emergency_ai_self_test_v3"
    existing = database.get_state(state_key)
    if existing:
        return str(json.loads(existing).get("status", "unknown"))
    ai_settings = settings.get("ai_review", {})
    if not ai_settings.get("enabled", True) or not ai_settings.get("self_test_on_startup", True):
        return "disabled"
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "key_missing"
    model = str(ai_settings.get("model", "gpt-5-nano"))
    result: dict[str, Any] = {"status": "error", "model": model}
    try:
        negative_payload = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": (
                        "This is a connectivity self-test, not a news event. Return the required JSON "
                        "with event_confirmed=false, same_event=true, is_speculation=true, both scores 0, "
                        "event_category=other, no affected assets, and short Japanese text."
                    ),
                }],
                "reasoning_effort": str(ai_settings.get("reasoning_effort", "minimal")),
                "max_completion_tokens": int(ai_settings.get("max_completion_tokens", 500)),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "emergency_market_event_review",
                        "strict": True,
                        "schema": EMERGENCY_SCHEMA,
                    },
                },
            },
            {"Authorization": f"Bearer {api_key}"},
            timeout=int(ai_settings.get("request_timeout_seconds", 45)),
        )
        negative_review = _validated_emergency_review(
            _extract_json(negative_payload["choices"][0]["message"]["content"]), "other"
        )
        positive_alert = {
            "event_key": "major_exchange:united_states",
            "event_type": "major_exchange",
            "event_state": "trading_halted",
            "entity": "NYSE",
            "confirmation": "trusted_source",
            "evidence": [{
                "title": (
                    "NYSE, Nasdaq, CME and all other major global exchanges halt all trading "
                    "indefinitely after a worldwide market-infrastructure failure"
                ),
                "domain": "reuters.com",
                "seen_date": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            }],
        }
        positive_payload = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": _emergency_prompt(positive_alert)}],
                "reasoning_effort": str(ai_settings.get("reasoning_effort", "minimal")),
                "max_completion_tokens": int(ai_settings.get("max_completion_tokens", 500)),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "emergency_market_event_review",
                        "strict": True,
                        "schema": EMERGENCY_SCHEMA,
                    },
                },
            },
            {"Authorization": f"Bearer {api_key}"},
            timeout=int(ai_settings.get("request_timeout_seconds", 45)),
        )
        positive_review = _validated_emergency_review(
            _extract_json(positive_payload["choices"][0]["message"]["content"]), "major_exchange"
        )
        impact_threshold = float(ai_settings.get("market_impact_threshold", 85))
        urgency_threshold = float(ai_settings.get("urgency_threshold", 85))
        negative_safe = (
            not bool(negative_review.get("event_confirmed"))
            and bool(negative_review.get("is_speculation"))
            and float(negative_review.get("market_impact", -1)) == 0
            and float(negative_review.get("urgency", -1)) == 0
        )
        positive_safe = (
            bool(positive_review.get("event_confirmed"))
            and bool(positive_review.get("same_event"))
            and not bool(positive_review.get("is_speculation", True))
            and positive_review.get("event_category") == "major_exchange"
            and float(positive_review.get("market_impact", 0)) >= impact_threshold
            and float(positive_review.get("urgency", 0)) >= urgency_threshold
        )
        prompt_tokens = sum(
            int(item.get("usage", {}).get("prompt_tokens", 0))
            for item in (negative_payload, positive_payload)
        )
        completion_tokens = sum(
            int(item.get("usage", {}).get("completion_tokens", 0))
            for item in (negative_payload, positive_payload)
        )
        result = {
            "status": "completed" if negative_safe and positive_safe else "unexpected_result",
            "model": model,
            "negative_safe": negative_safe,
            "positive_safe": positive_safe,
            "positive_market_impact": positive_review.get("market_impact"),
            "positive_urgency": positive_review.get("urgency"),
            "positive_category": positive_review.get("event_category"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    except Exception as exc:
        result = {"status": "error", "model": model, "error_type": type(exc).__name__}
    database.set_state(
        state_key,
        json.dumps(result, ensure_ascii=False),
        now.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )
    return str(result["status"])
