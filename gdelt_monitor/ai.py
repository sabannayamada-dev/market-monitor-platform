from __future__ import annotations

import json
import os
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
        evidence_parts = sorted(
            str(item.get("title_key") or item.get("url") or item.get("title") or "")
            for item in alert.get("evidence", [])
        )
        evidence_key = stable_id(
            EMERGENCY_REVIEW_VERSION, alert["event_key"], alert["event_state"], *evidence_parts
        )
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
