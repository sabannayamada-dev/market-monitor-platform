import json
import sqlite3
from datetime import datetime, timedelta, timezone


path = "/tmp/emergency-ai-validation.sqlite3"
now = datetime.now(timezone.utc)
paused = "2099-01-01T00:00:00+00:00"
state = {
    "interval_minutes": 120.0,
    "cooldown_hours": 1.0,
    "query_spacing_seconds": 20.0,
    "next_collection_at": paused,
    "cooldown_until": paused,
    "recovery_probe_pending": False,
    "stable_window_started_at": now.isoformat(timespec="seconds"),
    "stable_successful_runs": 0,
    "last_429_at": None,
    "last_change_reason": "emergency AI validation pause",
}
connection = sqlite3.connect(path)
with connection:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS service_state("
        "state_key TEXT PRIMARY KEY,state_value TEXT NOT NULL,updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR REPLACE INTO service_state VALUES(?,?,?)",
        ("adaptive_control_v1", json.dumps(state), now.isoformat(timespec="seconds")),
    )
    connection.execute(
        "INSERT OR REPLACE INTO service_state VALUES(?,?,?)",
        (
            "emergency_next_scan_at",
            (now - timedelta(seconds=1)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ),
    )
connection.close()
print("ready")
