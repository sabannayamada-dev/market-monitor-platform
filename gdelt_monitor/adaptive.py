from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from .database import NewsDatabase


STATE_KEY = "adaptive_control_v1"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class AdaptiveState:
    interval_minutes: float
    cooldown_hours: float
    query_spacing_seconds: float
    next_collection_at: str
    cooldown_until: str | None
    recovery_probe_pending: bool
    stable_window_started_at: str
    stable_successful_runs: int
    last_429_at: str | None
    last_change_reason: str

    def next_at(self, now: datetime) -> datetime:
        return _dt(self.next_collection_at, now)

    def cooldown_at(self, now: datetime) -> datetime | None:
        return _dt(self.cooldown_until, now) if self.cooldown_until else None


def _number(settings: dict[str, Any], name: str, default: float) -> float:
    return float(settings.get(name, default))


def _fixed_cooldown_hours(settings: dict[str, Any]) -> float:
    return max(1 / 60, _number(settings, "fixed_cooldown_hours", 1))


def _fixed_interval_minutes(settings: dict[str, Any]) -> float | None:
    value = settings.get("fixed_interval_minutes")
    return max(1.0, float(value)) if value is not None else None


def load_state(
    database: NewsDatabase,
    settings: dict[str, Any],
    now: datetime,
) -> AdaptiveState:
    raw = database.get_state(STATE_KEY)
    if raw:
        state = AdaptiveState(**json.loads(raw))
        fixed_interval = _fixed_interval_minutes(settings)
        fixed_cooldown = _fixed_cooldown_hours(settings)
        interval_changed = fixed_interval is not None and abs(state.interval_minutes - fixed_interval) > 1e-9
        cooldown_changed = abs(state.cooldown_hours - fixed_cooldown) > 1e-9
        if interval_changed or cooldown_changed:
            previous = AdaptiveState(**asdict(state))
            if fixed_interval is not None:
                state.interval_minutes = fixed_interval
            state.cooldown_hours = fixed_cooldown
            if cooldown_changed:
                cooldown = state.cooldown_at(now)
                maximum_until = now + timedelta(hours=fixed_cooldown)
                if cooldown and cooldown > maximum_until:
                    state.cooldown_until = _iso(maximum_until)
                    if state.next_at(now) > maximum_until:
                        state.next_collection_at = _iso(maximum_until)
            reasons = []
            if interval_changed:
                reasons.append(f"収集周期xを{fixed_interval:g}分に固定")
            if cooldown_changed:
                reasons.append(f"429休止時間yを{fixed_cooldown:g}時間に固定")
            state.last_change_reason = " / ".join(reasons)
            save_state(database, state, now, "fixed_schedule_policy", previous)
        return state

    interval = _fixed_interval_minutes(settings) or _number(settings, "initial_interval_minutes", 60)
    cooldown = _fixed_cooldown_hours(settings)
    legacy_until_text = database.get_state("rate_limit_cooldown_until")
    legacy_until = _dt(legacy_until_text, now) if legacy_until_text else None
    migrated_429 = legacy_until is not None
    if migrated_429:
        interval = min(
            _number(settings, "max_interval_minutes", 180),
            interval + _number(settings, "interval_increase_on_429_minutes", 4),
        )
    state = AdaptiveState(
        interval_minutes=interval,
        cooldown_hours=cooldown,
        query_spacing_seconds=_number(settings, "query_spacing_seconds", 20),
        next_collection_at=_iso(max(now, legacy_until) if legacy_until else now),
        cooldown_until=_iso(legacy_until) if legacy_until and legacy_until > now else None,
        recovery_probe_pending=migrated_429,
        stable_window_started_at=_iso(now),
        stable_successful_runs=0,
        last_429_at=None,
        last_change_reason="既存429休止状態を移行" if migrated_429 else "適応制御を初期化",
    )
    save_state(database, state, now, "initialized", None)
    return state


