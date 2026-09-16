import json
import sqlite3
from pathlib import Path


def rows(db, sql, params=()):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def section(name, values):
    print(f"=== {name} ===")
    for value in values:
        print(json.dumps(value, ensure_ascii=False, default=str))


gdelt = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
since = "2026-08-18T15:00:00+00:00"
section("GDELT COLLECTION SUMMARY", rows(gdelt, """
SELECT status, COUNT(*) runs,
 SUM(planned_queries) planned, SUM(executed_queries) executed,
 SUM(successful_queries) successful, SUM(empty_responses) empty_responses,
 SUM(non_json_responses) non_json, SUM(http_403) http_403, SUM(http_429) http_429,
 SUM(timeouts) timeouts, SUM(other_errors) other_errors,
 SUM(fetched_articles) fetched, SUM(inserted_articles) inserted,
 SUM(duplicate_articles) duplicates, SUM(company_matches) company_matches,
 SUM(scored_candidates) scored, SUM(gpt_sent) gpt_sent, SUM(gpt_passed) gpt_passed,
 SUM(gemini_sent) gemini_sent, SUM(final_candidates) final_candidates,
 SUM(urgent_candidates) urgent_candidates, SUM(carried_over) carried_over
FROM collection_runs WHERE started_at >= ? GROUP BY status
""", (since,)))
section("GDELT COLLECTION DAILY", rows(gdelt, """
SELECT substr(datetime(started_at),1,10) day, COUNT(*) runs,
 SUM(executed_queries) executed, SUM(successful_queries) successful,
 SUM(http_429) http_429, SUM(timeouts) timeouts, SUM(other_errors) other_errors,
 SUM(fetched_articles) fetched, SUM(inserted_articles) inserted,
 SUM(company_matches) matches, SUM(scored_candidates) scored,
 SUM(gpt_sent) gpt_sent, SUM(final_candidates) final_candidates
FROM collection_runs WHERE started_at >= ? GROUP BY day ORDER BY day
""", (since,)))
section("GDELT QUERY STATUS", rows(gdelt, """
SELECT status, COALESCE(error_type,'') error_type, COUNT(*) n,
 ROUND(AVG(duration_seconds),2) avg_seconds, SUM(fetched_count) fetched, SUM(inserted_count) inserted
FROM query_runs WHERE run_id IN (SELECT run_id FROM collection_runs WHERE started_at >= ?)
GROUP BY status, error_type ORDER BY n DESC
""", (since,)))
section("GDELT COLLECTION ERRORS", rows(gdelt, """
SELECT started_at,status,error_json FROM collection_runs
WHERE started_at >= ? AND (status <> 'success' OR error_json NOT IN ('[]','{}','',NULL))
ORDER BY started_at
""", (since,)))
section("GDELT ARTICLE AI", rows(gdelt, """
SELECT provider,status,COUNT(*) n,ROUND(AVG(importance),1) avg_importance,
 SUM(CASE WHEN importance >= 70 THEN 1 ELSE 0 END) high
FROM ai_reviews WHERE reviewed_at >= ? GROUP BY provider,status ORDER BY provider,status
""", (since,)))
section("GDELT ARTICLE OUTPUT", rows(gdelt, """
SELECT
 (SELECT COUNT(*) FROM articles WHERE first_seen_at >= ?) new_articles,
 (SELECT COUNT(*) FROM article_company_matches WHERE matched_at >= ?) company_matches,
 (SELECT COUNT(*) FROM article_scores WHERE scored_at >= ?) scores,
 (SELECT COUNT(*) FROM ai_reviews WHERE reviewed_at >= ?) ai_reviews,
 (SELECT COUNT(*) FROM notifications WHERE sent_at >= '2026-08-19 00:00:00' AND kind='daily_digest') digests
""", (since,since,since,since)))

