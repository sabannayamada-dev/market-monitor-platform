import json
import sqlite3

path = "/var/lib/stock-bottom-monitor/notifications.sqlite3"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
for row in connection.execute(
    "SELECT started_at,status,error_count,detail_json FROM monitor_runs "
    "WHERE started_at>='2026-08-19' ORDER BY started_at"
):
    detail = json.loads(row["detail_json"] or "{}")
    print(json.dumps({
        "started_at": row["started_at"],
        "status": row["status"],
        "error_count": row["error_count"],
        "errors": detail.get("errors"),
        "failed_symbols": detail.get("failed_symbols"),
        "cache": detail.get("cache"),
        "mail": detail.get("mail"),
    }, ensure_ascii=False))
connection.close()
