import json
import sqlite3
from datetime import datetime, timedelta, timezone


db_path = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
backup_path = "/opt/patent-news-monitor/backups/20260817_production_rollout/gdelt_monitor.sqlite3"
source = sqlite3.connect(db_path)
backup = sqlite3.connect(backup_path)
source.backup(backup)
backup.close()

now = datetime.now(timezone.utc)
jst = timezone(timedelta(hours=9))
local_now = now.astimezone(jst)
next_full_local = local_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
if next_full_local.hour % 2:
    next_full_local += timedelta(hours=1)
next_full = next_full_local.astimezone(timezone.utc)
minutes_to_boundary = 15 - (now.minute % 15)
next_scout = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_boundary)

with source:
    row = source.execute(
        "SELECT state_value FROM service_state WHERE state_key='adaptive_control_v1'"
    ).fetchone()
    if row is None:
        raise SystemExit("adaptive state missing")
    state = json.loads(row[0])
    state.update({
        "interval_minutes": 120.0,
        "cooldown_hours": 1.0,
        "next_collection_at": next_full.isoformat(timespec="seconds"),
        "cooldown_until": None,
        "recovery_probe_pending": False,
        "stable_window_started_at": now.isoformat(timespec="seconds"),
        "stable_successful_runs": 0,
        "last_429_at": None,
        "last_change_reason": "production rollout: fixed 120-minute cadence",
    })
    updated = now.isoformat(timespec="seconds")
    source.execute(
        "UPDATE service_state SET state_value=?,updated_at=? WHERE state_key='adaptive_control_v1'",
        (json.dumps(state, ensure_ascii=False), updated),
    )
    source.execute(
        "INSERT INTO service_state(state_key,state_value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",
        ("emergency_next_scan_at", next_scout.isoformat(timespec="seconds"), updated),
    )
source.close()
print(json.dumps({
    "next_full_utc": next_full.isoformat(timespec="seconds"),
    "next_full_jst": next_full_local.isoformat(timespec="seconds"),
    "next_scout_utc": next_scout.isoformat(timespec="seconds"),
}, ensure_ascii=False))
