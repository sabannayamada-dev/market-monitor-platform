from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import PaperDatabase


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_state(database: PaperDatabase, source: str) -> dict[str, Any]:
    raw = database.get_state(f"source_rate_limit:{source}")
    if not raw:
        return {"cooldown_until": "", "last_429_at": "", "consecutive_429": 0, "last_reason": ""}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"cooldown_until": "", "last_429_at": "", "consecutive_429": 0, "last_reason": "invalid state reset"}


def cooldown_until(database: PaperDatabase, source: str, now: datetime) -> datetime | None:
    until = _parse(str(load_state(database, source).get("cooldown_until") or ""))
    return until if until and until > _utc(now) else None


def on_rate_limited(
    database: PaperDatabase,
    source: str,
    now: datetime,
    retry_after_seconds: int | None,
    settings: dict[str, Any],
) -> dict[str, Any]:
    state = load_state(database, source)
    consecutive = int(state.get("consecutive_429", 0)) + 1
    if "initial_cooldown_minutes" in settings:
        base_seconds = max(60, float(settings["initial_cooldown_minutes"]) * 60)
    else:
        base_seconds = max(15 * 60, float(settings.get("initial_cooldown_hours", 6)) * 3600)
    if "max_cooldown_minutes" in settings:
        max_seconds = max(base_seconds, float(settings["max_cooldown_minutes"]) * 60)
    else:
        max_seconds = max(base_seconds, float(settings.get("max_cooldown_hours", 48)) * 3600)
    adaptive_seconds = min(max_seconds, base_seconds * (2 ** (consecutive - 1)))
    cooldown_seconds = max(adaptive_seconds, float(retry_after_seconds or 0))
    until = _utc(now) + timedelta(seconds=cooldown_seconds)
    state = {
        "cooldown_until": until.isoformat(timespec="seconds"),
        "last_429_at": _utc(now).isoformat(timespec="seconds"),
        "consecutive_429": consecutive,
        "last_reason": f"HTTP 429/403: {cooldown_seconds / 60:g}分休止",
    }
    database.set_state(f"source_rate_limit:{source}", json.dumps(state, ensure_ascii=False), _utc(now).isoformat(timespec="seconds"))
    return state


def on_success(database: PaperDatabase, source: str, now: datetime) -> None:
    previous = load_state(database, source)
    if not previous.get("cooldown_until") and not previous.get("consecutive_429"):
        return
    state = {
        "cooldown_until": "",
        "last_429_at": str(previous.get("last_429_at") or ""),
        "consecutive_429": 0,
        "last_reason": "休止後の収集成功により解除",
    }
    database.set_state(f"source_rate_limit:{source}", json.dumps(state, ensure_ascii=False), _utc(now).isoformat(timespec="seconds"))
