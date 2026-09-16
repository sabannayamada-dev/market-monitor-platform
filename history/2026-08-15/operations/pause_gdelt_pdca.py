import json
import sqlite3
from datetime import datetime, timezone


path = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
state_key = "adaptive_control_v1"
backup_key = "pdca_pause_backup_adaptive_control_v1"
paused_until = "2099-01-01T00:00:00+00:00"
now = datetime.now(timezone.utc).isoformat(timespec="seconds")

connection = sqlite3.connect(path)
with connection:
    row = connection.execute(
        "SELECT state_value FROM service_state WHERE state_key=?", (state_key,)
    ).fetchone()
    if row is None:
        raise SystemExit("adaptive state not found")
    original = row[0]
    connection.execute(
        """INSERT INTO service_state(state_key,state_value,updated_at) VALUES(?,?,?)
           ON CONFLICT(state_key) DO UPDATE SET
           state_value=excluded.state_value,updated_at=excluded.updated_at""",
        (backup_key, original, now),
    )
    state = json.loads(original)
    state["next_collection_at"] = paused_until
    state["cooldown_until"] = paused_until
    state["last_change_reason"] = "PDCA一時停止: 実運用時刻のすり合わせ待ち"
    connection.execute(
        "UPDATE service_state SET state_value=?,updated_at=? WHERE state_key=?",
        (json.dumps(state, ensure_ascii=False), now, state_key),
    )
print(json.dumps({"paused": True, "paused_until": paused_until, "backup_key": backup_key}, ensure_ascii=False))
connection.close()