section("EMERGENCY SCOUT DAILY", rows(gdelt, """
SELECT substr(datetime(started_at),1,10) day, COUNT(*) runs,
 SUM(head_requests) heads, SUM(discovered_files) discovered_files,
 SUM(scanned_files) scanned_files, SUM(downloaded_bytes) bytes,
 SUM(candidates_found) candidates, SUM(ready_alerts) ready_alerts,
 SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success_runs,
 SUM(CASE WHEN status<>'success' THEN 1 ELSE 0 END) failed_runs
FROM emergency_scout_runs WHERE started_at >= ? GROUP BY day ORDER BY day
""", (since,)))
section("EMERGENCY SCOUT STATUS", rows(gdelt, """
SELECT status,COUNT(*) n, SUM(candidates_found) candidates,SUM(ready_alerts) ready_alerts
FROM emergency_scout_runs WHERE started_at >= ? GROUP BY status ORDER BY n DESC
""", (since,)))
section("EMERGENCY AI DISTRIBUTION", rows(gdelt, """
SELECT status,event_confirmed,same_event,is_speculation,COUNT(*) n,
 ROUND(AVG(market_impact),1) avg_impact,ROUND(AVG(urgency),1) avg_urgency,
 SUM(CASE WHEN market_impact>=85 AND urgency>=85 THEN 1 ELSE 0 END) threshold_pass
FROM emergency_ai_reviews WHERE reviewed_at >= ?
GROUP BY status,event_confirmed,same_event,is_speculation ORDER BY n DESC
""", (since,)))
section("EMERGENCY HIGH REVIEWS", rows(gdelt, """
SELECT reviewed_at,event_key,event_state,status,event_confirmed,same_event,is_speculation,
 market_impact,urgency,event_category,japanese_summary,reason
FROM emergency_ai_reviews WHERE reviewed_at >= ? AND market_impact>=85 AND urgency>=85
ORDER BY reviewed_at DESC LIMIT 30
""", (since,)))
section("EMERGENCY COUNTS", rows(gdelt, """
SELECT
 (SELECT COUNT(*) FROM emergency_candidates WHERE discovered_at>=?) candidates,
 (SELECT COUNT(*) FROM emergency_ai_reviews WHERE reviewed_at>=?) ai_reviews,
 (SELECT COUNT(*) FROM emergency_alerts WHERE alerted_at>=?) alerts
""", (since,since,since)))

platform = "/var/lib/market-monitor/platform.sqlite3"
section("PLATFORM RUNS", rows(platform, """
SELECT service_id,status,COUNT(*) n,
 SUM(CASE WHEN exit_code=0 THEN 1 ELSE 0 END) exit_zero,
 SUM(CASE WHEN exit_code IS NOT NULL AND exit_code<>0 THEN 1 ELSE 0 END) exit_nonzero,
 MIN(started_at) first,MAX(started_at) last
FROM service_runs WHERE started_at>=? GROUP BY service_id,status ORDER BY service_id,status
""", (since,)))
section("PLATFORM EVENTS", rows(platform, """
SELECT service_id,level,event_type,COUNT(*) n FROM service_events
WHERE created_at>=? GROUP BY service_id,level,event_type ORDER BY service_id,level,n DESC
""", (since,)))

patent = "/var/lib/patent-news-monitor/patent_monitor.sqlite3"
section("PATENT RUNS", rows(patent, """
SELECT run_id,started_at,completed_at,input_count,family_count,gpt_count,gemini_count,error_count
FROM runs WHERE started_at >= '2026-08-19' ORDER BY started_at
"""))
section("PATENT AI COST", rows(patent, """
SELECT service,model,COUNT(*) calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
 ROUND(SUM(cost_jpy),4) cost_jpy FROM ai_cost_ledger
WHERE created_at >= '2026-08-19' GROUP BY service,model ORDER BY service,model
"""))
section("PATENT SEARCH STATE", rows(patent, """
SELECT CASE WHEN last_error IS NULL OR last_error='' THEN 'ok' ELSE last_error END state,
 COUNT(*) n,MAX(attempt_count) max_attempts,MIN(last_success_at) oldest_success,MAX(last_success_at) newest_success
FROM company_search_state GROUP BY state ORDER BY n DESC
"""))
section("PATENT NOTIFICATIONS", rows(patent, """
SELECT 'batch' kind,channel,COUNT(*) n,MIN(sent_at) first,MAX(sent_at) last FROM notification_batches
WHERE sent_at>='2026-08-19' GROUP BY channel
UNION ALL
SELECT 'item',channel,COUNT(*),MIN(sent_at),MAX(sent_at) FROM notifications
WHERE sent_at>='2026-08-19' GROUP BY channel
"""))
section("PATENT OUTBOX", rows(patent, """
SELECT o.patent_id,o.queued_at,o.updated_at,o.run_id,o.decision,
 CASE WHEN n.patent_id IS NULL THEN 0 ELSE 1 END sent
FROM notification_outbox o LEFT JOIN notifications n ON n.patent_id=o.patent_id
ORDER BY o.queued_at
"""))

stock = "/var/lib/stock-bottom-monitor/notifications.sqlite3"
section("STOCK RUNS", rows(stock, """
SELECT substr(started_at,1,10) day,status,target_count,completed_count,updated_count,
 detected_count,new_signal_count,sent_count,error_count,started_at,completed_at
FROM monitor_runs WHERE started_at>='2026-08-19' ORDER BY started_at
"""))
section("STOCK SIGNAL STATUS", rows(stock, """
SELECT delivery_status,COUNT(*) n,MIN(first_seen_at) first,MAX(last_seen_at) last,
 SUM(CASE WHEN last_error IS NOT NULL AND last_error<>'' THEN 1 ELSE 0 END) with_error
FROM bottom_signals GROUP BY delivery_status
"""))

print("=== PATENT RUN SUMMARIES RAW ===")
for path in sorted(Path("/var/lib/patent-news-monitor/outputs").glob("2026*/run_summary.json")):
    if path.parent.name[:8] >= "20260819":
        print(path.parent.name, path.read_text(encoding="utf-8").replace("\n", " "))
