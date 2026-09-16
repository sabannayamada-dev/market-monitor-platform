import sqlite3
from datetime import datetime, timedelta, timezone


path = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
now = datetime.now(timezone.utc)
with sqlite3.connect(path) as connection:
    connection.execute(
        "UPDATE service_state SET state_value=?,updated_at=? WHERE state_key='emergency_next_scan_at'",
        ((now - timedelta(seconds=1)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
print("ready")