def save_state(
    database: NewsDatabase,
    state: AdaptiveState,
    now: datetime,
    event_type: str,
    previous: AdaptiveState | None,
) -> None:
    timestamp = _iso(now)
    database.set_state(STATE_KEY, json.dumps(asdict(state), ensure_ascii=False), timestamp)
    database.record_adaptive_event(
        timestamp,
        event_type,
        previous.interval_minutes if previous else None,
        state.interval_minutes,
        previous.cooldown_hours if previous else None,
        state.cooldown_hours,
        previous.query_spacing_seconds if previous else None,
        state.query_spacing_seconds,
        state.next_collection_at,
        state.last_change_reason,
    )


def clone(state: AdaptiveState) -> AdaptiveState:
    return AdaptiveState(**asdict(state))


def lookback_minutes(state: AdaptiveState, collector: dict[str, Any], adaptive: dict[str, Any]) -> int:
    base = int(collector.get("lookback_minutes", 90))
    buffer_minutes = _number(adaptive, "lookback_buffer_minutes", 30)
    return max(15, base, ceil(state.interval_minutes + buffer_minutes))


def waiting_status(state: AdaptiveState, now: datetime) -> str | None:
    if now >= state.next_at(now):
        return None
    cooldown = state.cooldown_at(now)
    return "cooldown_skipped" if cooldown and now < cooldown else "schedule_skipped"


def on_429(
    state: AdaptiveState,
    settings: dict[str, Any],
    now: datetime,
    retry_after_seconds: int | None,
    intra_run_spacing_exposed: bool = False,
) -> tuple[AdaptiveState, AdaptiveState]:
    previous = clone(state)
    state.cooldown_hours = _fixed_cooldown_hours(settings)
    fixed_interval = _fixed_interval_minutes(settings)
    if fixed_interval is not None:
        state.interval_minutes = fixed_interval
        state.query_spacing_seconds = _number(settings, "query_spacing_seconds", 20)
    else:
        state.interval_minutes = min(
            _number(settings, "max_interval_minutes", 180),
            state.interval_minutes + _number(settings, "interval_increase_on_429_minutes", 4),
        )
        if intra_run_spacing_exposed:
            state.query_spacing_seconds = min(
                _number(settings, "max_query_spacing_seconds", 120),
                state.query_spacing_seconds
                + _number(settings, "query_spacing_increase_on_intra_run_429_seconds", 5),
            )
    z_reason = "、zを大幅延長" if intra_run_spacing_exposed else ""
    if state.recovery_probe_pending:
        reason = f"休止明け最初の収集も429: xを増加、yは固定値を維持{z_reason}"
    else:
        reason = f"429を検知: xを増加し固定y時間休止{z_reason}"
    cooldown_seconds = (
        state.cooldown_hours * 3600
        if fixed_interval is not None
        else max(state.cooldown_hours * 3600, float(retry_after_seconds or 0))
    )
    until = now + timedelta(seconds=cooldown_seconds)
    state.cooldown_until = _iso(until)
    state.next_collection_at = _iso(until)
    state.recovery_probe_pending = True
    state.stable_window_started_at = _iso(now)
    state.stable_successful_runs = 0
    state.last_429_at = _iso(now)
    state.last_change_reason = reason
    return state, previous


def on_403(
    state: AdaptiveState,
    now: datetime,
    retry_after_seconds: int | None,
) -> tuple[AdaptiveState, AdaptiveState]:
    previous = clone(state)
    cooldown_seconds = max(state.cooldown_hours * 3600, float(retry_after_seconds or 0))
    until = now + timedelta(seconds=cooldown_seconds)
    state.cooldown_until = _iso(until)
    state.next_collection_at = _iso(until)
    state.last_change_reason = "HTTP 403を検知: x・yを学習変更せず安全休止"
    return state, previous


