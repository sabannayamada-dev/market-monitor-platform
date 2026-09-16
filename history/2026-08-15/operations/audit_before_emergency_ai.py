import json
import sqlite3


connection = sqlite3.connect("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
connection.row_factory = sqlite3.Row
result = {
    "latest_runs": [dict(row) for row in connection.execute(
        "SELECT status,started_at,completed_at,http_429,other_errors,fetched_articles "
        "FROM collection_runs ORDER BY started_at DESC LIMIT 3"
    )],
    "openai_article_reviews": connection.execute(
        "SELECT COUNT(*) FROM ai_reviews WHERE provider='openai'"
    ).fetchone()[0],
    "emergency_candidates": connection.execute(
        "SELECT COUNT(*) FROM emergency_candidates"
    ).fetchone()[0],
    "emergency_alerts": connection.execute(
        "SELECT COUNT(*) FROM emergency_alerts"
    ).fetchone()[0],
    "emergency_ai_reviews": connection.execute(
        "SELECT COUNT(*) FROM emergency_ai_reviews"
    ).fetchone()[0],
    "latest_scouts": [dict(row) for row in connection.execute(
        "SELECT started_at,status,head_requests,discovered_files,scanned_files,downloaded_bytes,"
        "candidates_found,ready_alerts,error_json FROM emergency_scout_runs "
        "ORDER BY started_at DESC LIMIT 3"
    )],
    "next_states": [dict(row) for row in connection.execute(
        "SELECT state_key,state_value FROM service_state WHERE state_key IN "
        "('adaptive_control_v1','emergency_next_scan_at','emergency_ai_key_configured',"
        "'emergency_ai_self_test_v1')"
    )],
}
print(json.dumps(result, ensure_ascii=False, indent=2))
connection.close()
