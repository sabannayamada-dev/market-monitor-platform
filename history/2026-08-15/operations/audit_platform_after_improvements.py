import json
import sqlite3

path = "/var/lib/market-monitor/platform.sqlite3"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
print(json.dumps([
    dict(row) for row in connection.execute(
        "SELECT service_id,status,COUNT(*) count FROM service_runs GROUP BY service_id,status ORDER BY service_id,status"
    )
], ensure_ascii=False))
print(json.dumps([
    dict(row) for row in connection.execute(
        "SELECT service_id,run_id,started_at,completed_at,status,LENGTH(detail_json) detail_bytes "
        "FROM service_runs ORDER BY started_at DESC LIMIT 5"
    )
], ensure_ascii=False))
connection.close()
