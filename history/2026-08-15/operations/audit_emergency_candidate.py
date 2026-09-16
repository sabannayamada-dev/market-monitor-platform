import json
import sqlite3


connection = sqlite3.connect("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT event_type,event_state,entity,event_key,title,domain,url,seen_date,discovered_at "
    "FROM emergency_candidates ORDER BY discovered_at DESC LIMIT 10"
).fetchall()
runs = connection.execute(
    "SELECT status,head_requests,scanned_files,downloaded_bytes,candidates_found,error_json "
    "FROM emergency_scout_runs ORDER BY started_at DESC LIMIT 2"
).fetchall()
print(json.dumps({"candidates": [dict(row) for row in rows], "runs": [dict(row) for row in runs]}, ensure_ascii=False, indent=2))
connection.close()
