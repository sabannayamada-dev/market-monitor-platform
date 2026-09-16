from __future__ import annotations

import json
import sqlite3


DATABASE = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"


def emit(label: str, rows: list[sqlite3.Row]) -> None:
    print(label + "=" + json.dumps([dict(row) for row in rows], ensure_ascii=False))


def main() -> int:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    emit(
        "LATEST_ALERTS",
        connection.execute(
            "SELECT alert_id,event_key,event_type,event_state,entity,alerted_at,evidence_json "
            "FROM emergency_alerts ORDER BY alerted_at DESC LIMIT 5"
        ).fetchall(),
    )
    emit(
        "LATEST_AI_REVIEWS",
        connection.execute(
            "SELECT review_id,evidence_key,event_key,event_state,status,event_confirmed,same_event,"
            "market_impact,urgency,is_speculation,event_category,affected_assets_json,japanese_summary,"
            "reason,reviewed_at FROM emergency_ai_reviews ORDER BY reviewed_at DESC LIMIT 5"
        ).fetchall(),
    )
    emit(
        "LATEST_CANDIDATES",
        connection.execute(
            "SELECT candidate_id,event_key,event_type,event_state,entity,title,url,domain,seen_date,"
            "ngram_stamp,discovered_at FROM emergency_candidates ORDER BY discovered_at DESC LIMIT 15"
        ).fetchall(),
    )
    emit(
        "LATEST_NOTIFICATIONS",
        connection.execute(
            "SELECT notification_key,kind,sent_at,article_ids_json FROM notifications "
            "ORDER BY sent_at DESC LIMIT 10"
        ).fetchall(),
    )
    emit(
        "LATEST_SCOUT_RUNS",
        connection.execute(
            "SELECT started_at,completed_at,status,head_requests,discovered_files,scanned_files,"
            "downloaded_bytes,candidates_found,ready_alerts,error_json FROM emergency_scout_runs "
            "ORDER BY started_at DESC LIMIT 10"
        ).fetchall(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
