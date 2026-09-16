import json
import sqlite3
from datetime import datetime


path = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row

print("=== SERVICE STATE ===")
for row in connection.execute("SELECT * FROM service_state ORDER BY state_key"):
    print(json.dumps(dict(row), ensure_ascii=False))

print("=== ADAPTIVE EVENTS ===")
for row in connection.execute(
    "SELECT * FROM adaptive_events WHERE event_at >= '2026-08-18T15:00:00+00:00' ORDER BY event_at"
):
    print(json.dumps(dict(row), ensure_ascii=False))

print("=== ADAPTIVE EVENT SUMMARY ===")
for row in connection.execute(
    "SELECT event_type,COUNT(*) n,MIN(event_at) first,MAX(event_at) last,"
    "MIN(new_interval_minutes) min_interval,MAX(new_interval_minutes) max_interval,"
    "MIN(new_cooldown_hours) min_cooldown,MAX(new_cooldown_hours) max_cooldown,"
    "MIN(new_query_spacing_seconds) min_spacing,MAX(new_query_spacing_seconds) max_spacing "
    "FROM adaptive_events WHERE event_at >= '2026-08-18T15:00:00+00:00' GROUP BY event_type"
):
    print(json.dumps(dict(row), ensure_ascii=False))

print("=== COLLECTION CADENCE ===")
for row in connection.execute(
    "SELECT started_at,completed_at,window_start,window_end,status,executed_queries,"
    "fetched_articles,inserted_articles FROM collection_runs "
    "WHERE started_at >= '2026-08-29T15:00:00+00:00' ORDER BY started_at"
):
    print(json.dumps(dict(row), ensure_ascii=False))

print("=== ARTICLE DELIVERY LATENCY ===")
rows = list(connection.execute(
    "SELECT a.article_id,a.title,a.seen_date,a.first_seen_at,a.domain,"
    "MIN(n.sent_at) sent_at FROM articles a "
    "LEFT JOIN notifications n ON instr(n.article_ids_json,a.article_id)>0 "
    "WHERE a.first_seen_at >= '2026-08-18T15:00:00+00:00' "
    "GROUP BY a.article_id ORDER BY a.first_seen_at"
))
for row in rows:
    item = dict(row)
    try:
        seen = datetime.fromisoformat(str(item["seen_date"]).replace("Z", "+00:00"))
        first = datetime.fromisoformat(str(item["first_seen_at"]).replace("Z", "+00:00"))
        item["capture_delay_minutes"] = round((first - seen).total_seconds() / 60, 1)
    except Exception:
        item["capture_delay_minutes"] = None
    try:
        first = datetime.fromisoformat(str(item["first_seen_at"]).replace("Z", "+00:00"))
        sent = datetime.fromisoformat(str(item["sent_at"]).replace("Z", "+00:00"))
        item["email_delay_hours"] = round((sent - first).total_seconds() / 3600, 1)
    except Exception:
        item["email_delay_hours"] = None
    print(json.dumps(item, ensure_ascii=False))

connection.close()
