import json
import sqlite3
from pathlib import Path


def query(path, sql, params=()):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def show(label, values):
    print(f"=== {label} ===")
    for value in values:
        print(json.dumps(value, ensure_ascii=False, default=str))


since = "2026-08-18T15:00:00+00:00"
g = "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3"
show("EMERGENCY TOTAL", query(g, """
SELECT status,COUNT(*) runs,SUM(head_requests) heads,SUM(discovered_files) discovered,
SUM(scanned_files) scanned,ROUND(SUM(downloaded_bytes)/1048576.0,1) mib,
SUM(candidates_found) candidates,SUM(ready_alerts) ready
FROM emergency_scout_runs WHERE started_at>=? GROUP BY status
""", (since,)))
show("EMERGENCY DAILY", query(g, """
SELECT substr(datetime(started_at),1,10) day,COUNT(*) runs,SUM(scanned_files) scanned,
SUM(candidates_found) candidates,SUM(ready_alerts) ready
FROM emergency_scout_runs WHERE started_at>=? GROUP BY day ORDER BY day
""", (since,)))
show("EMERGENCY RUN ERRORS", query(g, """
SELECT status,COUNT(*) n FROM emergency_scout_runs
WHERE started_at>=? AND COALESCE(error_json,'[]') NOT IN ('[]','{}','') GROUP BY status
""", (since,)))
show("EMERGENCY AI", query(g, """
SELECT status,event_confirmed,same_event,is_speculation,COUNT(*) n,
ROUND(AVG(market_impact),1) impact,ROUND(AVG(urgency),1) urgency,
SUM(CASE WHEN market_impact>=85 AND urgency>=85 THEN 1 ELSE 0 END) high
FROM emergency_ai_reviews WHERE reviewed_at>=?
GROUP BY status,event_confirmed,same_event,is_speculation ORDER BY n DESC
""", (since,)))
show("EMERGENCY CATEGORY", query(g, """
SELECT event_category,COUNT(*) n,
SUM(CASE WHEN event_confirmed=1 THEN 1 ELSE 0 END) confirmed,
SUM(CASE WHEN market_impact>=85 AND urgency>=85 THEN 1 ELSE 0 END) high
FROM emergency_ai_reviews WHERE reviewed_at>=? GROUP BY event_category ORDER BY n DESC
""", (since,)))
show("EMERGENCY HIGH", query(g, """
SELECT reviewed_at,event_key,event_state,event_confirmed,same_event,is_speculation,
market_impact,urgency,event_category,japanese_summary
FROM emergency_ai_reviews WHERE reviewed_at>=? AND market_impact>=85 AND urgency>=85
ORDER BY reviewed_at DESC LIMIT 20
""", (since,)))
show("EMERGENCY OUTPUT", query(g, """
SELECT
(SELECT COUNT(*) FROM emergency_candidates WHERE discovered_at>=?) candidates,
(SELECT COUNT(*) FROM emergency_ai_reviews WHERE reviewed_at>=?) ai_reviews,
(SELECT COUNT(*) FROM emergency_alerts WHERE alerted_at>=?) alerts
""", (since,since,since)))

p = "/var/lib/market-monitor/platform.sqlite3"
show("PLATFORM RUNS", query(p, """
SELECT service_id,status,COUNT(*) n,
SUM(CASE WHEN exit_code=0 THEN 1 ELSE 0 END) ok,
SUM(CASE WHEN exit_code<>0 THEN 1 ELSE 0 END) bad,
MIN(started_at) first,MAX(started_at) last
FROM service_runs WHERE started_at>=? GROUP BY service_id,status ORDER BY service_id,status
""", (since,)))
show("PLATFORM WARNING EVENTS", query(p, """
SELECT service_id,level,event_type,COUNT(*) n FROM service_events
WHERE created_at>=? AND level NOT IN ('info','debug')
GROUP BY service_id,level,event_type ORDER BY service_id,n DESC
""", (since,)))

pat = "/var/lib/patent-news-monitor/patent_monitor.sqlite3"
show("PATENT RUNS", query(pat, """
SELECT run_id,started_at,completed_at,input_count,family_count,gpt_count,gemini_count,error_count
FROM runs WHERE started_at>='2026-08-19' ORDER BY started_at
"""))
show("PATENT SEARCH", query(pat, """
SELECT CASE WHEN COALESCE(last_error,'')='' THEN 'ok' ELSE last_error END state,
COUNT(*) n,MAX(attempt_count) max_attempts,MIN(last_success_at) oldest_success,MAX(last_success_at) newest_success
FROM company_search_state GROUP BY state ORDER BY n DESC
"""))
show("PATENT NOTIFICATIONS", query(pat, """
SELECT 'batch' kind,channel,COUNT(*) n,MIN(sent_at) first,MAX(sent_at) last
FROM notification_batches WHERE sent_at>='2026-08-19' GROUP BY channel
UNION ALL
SELECT 'item',channel,COUNT(*),MIN(sent_at),MAX(sent_at)
FROM notifications WHERE sent_at>='2026-08-19' GROUP BY channel
"""))
show("PATENT OUTBOX", query(pat, """
SELECT o.patent_id,o.queued_at,o.updated_at,o.run_id,o.decision,
CASE WHEN n.patent_id IS NULL THEN 0 ELSE 1 END sent
FROM notification_outbox o LEFT JOIN notifications n ON n.patent_id=o.patent_id
ORDER BY o.queued_at
"""))
show("PATENT AI COST", query(pat, """
SELECT service,model,COUNT(*) calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
ROUND(SUM(cost_jpy),3) cost_jpy FROM ai_cost_ledger
WHERE created_at>='2026-08-19' GROUP BY service,model ORDER BY service,model
"""))

print("=== PATENT OUTPUT COUNTS ===")
for path in sorted(Path("/var/lib/patent-news-monitor/outputs").glob("2026*/run_summary.json")):
    if path.parent.name[:8] < "20260819":
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({
        "run": path.parent.name,
        "counts": data.get("counts", {}),
        "matched": data.get("data_quality", {}).get("matched_company_count"),
        "detail_enriched": data.get("data_quality", {}).get("detail_enriched_count"),
        "detail_warning": data.get("data_quality", {}).get("detail_warning_count"),
        "detail_error": data.get("data_quality", {}).get("detail_error_count"),
        "threshold_initial": data.get("adaptive_escalation", {}).get("threshold_initial"),
        "threshold_next": data.get("adaptive_escalation", {}).get("threshold_next_run"),
    }, ensure_ascii=False))

s = "/var/lib/stock-bottom-monitor/notifications.sqlite3"
show("STOCK RUNS", query(s, """
SELECT substr(started_at,1,10) day,status,target_count,completed_count,updated_count,
detected_count,new_signal_count,sent_count,error_count,
ROUND((julianday(completed_at)-julianday(started_at))*1440,1) minutes
FROM monitor_runs WHERE started_at>='2026-08-19' ORDER BY started_at
"""))
show("STOCK SUMMARY", query(s, """
SELECT status,COUNT(*) runs,SUM(target_count) targets,SUM(completed_count) completed,
SUM(detected_count) detected,SUM(new_signal_count) new_signals,SUM(sent_count) sent,
SUM(error_count) errors FROM monitor_runs WHERE started_at>='2026-08-19' GROUP BY status
"""))
show("STOCK SIGNALS", query(s, """
SELECT delivery_status,COUNT(*) n,MIN(first_seen_at) first,MAX(last_seen_at) last,
SUM(CASE WHEN COALESCE(last_error,'')<>'' THEN 1 ELSE 0 END) errors
FROM bottom_signals GROUP BY delivery_status
"""))