def on_run_without_429(
    state: AdaptiveState,
    settings: dict[str, Any],
    now: datetime,
    fully_successful: bool,
) -> tuple[AdaptiveState, AdaptiveState, str]:
    previous = clone(state)
    reasons: list[str] = []
    fixed_interval = _fixed_interval_minutes(settings)
    if fixed_interval is not None:
        state.interval_minutes = fixed_interval
        state.cooldown_hours = _fixed_cooldown_hours(settings)
        state.query_spacing_seconds = _number(settings, "query_spacing_seconds", 20)
        state.recovery_probe_pending = False
        state.cooldown_until = None
        state.stable_successful_runs = 0
        state.next_collection_at = _iso(now + timedelta(minutes=fixed_interval))
        state.last_change_reason = (
            f"固定運用: {fixed_interval:g}分後に次回収集、429時は"
            f"{state.cooldown_hours:g}時間休止"
        )
        return state, previous, "fixed_schedule_completed"
    if state.recovery_probe_pending:
        state.cooldown_hours = _fixed_cooldown_hours(settings)
        state.recovery_probe_pending = False
        state.cooldown_until = None
        reasons.append("休止明け最初の収集で429なし: 固定yを維持")
    if fully_successful:
        state.stable_successful_runs += 1
    stable_hours = _number(settings, "stable_period_hours", 24)
    stable_start = _dt(state.stable_window_started_at, now)
    required = int(settings.get("stable_successful_runs_required", 6))
    if fixed_interval is None and now - stable_start >= timedelta(hours=stable_hours) and state.stable_successful_runs >= required:
        state.interval_minutes = max(
            _number(settings, "min_interval_minutes", 30),
            state.interval_minutes - _number(settings, "interval_decrease_after_stable_minutes", 2),
        )
        state.query_spacing_seconds = max(
            _number(settings, "min_query_spacing_seconds", 10),
            state.query_spacing_seconds
            - _number(settings, "query_spacing_decrease_after_stable_seconds", 1),
        )
        state.stable_window_started_at = _iso(now)
        state.stable_successful_runs = 0
        reasons.append("24時間429なし・成功回数条件達成: xを2分、zを1秒短縮")
    state.cooldown_until = None
    next_at = now + timedelta(minutes=state.interval_minutes)
    state.next_collection_at = _iso(next_at)
    state.last_change_reason = " / ".join(reasons) if reasons else "429なし: 現在のxを維持"
    return state, previous, "recovery_success" if reasons else "run_completed"


def snapshot(state: AdaptiveState) -> dict[str, Any]:
    return {
        "adaptive_interval_minutes": state.interval_minutes,
        "adaptive_cooldown_hours": state.cooldown_hours,
        "adaptive_query_spacing_seconds": state.query_spacing_seconds,
        "adaptive_next_collection_at": state.next_collection_at,
        "adaptive_cooldown_until": state.cooldown_until,
        "adaptive_recovery_probe_pending": state.recovery_probe_pending,
        "adaptive_stable_successful_runs": state.stable_successful_runs,
        "adaptive_last_change_reason": state.last_change_reason,
    }


def _reversal_count(values: list[float], epsilon: float = 1e-9) -> int:
    signs: list[int] = []
    for before, after in zip(values, values[1:]):
        delta = after - before
        if abs(delta) <= epsilon:
            continue
        signs.append(1 if delta > 0 else -1)
    return sum(left != right for left, right in zip(signs, signs[1:]))


