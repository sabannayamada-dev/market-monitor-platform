from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DATABASE = Path("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
JST = ZoneInfo("Asia/Tokyo")
CUTOFF = datetime.now(timezone.utc) - timedelta(days=8)


def parsed(value: str) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    result = datetime.fromisoformat(text)
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def main() -> int:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    cutoff = CUTOFF.isoformat(timespec="seconds")
    print("INTEGRITY=" + connection.execute("PRAGMA integrity_check").fetchone()[0])
    print(f"DATABASE_BYTES={DATABASE.stat().st_size}")

    daily: dict[str, Counter[str]] = defaultdict(Counter)
    statuses: Counter[str] = Counter()
    for row in connection.execute("SELECT * FROM collection_runs WHERE started_at>=? ORDER BY started_at", (cutoff,)):
        day = parsed(row["started_at"]).astimezone(JST).date().isoformat()
        stats = daily[day]
        stats["runs"] += 1
        statuses[str(row["status"])] += 1
        for name in (
            "planned_queries", "executed_queries", "successful_queries", "empty_responses",
            "non_json_responses", "http_403", "http_429", "timeouts", "other_errors",
            "fetched_articles", "inserted_articles", "duplicate_articles", "company_matches",
            "scored_candidates", "gpt_sent", "gpt_passed", "final_candidates", "urgent_candidates",
            "carried_over",
        ):
            stats[name] += int(row[name] or 0)
    print("COLLECTION_STATUSES=" + json.dumps(statuses, ensure_ascii=False))
    print("COLLECTION_DAILY=" + json.dumps({day: dict(value) for day, value in daily.items()}, ensure_ascii=False))

    query_errors = [dict(row) for row in connection.execute(
        """SELECT q.error_type,COUNT(*) AS count,MAX(q.error_message) AS sample
           FROM query_runs q
           JOIN collection_runs c ON c.run_id=q.run_id
           WHERE c.started_at>=? AND q.status<>'completed' AND q.error_type<>''
           GROUP BY q.error_type ORDER BY count DESC""",
        (cutoff,),
    )]
    print("QUERY_ERRORS=" + json.dumps(query_errors, ensure_ascii=False))

    scout_daily: dict[str, Counter[str]] = defaultdict(Counter)
    for row in connection.execute("SELECT * FROM emergency_scout_runs WHERE started_at>=? ORDER BY started_at", (cutoff,)):
        day = parsed(row["started_at"]).astimezone(JST).date().isoformat()
        stats = scout_daily[day]
        stats["runs"] += 1
        stats[str(row["status"])] += 1
        for name in ("head_requests", "discovered_files", "scanned_files", "downloaded_bytes", "candidates_found", "ready_alerts"):
            stats[name] += int(row[name] or 0)
        if str(row["error_json"] or "[]") != "[]":
            stats["runs_with_errors"] += 1
    print("SCOUT_DAILY=" + json.dumps({day: dict(value) for day, value in scout_daily.items()}, ensure_ascii=False))

    alerts = [dict(row) for row in connection.execute(
        "SELECT event_type,event_state,entity,alerted_at,evidence_json FROM emergency_alerts WHERE alerted_at>=? ORDER BY alerted_at",
        (cutoff,),
    )]
    print("EMERGENCY_ALERTS=" + json.dumps(alerts, ensure_ascii=False))

    reviews = Counter()
    for row in connection.execute("SELECT * FROM emergency_ai_reviews WHERE reviewed_at>=?", (cutoff,)):
        reviews["total"] += 1
        reviews["confirmed"] += int(row["event_confirmed"] or 0)
        reviews["approved_threshold"] += int(
            bool(row["event_confirmed"]) and bool(row["same_event"]) and not bool(row["is_speculation"])
            and float(row["market_impact"] or 0) >= 85 and float(row["urgency"] or 0) >= 85
        )
        reviews["input_tokens"] += int(row["input_tokens"] or 0)
        reviews["output_tokens"] += int(row["output_tokens"] or 0)
    print("EMERGENCY_AI=" + json.dumps(reviews, ensure_ascii=False))

    notifications = [dict(row) for row in connection.execute(
        "SELECT notification_key,kind,sent_at,article_ids_json FROM notifications ORDER BY sent_at DESC LIMIT 12"
    )]
    print("NOTIFICATIONS=" + json.dumps(notifications, ensure_ascii=False))

    global_rows = [dict(row) for row in connection.execute(
        "SELECT * FROM global_news_digests ORDER BY digest_date"
    )]
    global_summary = []
    problems = []
    for row in global_rows:
        try:
            items = json.loads(row["selected_json"] or "[]")
        except json.JSONDecodeError:
            items = []
            problems.append(f"{row['digest_date']}: invalid JSON")
        compact = []
        for item in items:
            compact.append({key: item.get(key) for key in (
                "title", "japanese_title", "summary", "importance", "reason", "topic", "domain", "selection_method"
            )})
            if not str(item.get("title") or "").strip():
                problems.append(f"{row['digest_date']}: empty title")
            if float(item.get("importance") or 0) <= 0:
                problems.append(f"{row['digest_date']}: non-positive importance")
            if item.get("selection_method") == "ai" and not str(item.get("summary") or "").strip():
                problems.append(f"{row['digest_date']}: AI item missing summary")
        if len(items) != 2:
            problems.append(f"{row['digest_date']}: selected {len(items)} items")
        if len({str(item.get('title_key') or item.get('title')) for item in items}) != len(items):
            problems.append(f"{row['digest_date']}: duplicate items")
        global_summary.append({
            "digest_date": row["digest_date"], "status": row["status"], "model": row["model"],
            "reviewed_at": row["reviewed_at"], "items": compact,
        })
    print("GLOBAL_NEWS_DIGESTS=" + json.dumps(global_summary, ensure_ascii=False))
    print("GLOBAL_NEWS_PROBLEMS=" + json.dumps(problems, ensure_ascii=False))
    candidate_count = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT title_key),MIN(discovered_at),MAX(discovered_at) FROM global_news_candidates"
    ).fetchone()
    print("GLOBAL_CANDIDATES=" + json.dumps({
        "rows": candidate_count[0], "distinct_titles": candidate_count[1],
        "first": candidate_count[2], "last": candidate_count[3],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
