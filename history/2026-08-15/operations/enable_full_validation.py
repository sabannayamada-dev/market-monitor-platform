import json
import sqlite3
from datetime import datetime, timedelta, timezone


path = "/tmp/gdelt-pdca-20260816/emergency_validation.sqlite3"
now = datetime.now(timezone.utc)
connection = sqlite3.connect(path)
with connection:
    row = connection.execute(
        "SELECT state_value FROM service_state WHERE state_key='adaptive_control_v1'"
    ).fetchone()
    if row is None:
        raise SystemExit("adaptive state is missing")
    state = json.loads(row[0])
    state["interval_minutes"] = 120.0
    state["next_collection_at"] = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
    state["cooldown_until"] = None
    state["last_change_reason"] = "temporary full-window validation"
    connection.execute(
        "UPDATE service_state SET state_value=?,updated_at=? WHERE state_key='adaptive_control_v1'",
        (json.dumps(state, ensure_ascii=False), now.isoformat(timespec="seconds")),
    )
connection.close()
print("ready")
