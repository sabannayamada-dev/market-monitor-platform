import json
import sqlite3
from pathlib import Path


config = json.loads(Path("/opt/patent-news-monitor/app/gdelt_monitor_config.json").read_text(encoding="utf-8"))
connection = sqlite3.connect("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
state_rows = dict(connection.execute(
    "SELECT state_key,state_value FROM service_state WHERE state_key IN ('adaptive_control_v1','emergency_next_scan_at')"
))
adaptive = json.loads(state_rows["adaptive_control_v1"])
counts = {
    "articles": connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
    "ngram_files": connection.execute("SELECT COUNT(*) FROM ngram_files").fetchone()[0],
    "emergency_scout_runs": connection.execute("SELECT COUNT(*) FROM emergency_scout_runs").fetchone()[0],
    "emergency_candidates": connection.execute("SELECT COUNT(*) FROM emergency_candidates").fetchone()[0],
    "emergency_alerts": connection.execute("SELECT COUNT(*) FROM emergency_alerts").fetchone()[0],
}
latest = connection.execute(
    "SELECT status,started_at,http_429,other_errors FROM collection_runs ORDER BY started_at DESC LIMIT 1"
).fetchone()
connection.close()
print(json.dumps({
    "source": config["collector"]["source"],
    "lookback_minutes": config["collector"]["lookback_minutes"],
    "file_download_spacing_seconds": config["collector"]["file_download_spacing_seconds"],
    "fixed_interval_minutes": config["adaptive_control"]["fixed_interval_minutes"],
    "emergency_enabled": config["emergency_monitor"]["enabled"],
    "emergency_interval_minutes": config["emergency_monitor"]["interval_minutes"],
    "adaptive_interval_minutes": adaptive["interval_minutes"],
    "next_collection_at": adaptive["next_collection_at"],
    "cooldown_until": adaptive["cooldown_until"],
    "emergency_next_scan_at": state_rows["emergency_next_scan_at"],
    "counts": counts,
    "latest_run": latest,
}, ensure_ascii=False, indent=2))
