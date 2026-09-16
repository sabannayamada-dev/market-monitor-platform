#!/opt/patent-news-monitor/venv/bin/python
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("/var/lib/patent-news-monitor/backfill_v2")
DEFAULT_COMPANIES = Path("/opt/patent-news-monitor/app/patent_company_master.csv")
SERVICE = "patent-monitor-backfill"


def _systemctl(*args: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(value: str, now: datetime) -> str:
    seconds = _age_seconds(value, now)
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"


def _age_seconds(value: str, now: datetime) -> int | None:
    parsed = _parse_iso(value)
    if not parsed:
        return None
    return max(0, int((now - parsed.astimezone(now.tzinfo)).total_seconds()))


def _file_age(path: Path, now: datetime) -> tuple[str, int | None]:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo).isoformat()
    except OSError:
        return "missing", None
    return _age(mtime, now), _age_seconds(mtime, now)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TiB"


def _percent(done: int | float, total: int | float) -> float:
    return max(0.0, min(100.0, (float(done) * 100.0 / float(total)) if total else 0.0))


def _ansi(value: str, color: str, enabled: bool) -> str:
    if not enabled:
        return value
    codes = {
        "green": "32",
        "yellow": "33",
        "cyan": "36",
        "red": "31",
        "bold": "1",
        "dim": "2",
    }
    return f"\033[{codes[color]}m{value}\033[0m"


def _delta_badge(delta: int | float, color: bool, *, suffix: str = "", invert: bool = False) -> str:
    if delta == 0:
        return _ansi("(=)", "dim", color)
    sign = "+" if delta > 0 else ""
    good = delta > 0
    if invert:
        good = not good
    badge_color = "green" if good else "red"
    if isinstance(delta, float) and not float(delta).is_integer():
        text = f"({sign}{delta:.1f}{suffix})"
    else:
        text = f"({sign}{int(delta)}{suffix})"
    return _ansi(text, badge_color, color)


def _delta_value(value: Any, delta: int | float, color: bool, *, suffix: str = "", invert: bool = False) -> str:
    return f"{value} {_delta_badge(delta, color, suffix=suffix, invert=invert)}"


def _size_delta_badge(delta_bytes: int, color: bool) -> str:
    if delta_bytes == 0:
        return _ansi("(=)", "dim", color)
    sign = "+" if delta_bytes > 0 else ""
    return _ansi(f"({sign}{_human_size(abs(delta_bytes))})", "green" if delta_bytes > 0 else "red", color)


