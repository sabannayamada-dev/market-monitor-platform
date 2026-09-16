import json
import sqlite3
from pathlib import Path


DATABASES = {
    "gdelt": Path("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"),
    "platform": Path("/var/lib/market-monitor/platform.sqlite3"),
    "patent": Path("/var/lib/patent-news-monitor/patent_monitor.sqlite3"),
    "deepl": Path("/var/lib/market-monitor/deepl_translation.sqlite3"),
    "stock_notifications": Path("/var/lib/stock-bottom-monitor/notifications.sqlite3"),
}


def scalar(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchone()[0]


for name, path in DATABASES.items():
    print(f"=== DB {name} path={path} bytes={path.stat().st_size if path.exists() else 0} ===")
    if not path.exists():
        continue
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for table in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        count = scalar(connection, f'SELECT COUNT(*) FROM "{table}"')
        print(f"TABLE {table} rows={count} columns={','.join(columns)}")
    connection.close()

print("=== GDELT TARGETED ===")
g = sqlite3.connect(f"file:{DATABASES['gdelt']}?mode=ro", uri=True)
g.row_factory = sqlite3.Row
queries = {
    "runs_by_status": "SELECT status, COUNT(*) n FROM runs WHERE started_at >= '2026-08-18T15:00:00' GROUP BY status ORDER BY status",
    "run_span": "SELECT MIN(started_at) first, MAX(started_at) last, COUNT(*) n FROM runs WHERE started_at >= '2026-08-18T15:00:00'",
    "query_outcomes": "SELECT outcome, COUNT(*) n FROM query_log WHERE requested_at >= '2026-08-18T15:00:00' GROUP BY outcome ORDER BY n DESC",
    "query_http": "SELECT http_status, COUNT(*) n FROM query_log WHERE requested_at >= '2026-08-18T15:00:00' GROUP BY http_status ORDER BY n DESC",
    "notifications": "SELECT kind, COUNT(*) n, MIN(sent_at) first, MAX(sent_at) last FROM notifications WHERE sent_at >= '2026-08-18 15:00:00' GROUP BY kind",
    "articles": "SELECT COUNT(*) n, MIN(first_seen_at) first, MAX(first_seen_at) last FROM articles WHERE first_seen_at >= '2026-08-18T15:00:00'",
    "candidates": "SELECT COUNT(*) n, MIN(created_at) first, MAX(created_at) last FROM candidates WHERE created_at >= '2026-08-18T15:00:00'",
    "emergency_candidates": "SELECT event_type,event_state,alert_status,COUNT(*) n FROM emergency_candidates WHERE created_at >= '2026-08-18T15:00:00' GROUP BY event_type,event_state,alert_status ORDER BY n DESC",
    "emergency_ai": "SELECT decision,COUNT(*) n FROM emergency_ai_reviews WHERE created_at >= '2026-08-18T15:00:00' GROUP BY decision ORDER BY n DESC",
}
for label, sql in queries.items():
    print(f"-- {label}")
    try:
        for row in g.execute(sql):
            print(json.dumps(dict(row), ensure_ascii=False, default=str))
    except sqlite3.Error as exc:
        print(f"SQL_ERROR {exc}")
g.close()

print("=== DEEPL TARGETED ===")
d = sqlite3.connect(f"file:{DATABASES['deepl']}?mode=ro", uri=True)
d.row_factory = sqlite3.Row
for row in d.execute("SELECT usage_date, characters FROM daily_usage WHERE usage_date >= '2026-08-19' ORDER BY usage_date"):
    print(json.dumps(dict(row), ensure_ascii=False))
print("translations", scalar(d, "SELECT COUNT(*) FROM translations"))
d.close()

print("=== PATENT OUTPUT SUMMARIES ===")
root = Path("/var/lib/patent-news-monitor/outputs")
for path in sorted(root.glob("2026*/run_summary.json")):
    if path.parent.name[:8] < "20260819":
        continue
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(path, "READ_ERROR", type(exc).__name__)
        continue
    selected = {key: data.get(key) for key in (
        "run_id", "source_count", "family_count", "matched_count", "scored_count",
        "gpt_count", "gemini_count", "important_count", "urgent_count", "rejected_count",
        "pending_count", "email_count", "duration_seconds"
    ) if key in data}
    print(path.parent.name, json.dumps(selected, ensure_ascii=False, default=str))

status = root / "daily_logs/latest_status.json"
if status.exists():
    print("=== PATENT LATEST STATUS ===")
    print(status.read_text(encoding="utf-8"))
