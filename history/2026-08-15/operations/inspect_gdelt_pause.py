import json
import sqlite3


path = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
connection = sqlite3.connect(path)
connection.row_factory = sqlite3.Row
tables = {
    row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
state = {
    row["state_key"]: row["state_value"]
    for row in connection.execute("SELECT state_key,state_value FROM service_state")
}
latest = connection.execute(
    "SELECT started_at,status,executed_queries,http_429 FROM collection_runs ORDER BY started_at DESC LIMIT 1"
).fetchone()
ngram_rows = connection.execute("SELECT COUNT(*) FROM ngram_files").fetchone()[0] if "ngram_files" in tables else 0
print(json.dumps({"state": state, "latest_run": dict(latest) if latest else None, "ngram_rows": ngram_rows}, ensure_ascii=False, indent=2))
connection.close()