def _extract_progress_counts(message: str) -> dict[str, int]:
    text = str(message or "")
    values: dict[str, int] = {}
    patterns = {
        "company_candidates": r"company_candidates=(\d+)",
        "window_new": r"window_new=(\d+)",
        "window_total": r"window_total=(\d+)",
        "new_candidates": r"新規候補=(\d+)",
        "total_candidates": r"累計=(\d+)",
        "detail_index": r"EPO詳細補完(?: 開始)? (\d+)/(\d+)件",
        "claims_index": r"EPO請求項補完 (\d+)/(\d+)件",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            continue
        if key.endswith("_index"):
            values[key] = int(match.group(1))
            values[key.replace("_index", "_total")] = int(match.group(2))
        else:
            values[key] = int(match.group(1))
    return values


def _work_count_text(counts: dict[str, int], delta: dict[str, Any], color: bool) -> str:
    parts: list[str] = []
    if "company_candidates" in counts:
        parts.append(
            "この会社候補="
            + _delta_value(
                counts["company_candidates"],
                int(delta.get("work_company_candidates") or 0),
                color,
            )
        )
    if "window_new" in counts:
        parts.append(
            "窓内新規="
            + _delta_value(
                counts["window_new"],
                int(delta.get("work_window_new") or 0),
                color,
            )
        )
    if "window_total" in counts:
        parts.append(
            "窓内累計="
            + _delta_value(
                counts["window_total"],
                int(delta.get("work_window_total") or 0),
                color,
            )
        )
    if "new_candidates" in counts:
        parts.append(
            "新規候補="
            + _delta_value(
                counts["new_candidates"],
                int(delta.get("work_new_candidates") or 0),
                color,
            )
        )
    if "total_candidates" in counts:
        parts.append(
            "累計="
            + _delta_value(
                counts["total_candidates"],
                int(delta.get("work_total_candidates") or 0),
                color,
            )
        )
    if "detail_index" in counts:
        parts.append(
            "詳細="
            + _delta_value(
                counts["detail_index"],
                int(delta.get("work_detail_index") or 0),
                color,
            )
            + f"/{counts.get('detail_total', '?')}"
        )
    if "claims_index" in counts:
        parts.append(
            "claims="
            + _delta_value(
                counts["claims_index"],
                int(delta.get("work_claims_index") or 0),
                color,
            )
            + f"/{counts.get('claims_total', '?')}"
        )
    return " ".join(parts)


def _bar(percent: float, width: int = 24, color: str = "green", enabled: bool = True) -> str:
    pct = max(0.0, min(100.0, percent))
    filled = int(round(width * pct / 100.0))
    raw = "#" * filled + "-" * (width - filled)
    return "[" + _ansi(raw, color, enabled) + f"] {pct:5.1f}%"


def _phase_label(phase: str) -> str:
    labels = {
        "COMPANY SEARCH": "1/3 会社検索: どの会社に何件あるか探している",
        "DETAIL ENRICHMENT": "2/3 詳細補完: biblio/abstract/引用/legalを集めている",
        "SCORING / FINALIZING": "3/3 保存・AI判定: 正規化DB保存とAI絞り込み中",
        "STOPPED": "停止中",
    }
    return labels.get(phase, phase)


def _window_span_days(window: dict[str, Any]) -> int:
    try:
        start = datetime.fromisoformat(str(window.get("window_start") or ""))
        end = datetime.fromisoformat(str(window.get("window_end") or ""))
    except ValueError:
        return 0
    return (end.date() - start.date()).days + 1


def _phase_progress(status: dict[str, Any], window: dict[str, Any]) -> dict[str, float]:
    target = int(status.get("target_companies") or 0)
    company_done = int(window.get("company_done") or 0)
    staged = int(window.get("staged_patents") or 0)
    normalized = int(window.get("normalized_patents") or 0)
    touched = int(window.get("cache_touched_patents") or 0)
    runtime = status.get("runtime") or {}
    runtime_phase = str(runtime.get("phase") or "")
    runtime_status = str(runtime.get("status") or "")
    runtime_company_total = int(runtime.get("company_total") or 0)
    runtime_company_index = int(runtime.get("company_index") or 0)
    company_progress = _percent(company_done, target)
    if (
        runtime_phase == "company_search"
        and runtime_status == "running"
        and runtime_company_total > 0
        and runtime_company_index > 0
    ):
        company_progress = _percent(runtime_company_index, runtime_company_total)
    return {
        "company": company_progress,
        "detail": _percent(touched, staged),
        "final": _percent(normalized, staged),
    }


def _overall_window_progress(status: dict[str, Any], window: dict[str, Any]) -> float:
    phase = str(window.get("phase") or "")
    progress = _phase_progress(status, window)
    if phase == "COMPANY SEARCH":
        return progress["company"] * 0.35
    if phase == "DETAIL ENRICHMENT":
        return 35.0 + progress["detail"] * 0.55
    if phase == "SCORING / FINALIZING":
        return 90.0 + progress["final"] * 0.10
    if str(window.get("status") or "") == "completed":
        return 100.0
    return 0.0


def _directory_size(path: Path) -> int:
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        if result.returncode == 0 and result.stdout:
            return int(result.stdout.split()[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


def _target_companies(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            return [
                {
                    "company_id": str(row.get("company_id") or ""),
                    "company_name": str(row.get("company_name") or ""),
                    "ticker": str(row.get("ticker") or ""),
                }
                for row in rows
                if str(row.get("target", "1")).strip().lower()
                not in {"", "0", "false", "no", "off"}
            ]
    except OSError:
        return []


def _snapshot_path(output_dir: Path) -> Path:
    return output_dir / "patent_status_last.json"


def _snapshot_from_status(status: dict[str, Any]) -> dict[str, Any]:
    window = status.get("window") or {}
    runtime = status.get("runtime") or {}
    last_progress = status.get("last_progress") or {}
    counts = _extract_progress_counts(
        str(runtime.get("message") or "") + " " + str(last_progress.get("message") or "")
    )
    runtime_window_total = int(runtime.get("window_total_records") or counts.get("window_total") or 0)
    runtime_window_new = int(runtime.get("window_new_records") or counts.get("window_new") or 0)
    runtime_company_candidates = int(
        runtime.get("company_candidate_records") or counts.get("company_candidates") or 0
    )
    return {
        "checked_at": status.get("checked_at", ""),
        "phase": window.get("phase", ""),
        "company_done": int(window.get("company_done") or 0),
        "skipped_stalled_companies": int(window.get("skipped_stalled_companies") or 0),
        "staged_patents": int(window.get("staged_patents") or 0),
        "exact_window_staged_patents": int(window.get("exact_window_staged_patents") or 0),
        "normalized_patents": int(window.get("normalized_patents") or 0),
        "cache_touched_patents": int(window.get("cache_touched_patents") or 0),
        "raw_observations": int(window.get("raw_observations") or 0),
        "requests_last_10m": int(window.get("requests_last_10m") or 0),
        "latest_observation_age_seconds": int(window.get("latest_observation_age_seconds") or 0),
        "latest_observation": window.get("latest_observation", ""),
        "storage_bytes": int(status.get("storage_bytes") or 0),
        "db_age_seconds": int(status.get("db_age_seconds") or 0),
        "state_age_seconds": int(status.get("state_age_seconds") or 0),
        "work_company_candidates": runtime_company_candidates,
        "work_window_new": runtime_window_new,
        "work_window_total": runtime_window_total,
        "work_new_candidates": int(counts.get("new_candidates") or 0),
        "work_total_candidates": int(counts.get("total_candidates") or 0),
        "work_detail_index": int(counts.get("detail_index") or 0),
        "work_claims_index": int(counts.get("claims_index") or 0),
    }


def _attach_delta(status: dict[str, Any], previous: dict[str, Any], now: datetime) -> None:
    if not previous:
        status["delta"] = {"available": False}
        return
    current = _snapshot_from_status(status)
    previous_checked = str(previous.get("checked_at") or "")
    previous_time = _parse_iso(previous_checked)
    elapsed = (
        max(0, int((now - previous_time.astimezone(now.tzinfo)).total_seconds()))
        if previous_time
        else 0
    )
    status["delta"] = {
        "available": True,
        "elapsed_seconds": elapsed,
        "company_done": current["company_done"] - int(previous.get("company_done") or 0),
        "skipped_stalled_companies": current["skipped_stalled_companies"] - int(previous.get("skipped_stalled_companies") or 0),
        "staged_patents": current["staged_patents"] - int(previous.get("staged_patents") or 0),
        "exact_window_staged_patents": current["exact_window_staged_patents"] - int(previous.get("exact_window_staged_patents") or 0),
        "normalized_patents": current["normalized_patents"] - int(previous.get("normalized_patents") or 0),
        "cache_touched_patents": current["cache_touched_patents"] - int(previous.get("cache_touched_patents") or 0),
        "raw_observations": current["raw_observations"] - int(previous.get("raw_observations") or 0),
        "requests_last_10m": current["requests_last_10m"] - int(previous.get("requests_last_10m") or 0),
        "latest_observation_age_seconds": current["latest_observation_age_seconds"] - int(previous.get("latest_observation_age_seconds") or 0),
        "storage_bytes": current["storage_bytes"] - int(previous.get("storage_bytes") or 0),
        "db_age_seconds": current["db_age_seconds"] - int(previous.get("db_age_seconds") or 0),
        "state_age_seconds": current["state_age_seconds"] - int(previous.get("state_age_seconds") or 0),
        "work_company_candidates": current["work_company_candidates"] - int(previous.get("work_company_candidates") or 0),
        "work_window_new": current["work_window_new"] - int(previous.get("work_window_new") or 0),
        "work_window_total": current["work_window_total"] - int(previous.get("work_window_total") or 0),
        "work_new_candidates": current["work_new_candidates"] - int(previous.get("work_new_candidates") or 0),
        "work_total_candidates": current["work_total_candidates"] - int(previous.get("work_total_candidates") or 0),
        "work_detail_index": current["work_detail_index"] - int(previous.get("work_detail_index") or 0),
        "work_claims_index": current["work_claims_index"] - int(previous.get("work_claims_index") or 0),
    }


def _write_snapshot(output_dir: Path, status: dict[str, Any]) -> None:
    try:
        _snapshot_path(output_dir).write_text(
            json.dumps(_snapshot_from_status(status), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _attach_diagnosis(status: dict[str, Any]) -> None:
    window = status.get("window") or {}
    delta = status.get("delta") or {}
    notes: list[str] = []
    severity = "OK"
    db_age = status.get("db_age_seconds")
    state_age = status.get("state_age_seconds")
    epo_age = window.get("latest_observation_age_seconds")
    runtime = status.get("runtime") or {}
    runtime_age = _age_seconds(
        str(runtime.get("updated_at") or ""),
        datetime.now().astimezone(),
    )
    epo_recent = (
        (isinstance(epo_age, int) and epo_age <= 600)
        or int(window.get("requests_last_10m") or 0) > 0
    )
    elapsed = int(delta.get("elapsed_seconds") or 0)
    moved = any(
        int(delta.get(key) or 0) > 0
        for key in ("company_done", "staged_patents", "normalized_patents", "cache_touched_patents", "raw_observations")
    )

    if str(status.get("service") or "") != "active":
        severity = "STOPPED"
        notes.append("systemd service is not active")
    if isinstance(db_age, int) and db_age > 600:
        severity = "STUCK" if severity == "OK" else severity
        notes.append("DB file timestamp is old; collector heartbeat may be stale")
    if isinstance(state_age, int) and state_age > 600:
        severity = "WARN" if severity == "OK" else severity
        notes.append("collector_state.json is old")
    if (
        runtime
        and runtime.get("status") == "running"
        and isinstance(runtime_age, int)
        and runtime_age > 600
        and not epo_recent
    ):
        severity = "STUCK" if severity in {"OK", "WATCH", "WARN"} else severity
        notes.append("current runtime company has been running for over 10 minutes")
    elif runtime and runtime.get("status") == "running" and isinstance(runtime_age, int) and runtime_age > 600:
        notes.append("runtime heartbeat is old, but EPO observations are recent")

    if delta.get("available") and elapsed >= 60:
        if moved:
            notes.append("progress changed since previous patent-status")
        else:
            notes.append("no counter changed since previous patent-status")
            if elapsed >= 1200:
                severity = "STUCK" if severity in {"OK", "WARN"} else severity
            elif severity == "OK":
                severity = "WATCH"

    if isinstance(epo_age, int):
        if epo_age > 1800:
            severity = "STUCK" if severity in {"OK", "WATCH", "WARN"} else severity
            notes.append("latest EPO observation is older than 30 minutes")
        elif epo_age > 600:
            severity = "WATCH" if severity == "OK" else severity
            notes.append("latest EPO observation is older than 10 minutes")
        elif int(window.get("requests_last_10m") or 0) > 0:
            notes.append("EPO observations are recent")

    checkpoint = status.get("checkpoint") or {}
    if checkpoint.get("stopped_by_throttle"):
        severity = "WARN" if severity == "OK" else severity
        notes.append("checkpoint says EPO throttle/robot detection paused the search")
    if checkpoint.get("throttle_pause_count"):
        notes.append(f"checkpoint throttle pauses={checkpoint.get('throttle_pause_count')}")
    if checkpoint.get("failure_count"):
        severity = "WARN" if severity == "OK" else severity
        notes.append(f"checkpoint failures={checkpoint.get('failure_count')}")

    if not notes:
        notes.append("no obvious stall signal")
    status["diagnosis"] = {
        "severity": severity,
        "summary": "; ".join(notes[:4]),
        "notes": notes,
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(sql, parameters).fetchone()
    return row[0] if row else 0


def _cache_progress(cache_path: Path, publication_numbers: set[str]) -> tuple[int, Counter[str]]:
    if not cache_path.exists() or not publication_numbers:
        return 0, Counter()
    normalized = {re.sub(r"[^0-9A-Z]", "", value.upper()) for value in publication_numbers}
    touched: set[str] = set()
    constituents: Counter[str] = Counter()
    connection = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True, timeout=10)
    try:
        values = sorted(normalized)
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT publication_number,constituent FROM epo_detail_cache "
                f"WHERE publication_number IN ({placeholders})",
                chunk,
            )
            for publication_number, constituent in rows:
                touched.add(str(publication_number))
                constituents[str(constituent)] += 1
    except sqlite3.Error:
        return 0, Counter()
    finally:
        connection.close()
    return len(touched), constituents


def collect_status(output_dir: Path, company_master: Path) -> dict[str, Any]:
    now = datetime.now().astimezone()
    db_path = output_dir / "collector.sqlite3"
    cache_path = output_dir / "epo_detail_cache.sqlite3"
    state_path = output_dir / "collector_state.json"
    checkpoint_path = output_dir / "company_search_checkpoint.json"
    previous_snapshot = _read_json_file(_snapshot_path(output_dir))
    service_state = _systemctl("is-active", SERVICE) or "unknown"
    properties = {}
    for line in _systemctl(
        "show", SERVICE, "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveEnterTimestamp"
    ).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value

    state = _read_json_file(state_path)
    checkpoint = _read_json_file(checkpoint_path)
    target_companies = _target_companies(company_master)
    state_age, state_age_seconds = _file_age(state_path, now)
    checkpoint_age, checkpoint_age_seconds = _file_age(checkpoint_path, now)
    db_age, db_age_seconds = _file_age(db_path, now)

    result: dict[str, Any] = {
        "checked_at": now.isoformat(timespec="seconds"),
        "service": service_state,
        "pid": properties.get("MainPID", "0"),
        "restarts": int(properties.get("NRestarts", "0") or 0),
        "active_since": properties.get("ActiveEnterTimestamp", ""),
        "state_mode": str(state.get("mode") or "unknown"),
        "state_age": state_age,
        "state_age_seconds": state_age_seconds,
        "runtime": dict(state.get("runtime") or {}),
        "last_progress": dict(state.get("last_progress") or {}),
        "next_end_date": str(state.get("next_end_date") or ""),
        "configured_rate": int(state.get("collector_ops_requests_per_minute") or 0),
        "target_companies": len(target_companies),
        "checkpoint": {
            "exists": bool(checkpoint),
            "age": checkpoint_age,
            "age_seconds": checkpoint_age_seconds,
            "completed_in_checkpoint": len(checkpoint.get("companies") or []),
            "unique_records": int(checkpoint.get("unique_records") or 0),
            "failure_count": int(checkpoint.get("failure_count") or 0),
            "throttle_pause_count": int(checkpoint.get("throttle_pause_count") or 0),
            "stopped_by_throttle": bool(checkpoint.get("stopped_by_throttle")),
            "stopped_by_request": bool(checkpoint.get("stopped_by_request")),
            "throttle_error": str(checkpoint.get("throttle_error") or ""),
            "last_completed_companies": (checkpoint.get("companies") or [])[-5:],
        },
        "window": None,
        "completed_windows": 0,
        "completed_days": 0,
        "total_records_seen": int(state.get("total_records_seen") or 0),
        "db_age": db_age,
        "db_age_seconds": db_age_seconds,
    }
    if not db_path.exists():
        result["health"] = "ERROR: collector.sqlite3 is missing"
        _attach_delta(result, previous_snapshot, now)
        result["diagnosis"] = {
            "severity": "STOPPED",
            "summary": "collector.sqlite3 is missing; status cannot inspect backfill progress",
            "notes": ["collector.sqlite3 is missing"],
        }
        return result
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        if _table_exists(connection, "collector_windows"):
            result["completed_windows"] = int(
                _scalar(connection, "SELECT COUNT(*) FROM collector_windows WHERE status='completed'") or 0
            )
            result["completed_days"] = int(
                _scalar(
                    connection,
                    "SELECT COALESCE(SUM(julianday(window_end)-julianday(window_start)+1),0) "
                    "FROM collector_windows WHERE status='completed'",
                )
                or 0
            )
            window = connection.execute(
                "SELECT * FROM collector_windows "
                "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, started_at DESC LIMIT 1"
            ).fetchone()
        else:
            window = None

        if window:
            window_data = dict(window)
            start = str(window_data.get("window_start") or "")
            end = str(window_data.get("window_end") or "")
            parameters = (start, end)
            runtime = result.get("runtime") or {}
            runtime_phase = str(runtime.get("phase") or "")
            runtime_status = str(runtime.get("status") or "")
            company_done = int(
                _scalar(
                    connection,
                    "SELECT COUNT(DISTINCT company_id) FROM collector_company_progress "
                    "WHERE window_end=? AND status IN ('completed', 'skipped_stalled')",
                    (end,),
                )
                or 0
            ) if _table_exists(connection, "collector_company_progress") else 0
            skipped_stalled = int(
                _scalar(
                    connection,
                    "SELECT COUNT(*) FROM collector_company_progress "
                    "WHERE window_end=? AND status='skipped_stalled'",
                    (end,),
                )
                or 0
            ) if _table_exists(connection, "collector_company_progress") else 0
            exact_staged_count = int(
                _scalar(
                    connection,
                    "SELECT COUNT(*) FROM collector_staged_records WHERE window_start=? AND window_end=?",
                    parameters,
                )
                or 0
            ) if _table_exists(connection, "collector_staged_records") else 0
            staged_rows = connection.execute(
                "SELECT record_json FROM collector_staged_records "
                "WHERE window_end=?",
                (end,),
            ).fetchall() if _table_exists(connection, "collector_staged_records") else []
            publications: set[str] = set()
            for row in staged_rows:
                try:
                    publication = str(json.loads(row[0]).get("publication_number") or "")
                except (TypeError, ValueError):
                    publication = ""
                if publication:
                    publications.add(publication)
            normalized = int(
                _scalar(
                    connection,
                    "SELECT COUNT(*) FROM normalized_patent_records "
                    "WHERE query_window_end=?",
                    (end,),
                )
                or 0
            ) if _table_exists(connection, "normalized_patent_records") else 0
            touched, constituents = _cache_progress(cache_path, publications)

            observations = 0
            newest = ""
            recent = 0
            latest_rows: list[dict[str, Any]] = []
            if _table_exists(connection, "epo_raw_observations"):
                row = connection.execute(
                    "SELECT COUNT(*),MAX(retrieved_at) FROM epo_raw_observations "
                    "WHERE query_window_end=?",
                    (end,),
                ).fetchone()
                observations, newest = int(row[0] or 0), str(row[1] or "")
                cutoff = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
                recent = int(
                    _scalar(
                        connection,
                        "SELECT COUNT(*) FROM epo_raw_observations WHERE retrieved_at>=?",
                        (cutoff,),
                    )
                    or 0
                )
                latest_rows = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT retrieved_at, company_id, company_name, publication_number, constituent
                        FROM epo_raw_observations
                        WHERE query_window_end=?
                        ORDER BY retrieved_at DESC
                        LIMIT 5
                        """,
                        (end,),
                    ).fetchall()
                ]

            target = int(result["target_companies"] or 0)
            status = str(window_data.get("status") or "")
            if service_state != "active":
                phase = "STOPPED"
            elif status != "running":
                phase = status.upper() or "IDLE"
            elif runtime_status == "running" and runtime_phase == "company_search":
                phase = "COMPANY SEARCH"
            elif runtime_status == "running" and runtime_phase == "detail_enrichment":
                phase = "DETAIL ENRICHMENT"
            elif runtime_status == "running" and runtime_phase == "scoring_finalizing":
                phase = "SCORING / FINALIZING"
            elif target and company_done < target:
                phase = "COMPANY SEARCH"
            elif len(staged_rows) and normalized < len(staged_rows):
                phase = "DETAIL ENRICHMENT"
            else:
                phase = "SCORING / FINALIZING"
            next_company = {}
            if phase == "COMPANY SEARCH" and company_done < len(target_companies):
                next_company = target_companies[company_done]
            recent_completed_companies: list[dict[str, Any]] = []
            recent_skipped_companies: list[dict[str, Any]] = []
            if _table_exists(connection, "collector_company_progress"):
                recent_completed_companies = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT completed_at, company_id, company_name, record_count
                        FROM collector_company_progress
                        WHERE window_end=? AND status='completed'
                        ORDER BY completed_at DESC
                        LIMIT 5
                        """,
                        (end,),
                    ).fetchall()
                ]
            if _table_exists(connection, "collector_company_skip_history"):
                recent_skipped_companies = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT skipped_at, company_id, company_name, reason, elapsed_seconds,
                               actual_window_start, actual_window_end
                        FROM collector_company_skip_history
                        WHERE window_end=?
                        ORDER BY skipped_at DESC
                        LIMIT 5
                        """,
                        (end,),
                    ).fetchall()
                ]

            result["window"] = {
                **window_data,
                "phase": phase,
                "company_done": company_done,
                "skipped_stalled_companies": skipped_stalled,
                "staged_patents": len(staged_rows),
                "exact_window_staged_patents": exact_staged_count,
                "normalized_patents": normalized,
                "cache_touched_patents": touched,
                "cache_constituents": dict(sorted(constituents.items())),
                "raw_observations": observations,
                "latest_observation": newest,
                "latest_observation_age": _age(newest, now),
                "latest_observation_age_seconds": _age_seconds(newest, now),
                "requests_last_10m": recent,
                "effective_requests_per_minute": recent / 10.0,
                "latest_observations": latest_rows,
                "next_company_guess": next_company,
                "recent_completed_companies": recent_completed_companies,
                "recent_skipped_companies": recent_skipped_companies,
            }
    finally:
        connection.close()

    result["storage_bytes"] = _directory_size(output_dir)
    try:
        usage = shutil.disk_usage(output_dir)
        result["disk_total_bytes"] = usage.total
        result["disk_free_bytes"] = usage.free
        result["disk_used_percent"] = (usage.used * 100.0 / usage.total) if usage.total else 0.0
    except OSError:
        result["disk_total_bytes"] = 0
        result["disk_free_bytes"] = 0
        result["disk_used_percent"] = 0.0

    _attach_delta(result, previous_snapshot, now)
    _attach_diagnosis(result)
    _write_snapshot(output_dir, result)

    if service_state != "active":
        result["health"] = "CHECK: service is not active"
    elif result["restarts"]:
        result["health"] = "CHECK: systemd restart count is non-zero"
    elif result.get("window") and result["window"].get("error"):
        result["health"] = "CHECK: current window has an error"
    elif (result.get("diagnosis") or {}).get("severity") == "STUCK":
        result["health"] = "CHECK: likely stalled"
    else:
        result["health"] = "OK"
    return result


def print_status(status: dict[str, Any], color: bool = True) -> None:
    print("=== Patent Backfill Status ===")
    print(f"Checked    : {status['checked_at']}")
    print(
        f"Service    : {status['service'].upper()}  pid={status['pid']} "
        f"restarts={status['restarts']}"
    )
    window = status.get("window")
    delta = status.get("delta") or {}
    delta_available = bool(delta.get("available"))
    if window:
        phase = str(window.get("phase") or "")
        progress = _phase_progress(status, window)
        overall_pct = _overall_window_progress(status, window)
        span_days = _window_span_days(window)
        active_color = "yellow" if phase == "COMPANY SEARCH" else "cyan" if phase == "DETAIL ENRICHMENT" else "green"
        print(f"Now        : {_ansi(_phase_label(phase), active_color, color)}")
        print(f"30-day job : {_bar(overall_pct, color=active_color, enabled=color)}  この窓全体")
        print("Steps      :")
        company_marker = " <-- NOW" if phase == "COMPANY SEARCH" else ""
        detail_marker = " <-- NOW" if phase == "DETAIL ENRICHMENT" else ""
        final_marker = " <-- NOW" if phase == "SCORING / FINALIZING" else ""
        print(
            "  1. 会社検索   "
            + _bar(progress["company"], width=18, color="yellow" if phase == "COMPANY SEARCH" else "green", enabled=color)
            + _ansi(company_marker, "yellow", color)
        )
        print(
            "  2. 詳細補完   "
            + _bar(progress["detail"], width=18, color="cyan" if phase == "DETAIL ENRICHMENT" else "green", enabled=color)
            + _ansi(detail_marker, "cyan", color)
        )
        print(
            "  3. 保存/AI    "
            + _bar(progress["final"], width=18, color="green", enabled=color)
            + _ansi(final_marker, "green", color)
        )
        print(f"Phase      : {phase}")
        print(
            f"Window     : {window.get('window_start', '?')} .. {window.get('window_end', '?')} "
            f"({span_days or '?'} days, {window.get('status', '?')}, started {window.get('started_at', '?')})"
        )
        target = int(status.get("target_companies") or 0)
        company_done = int(window.get("company_done") or 0)
        company_pct = _percent(company_done, target)
        skipped_stalled = int(window.get("skipped_stalled_companies") or 0)
        company_text = (
            _delta_value(company_done, int(delta.get("company_done") or 0), color)
            if delta_available
            else str(company_done)
        )
        skipped_text = ""
        if skipped_stalled:
            skipped_text = f" skipped_stalled={skipped_stalled}"
            if delta_available:
                skipped_text += " " + _delta_badge(int(delta.get("skipped_stalled_companies") or 0), color, invert=True)
        print(f"Companies  : stored={company_text}/{target or '?'} ({company_pct:.1f}%){skipped_text}")
        runtime = status.get("runtime") or {}
        if (
            phase == "COMPANY SEARCH"
            and str(runtime.get("phase") or "") == "company_search"
            and str(runtime.get("status") or "") == "running"
        ):
            current_index = int(runtime.get("company_index") or 0)
            current_total = int(runtime.get("company_total") or 0)
            if current_index and current_total:
                pass_pct = _percent(current_index, current_total)
                print(f"Search pass: current={current_index}/{current_total} ({pass_pct:.1f}%)")
        staged = int(window.get("staged_patents") or 0)
        exact_staged = int(window.get("exact_window_staged_patents") or 0)
        normalized = int(window.get("normalized_patents") or 0)
        touched = int(window.get("cache_touched_patents") or 0)
        detail_pct = _percent(touched, staged)
        staged_text = _delta_value(staged, int(delta.get("staged_patents") or 0), color) if delta_available else str(staged)
        normalized_text = _delta_value(normalized, int(delta.get("normalized_patents") or 0), color) if delta_available else str(normalized)
        touched_text = _delta_value(touched, int(delta.get("cache_touched_patents") or 0), color) if delta_available else str(touched)
        staged_note = f" exact30d={exact_staged}" if exact_staged and exact_staged != staged else ""
        if staged_note and delta_available:
            staged_note += " " + _delta_badge(int(delta.get("exact_window_staged_patents") or 0), color)
        print(
            f"Patents    : staged={staged_text}{staged_note} normalized={normalized_text} "
            f"detail-touched={touched_text}/{staged or '?'} ({detail_pct:.1f}%)"
        )
        window_count_parts = [
            "窓内累計="
            + (
                _delta_value(staged, int(delta.get("staged_patents") or 0), color)
                if delta_available
                else str(staged)
            )
        ]
        if exact_staged and exact_staged != staged:
            exact_text = (
                _delta_value(
                    exact_staged,
                    int(delta.get("exact_window_staged_patents") or 0),
                    color,
                )
                if delta_available
                else str(exact_staged)
            )
            window_count_parts.append(f"30日内={exact_text}")
        runtime_window_new = int(runtime.get("window_new_records") or 0)
        runtime_company_candidates = int(runtime.get("company_candidate_records") or 0)
        if runtime_window_new:
            window_count_parts.append(
                "直近新規="
                + (
                    _delta_value(runtime_window_new, int(delta.get("work_window_new") or 0), color)
                    if delta_available
                    else str(runtime_window_new)
                )
            )
        if runtime_company_candidates:
            window_count_parts.append(
                "この会社候補="
                + (
                    _delta_value(
                        runtime_company_candidates,
                        int(delta.get("work_company_candidates") or 0),
                        color,
                    )
                    if delta_available
                    else str(runtime_company_candidates)
                )
            )
        print("Window cnt : " + " ".join(window_count_parts))
        constituent_text = " ".join(
            f"{key}={value}" for key, value in window.get("cache_constituents", {}).items()
        )
        if constituent_text:
            print(f"Details    : {constituent_text}")
        print(
            f"EPO        : observations="
            f"{_delta_value(window.get('raw_observations', 0), int(delta.get('raw_observations') or 0), color) if delta_available else window.get('raw_observations', 0)} "
            f"latest={window.get('latest_observation_age', 'unknown')} "
            f"{_delta_badge(int(delta.get('latest_observation_age_seconds') or 0), color, suffix='s', invert=True) if delta_available else ''} "
            f"last10m="
            f"{_delta_value(window.get('requests_last_10m', 0), int(delta.get('requests_last_10m') or 0), color) if delta_available else window.get('requests_last_10m', 0)} "
            f"({window.get('effective_requests_per_minute', 0):.1f} req/min)"
        )
        if delta.get("available"):
            elapsed = int(delta.get("elapsed_seconds") or 0)
            print(
                f"Since last : {elapsed}s  "
                f"companies={_delta_badge(int(delta.get('company_done') or 0), color)} "
                f"staged={_delta_badge(int(delta.get('staged_patents') or 0), color)} "
                f"detail={_delta_badge(int(delta.get('cache_touched_patents') or 0), color)} "
                f"obs={_delta_badge(int(delta.get('raw_observations') or 0), color)} "
                f"normalized={_delta_badge(int(delta.get('normalized_patents') or 0), color)}"
            )
        else:
            print("Since last : first run; run patent-status again to see deltas")
        runtime = status.get("runtime") or {}
        if runtime:
            runtime_age = _age(str(runtime.get("updated_at") or ""), datetime.now().astimezone())
            index_text = ""
            if runtime.get("company_index") or runtime.get("company_total"):
                index_text = f"{runtime.get('company_index', '?')}/{runtime.get('company_total', '?')}"
            elif runtime.get("record_count"):
                index_text = f"records={runtime.get('record_count')}"
            current_bits = [
                str(runtime.get("phase") or ""),
                str(runtime.get("status") or ""),
                index_text,
                str(runtime.get("company_id") or ""),
                str(runtime.get("company_name") or ""),
            ]
            print(
                "Current   : "
                + " ".join(value for value in current_bits if value)
                + f" age={runtime_age} "
                + f"range={runtime.get('actual_window_start', '')}..{runtime.get('actual_window_end', '')}"
            )
            if runtime.get("message"):
                print(f"Work msg   : {runtime.get('message')}")
                work_counts = _extract_progress_counts(str(runtime.get("message") or ""))
                if work_counts:
                    print(f"Work count : {_work_count_text(work_counts, delta, color)}")
            if runtime.get("last_epo_at"):
                print(
                    f"Work EPO   : {runtime.get('last_epo_at', '')} "
                    f"{runtime.get('last_epo_company_id', '')} {runtime.get('last_epo_company_name', '')} "
                    f"{runtime.get('last_epo_publication_number', '')} "
                    f"{runtime.get('last_epo_endpoint', '')}"
                )
        last_progress = status.get("last_progress") or {}
        if last_progress.get("message"):
            print(
                f"Progress   : {_age(str(last_progress.get('updated_at') or ''), datetime.now().astimezone())} "
                f"{last_progress.get('message')}"
            )
            progress_counts = _extract_progress_counts(str(last_progress.get("message") or ""))
            if progress_counts:
                print(f"Prog count : {_work_count_text(progress_counts, delta, color)}")
        next_company = window.get("next_company_guess") or {}
        if next_company:
            print(
                f"Next guess : {next_company.get('company_id', '')} "
                f"{next_company.get('company_name', '')} "
                f"ticker={next_company.get('ticker', '')}"
            )
        recent_done = window.get("recent_completed_companies") or []
        if recent_done:
            latest_done = recent_done[0]
            print(
                f"Last done  : {latest_done.get('company_id', '')} "
                f"{latest_done.get('company_name', '')} "
                f"records={latest_done.get('record_count', 0)} "
                f"at={latest_done.get('completed_at', '')}"
            )
        recent_skipped = window.get("recent_skipped_companies") or []
        if recent_skipped:
            latest_skip = recent_skipped[0]
            print(
                f"Last skip  : {latest_skip.get('company_id', '')} "
                f"{latest_skip.get('company_name', '')} "
                f"elapsed={latest_skip.get('elapsed_seconds', 0)}s "
                f"range={latest_skip.get('actual_window_start', '')}..{latest_skip.get('actual_window_end', '')}"
            )
            if latest_skip.get("reason"):
                print(f"Skip why   : {latest_skip.get('reason')}")
        latest_observations = window.get("latest_observations") or []
        if latest_observations:
            latest = latest_observations[0]
            print(
                f"Last EPO   : {latest.get('retrieved_at', '')} "
                f"{latest.get('company_id', '')} {latest.get('company_name', '')} "
                f"{latest.get('publication_number', '')} {latest.get('constituent', '')}"
            )
        checkpoint = status.get("checkpoint") or {}
        if checkpoint.get("exists"):
            print(
                f"Checkpoint : age={checkpoint.get('age', 'unknown')} "
                f"companies={checkpoint.get('completed_in_checkpoint', 0)} "
                f"unique={checkpoint.get('unique_records', 0)} "
                f"failures={checkpoint.get('failure_count', 0)} "
                f"throttle_pauses={checkpoint.get('throttle_pause_count', 0)}"
            )
        print(
            f"Errors     : failures={window.get('epo_failure_count', 0)} "
            f"throttles={window.get('epo_throttle_count', 0)} "
            f"capped={window.get('capped_company_count', 0)}"
        )
    else:
        print("Phase      : no collection window found")
    print(
        f"Overall    : completed_windows={status.get('completed_windows', 0)} "
        f"completed_days={status.get('completed_days', 0)} "
        f"records_seen={status.get('total_records_seen', 0)} "
        f"next_end={status.get('next_end_date', '')}"
    )
    print(
        f"Storage    : collector={_human_size(int(status.get('storage_bytes', 0)))} "
        f"{_size_delta_badge(int(delta.get('storage_bytes') or 0), color) if delta_available else ''} "
        f"disk_free={_human_size(int(status.get('disk_free_bytes', 0)))} "
        f"disk_used={float(status.get('disk_used_percent', 0)):.1f}%"
    )
    print(
        f"DB update  : {status.get('db_age', 'unknown')} "
        f"{_delta_badge(int(delta.get('db_age_seconds') or 0), color, suffix='s', invert=True) if delta_available else ''}"
    )
    print(
        f"State file : {status.get('state_age', 'unknown')} "
        f"{_delta_badge(int(delta.get('state_age_seconds') or 0), color, suffix='s', invert=True) if delta_available else ''}"
    )
    diagnosis = status.get("diagnosis") or {}
    severity = str(diagnosis.get("severity") or "unknown")
    diagnosis_color = "green" if severity == "OK" else "yellow" if severity in {"WATCH", "WARN"} else "red"
    print(
        f"Diagnosis  : {_ansi(severity, diagnosis_color, color)} "
        f"{diagnosis.get('summary', '')}"
    )
    health = str(status.get("health", "unknown"))
    health_color = "green" if health.startswith("OK") else "red"
    print(f"Health     : {_ansi(health, health_color, color)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show durable EPO patent backfill progress.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--company-master", type=Path, default=DEFAULT_COMPANIES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()
    status = collect_status(args.output_dir, args.company_master)
    if args.json:
        print(json.dumps(status, ensure_ascii=True, indent=2, default=str))
    else:
        print_status(status, color=not args.no_color and "NO_COLOR" not in os.environ)
    return 0 if str(status.get("health", "")).startswith("OK") else 1


if __name__ == "__main__":
    raise SystemExit(main())
