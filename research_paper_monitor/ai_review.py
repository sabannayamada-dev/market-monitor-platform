from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .database import PaperDatabase


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relevant": {"type": "boolean"},
        "importance": {"type": "integer", "minimum": 0, "maximum": 100},
        "japanese_title": {"type": "string"},
        "summary": {"type": "string"},
        "novelty": {"type": "string"},
        "applications": {"type": "string"},
        "caution": {"type": "string"},
    },
    "required": ["relevant", "importance", "japanese_title", "summary", "novelty", "applications", "caution"],
}


def _extract_output_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return str(content["text"])
    raise ValueError("OpenAI response did not contain output text")


def _validate_review(review: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in REVIEW_SCHEMA["required"] if name not in review]
    if missing:
        raise ValueError("AI review missing fields: " + ", ".join(missing))
    importance = review["importance"]
    if isinstance(importance, bool) or not isinstance(importance, (int, float)) or not 0 <= importance <= 100:
        raise ValueError("importance must be 0..100")
    return {
        "relevant": bool(review["relevant"]),
        "importance": int(importance),
        **{name: str(review[name]).strip() for name in ("japanese_title", "summary", "novelty", "applications", "caution")},
    }


def _post_review(paper: dict[str, Any], profile_label: str, settings: dict[str, Any], api_key: str) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
    model = str(settings.get("model", "gpt-5.4-nano"))
    material = {
        "profile": profile_label,
        "title": paper.get("title", ""),
        "abstract": str(paper.get("abstract", ""))[:8000],
        "authors": (paper.get("authors") or [])[:8],
        "published_at": paper.get("published_at", ""),
        "journal": paper.get("journal", ""),
        "categories": paper.get("categories", []),
    }
    prompt = (
        "あなたは工学系論文の慎重な選別担当です。提供された書誌情報と要約だけを使い、"
        "対象分野との関連性、技術的新規性、研究・産業上の有用性を評価してください。"
        "断定できない内容はcautionに記載してください。summaryは日本語で3文以内、"
        "noveltyとapplicationsは各1文、japanese_titleは自然な日本語にしてください。入力:\n"
        + json.dumps(material, ensure_ascii=False)
    )
    request_payload = {
        "model": model,
        "instructions": "JSON Schemaに厳密に従って回答してください。",
        "input": prompt,
        "reasoning": {"effort": str(settings.get("reasoning_effort", "none"))},
        "max_output_tokens": int(settings.get("max_output_tokens", 700)),
        "store": False,
        "text": {
            "format": {
                "type": "json_schema", "name": "research_paper_review",
                "strict": True, "schema": REVIEW_SCHEMA,
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=int(settings.get("timeout_seconds", 60))) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    review = _validate_review(json.loads(_extract_output_text(payload)))
    usage_payload = payload.get("usage") or {}
    usage = {
        "input_tokens": int(usage_payload.get("input_tokens", 0)),
        "output_tokens": int(usage_payload.get("output_tokens", 0)),
    }
    return review, usage, payload


def review_candidates(database: PaperDatabase, candidates: list[dict[str, Any]], config: dict[str, Any], now: datetime) -> tuple[dict[str, int], list[str]]:
    stats = {"ai_sent": 0, "ai_completed": 0, "ai_failed": 0, "ai_budget_skipped": 0}
    errors: list[str] = []
    settings = config.get("ai", {})
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not settings.get("enabled", True) or not api_key:
        return stats, (["OPENAI_API_KEY is not configured; rule results will be used"] if settings.get("enabled", True) else [])
    now_jst = now.astimezone(ZoneInfo("Asia/Tokyo"))
    day_start = datetime(now_jst.year, now_jst.month, now_jst.day, tzinfo=now_jst.tzinfo).astimezone(timezone.utc).isoformat()
    month_start = datetime(now_jst.year, now_jst.month, 1, tzinfo=now_jst.tzinfo).astimezone(timezone.utc).isoformat()
    daily = database.ai_usage(day_start)
    monthly = database.ai_usage(month_start)
    daily_limit = int(settings.get("daily_limit", 15))
    monthly_limit = int(settings.get("monthly_limit", 500))
    monthly_budget = float(settings.get("monthly_budget_usd", 3.0))
    model = str(settings.get("model", "gpt-5.4-nano"))
    for candidate in candidates:
        if database.has_ai_review(candidate["paper_id"]):
            continue
        if daily["requests"] + stats["ai_completed"] >= daily_limit or monthly["requests"] + stats["ai_completed"] >= monthly_limit or monthly["cost"] >= monthly_budget:
            stats["ai_budget_skipped"] += 1
            continue
        stats["ai_sent"] += 1
        try:
            review, usage, _ = _post_review(candidate, candidate["profile_label"], settings, api_key)
            cost = (
                usage["input_tokens"] * float(settings.get("input_usd_per_million", 0.20))
                + usage["output_tokens"] * float(settings.get("output_usd_per_million", 1.25))
            ) / 1_000_000
            database.save_ai_review(candidate["paper_id"], model, review, usage, cost, now.astimezone(timezone.utc).isoformat(timespec="seconds"))
            stats["ai_completed"] += 1
        except Exception as exc:
            stats["ai_failed"] += 1
            errors.append(f"AI {candidate['paper_id']}: {type(exc).__name__}: {exc}")
    return stats, errors