def detect_stability(
    database: NewsDatabase,
    settings: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    minimum_samples = int(settings.get("minimum_samples", 12))
    window_hours = _number(settings, "window_hours", 72)
    query_margin_hours = _number(settings, "query_margin_hours", 6)
    since = _iso(now - timedelta(hours=window_hours + query_margin_hours))
    rows = database.adaptive_events_since(since)
    result: dict[str, Any] = {
        "stable": False,
        "status": "観測中",
        "reason": "判定に必要な履歴が不足",
        "sample_count": len(rows),
        "window_hours": 0.0,
    }
    if len(rows) < minimum_samples or not database.has_adaptive_rate_signal():
        return result
    first_at = _dt(str(rows[0]["event_at"]), now)
    last_at = _dt(str(rows[-1]["event_at"]), now)
    observed_hours = max(0.0, (last_at - first_at).total_seconds() / 3600)
    result["window_hours"] = round(observed_hours, 2)
    if observed_hours < window_hours:
        result["reason"] = f"観測期間が{window_hours:g}時間未満"
        return result
    latest_age = (now - last_at).total_seconds() / 3600
    if latest_age > _number(settings, "maximum_latest_age_hours", 6):
        result["reason"] = "直近の収集結果が古いため判定保留"
        return result

    x_values = [float(row["new_interval_minutes"]) for row in rows]
    y_values = [float(row["new_cooldown_hours"]) for row in rows]
    z_values = [float(row["new_query_spacing_seconds"]) for row in rows]
    x_range = max(x_values) - min(x_values)
    y_range = max(y_values) - min(y_values)
    z_range = max(z_values) - min(z_values)
    result.update(
        {
            "x_baseline": float(statistics.median(x_values)),
            "y_baseline": float(statistics.median(y_values)),
            "z_baseline": float(statistics.median(z_values)),
            "x_current": x_values[-1], "y_current": y_values[-1], "z_current": z_values[-1],
            "x_range": x_range, "y_range": y_range, "z_range": z_range,
        }
    )
    parameter_specs = {
        "x": (x_values, x_range, "maximum_x_range_minutes", 8, "plateau_x_range_minutes", 2),
        "y": (y_values, y_range, "maximum_y_range_hours", 1, "plateau_y_range_hours", 0.5),
        "z": (z_values, z_range, "maximum_z_range_seconds", 5, "plateau_z_range_seconds", 2),
    }
    parameters: dict[str, dict[str, Any]] = {}
    stable_parameters: list[str] = []
    for name, (values, value_range, maximum_key, maximum_default, plateau_key, plateau_default) in parameter_specs.items():
        maximum = _number(settings, maximum_key, maximum_default)
        plateau_limit = _number(settings, plateau_key, plateau_default)
        reversals = _reversal_count(values)
        plateau = value_range <= plateau_limit
        oscillating = (
            value_range <= maximum
            and reversals >= int(settings.get("minimum_reversals", 2))
            and abs(values[-1] - values[0]) <= plateau_limit
        )
        stable = plateau or oscillating
        if value_range > maximum:
            reason = "変動幅が安定域を超過"
        elif plateau:
            reason = "3日間（72時間）ほぼ横ばい"
        elif oscillating:
            reason = "狭い範囲で往復し開始水準へ回帰"
        else:
            reason = "狭い範囲だが一方向への移動中"
        parameters[name] = {
            "stable": stable,
            "reason": reason,
            "baseline": float(statistics.median(values)),
            "current": values[-1],
            "range": value_range,
            "reversal_count": reversals,
        }
        if stable:
            stable_parameters.append(name)
    result["parameters"] = parameters
    result["stable_parameters"] = stable_parameters
    result["stable"] = bool(stable_parameters)
    result["all_stable"] = len(stable_parameters) == 3
    result["status"] = "全体安定" if result["all_stable"] else (
        "個別安定: " + ",".join(stable_parameters) if stable_parameters else "観測中"
    )
    result["reason"] = " / ".join(
        f"{name}:{details['reason']}" for name, details in parameters.items()
    )
    return result


def stability_alert_parameters(
    database: NewsDatabase,
    result: dict[str, Any],
    settings: dict[str, Any],
) -> list[str]:
    if not result.get("stable"):
        return []
    raw = database.get_state("adaptive_stability_last_alert")
    previous = json.loads(raw) if raw else {}
    distances = {"x": ("realert_x_distance_minutes", 8), "y": ("realert_y_distance_hours", 1), "z": ("realert_z_distance_seconds", 5)}
    pending: list[str] = []
    for name in result.get("stable_parameters", []):
        prior = previous.get(name)
        if prior is None:
            pending.append(name)
            continue
        key, default = distances[name]
        if abs(float(result["parameters"][name]["baseline"]) - float(prior["baseline"])) > _number(settings, key, default):
            pending.append(name)
    return pending


def mark_stability_alert_sent(
    database: NewsDatabase,
    result: dict[str, Any],
    parameters: list[str],
    now: datetime,
) -> None:
    raw = database.get_state("adaptive_stability_last_alert")
    payload = json.loads(raw) if raw else {}
    for name in parameters:
        payload[name] = {
            "baseline": result["parameters"][name]["baseline"],
            "sent_at": _iso(now),
        }
    database.set_state(
        "adaptive_stability_last_alert", json.dumps(payload, ensure_ascii=False), _iso(now)
    )
