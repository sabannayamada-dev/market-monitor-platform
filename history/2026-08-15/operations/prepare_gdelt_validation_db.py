import json
import sqlite3
from datetime import datetime, timezone


path = "/tmp/gdelt-pdca-20260816/emergency_validation.sqlite3"
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
paused = "2099-01-01T00:00:00+00:00"
state = {
    "interval_minutes": 120.0,
    "cooldown_hours": 1.0,
    "query_spacing_seconds": 20.0,
    "next_collection_at": paused,
    "cooldown_until": paused,
    "recovery_probe_pending": False,
    "stable_window_started_at": now,
    "stable_successful_runs": 0,
    "last_429_at": None,
    "last_change_reason": "temporary validation pause",
}

connection = sqlite3.connect(path)
with connection:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS service_state(
               state_key TEXT PRIMARY KEY,
               state_value TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """INSERT INTO service_state(state_key,state_value,updated_at) VALUES(?,?,?)
           ON CONFLICT(state_key) DO UPDATE SET
           state_value=excluded.state_value,updated_at=excluded.updated_at""",
        ("adaptive_control_v1", json.dumps(state), now),
    )
connection.close()
print(path)
