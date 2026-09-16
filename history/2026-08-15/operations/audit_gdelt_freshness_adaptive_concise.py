import json
import sqlite3
from datetime import datetime, timezone


def parse(value):
    text = str(value or "")
    if not text:
        return None
    if len(text) == 14 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


c = sqlite3.connect("file:/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print("=== STATE ===")
for row in c.execute("SELECT * FROM service_state ORDER BY state_key"):
    print(json.dumps(dict(row), ensure_ascii=False))
print("=== ADAPTIVE SUMMARY ===")
for row in c.execute(
    "SELECT event_type,COUNT(*) n,MIN(event_at) first,MAX(event_at) last,"
    "MIN(new_interval_minutes) min_interval,MAX(new_interval_minutes) max_interval,"
    "MIN(new_cooldown_hours) min_cooldown,MAX(new_cooldown_hours) max_cooldown,"
    "MIN(new_query_spacing_seconds) min_spacing,MAX(new_query_spacing_seconds) max_spacing "
    "FROM adaptive_events WHERE event_at>='2026-08-18T15:00:00+00:00' GROUP BY event_type"
):
    print(json.dumps(dict(row), ensure_ascii=False))
print("=== LAST ADAPTIVE EVENTS ===")
for row in c.execute("SELECT * FROM adaptive_events ORDER BY event_at DESC LIMIT 12"):
    print(json.dumps(dict(row), ensure_ascii=False))

delays = []
mail_delays = []
examples = []
for row in c.execute(
    "SELECT a.title,a.seen_date,a.first_seen_at,MIN(n.sent_at) sent_at FROM articles a "
    "LEFT JOIN notifications n ON instr(n.article_ids_json,a.article_id)>0 "
    "WHERE a.first_seen_at>='2026-08-18T15:00:00+00:00' GROUP BY a.article_id"
):
    item = dict(row)
    try:
        delay = (parse(item["first_seen_at"]) - parse(item["seen_date"])).total_seconds()/60
        delays.append(delay)
    except Exception:
        delay = None
    try:
        mail_delay = (parse(item["sent_at"]) - parse(item["first_seen_at"])).total_seconds()/3600
        mail_delays.append(mail_delay)
    except Exception:
        mail_delay = None
    examples.append({"title": item["title"], "seen_date": item["seen_date"], "first_seen_at": item["first_seen_at"], "capture_delay_minutes": delay, "email_delay_hours": mail_delay})
print("=== LATENCY SUMMARY ===")
print(json.dumps({
    "articles": len(examples),
    "capture_delay_min": round(min(delays),1) if delays else None,
    "capture_delay_median": round(sorted(delays)[len(delays)//2],1) if delays else None,
    "capture_delay_max": round(max(delays),1) if delays else None,
    "email_delay_min": round(min(mail_delays),1) if mail_delays else None,
    "email_delay_median": round(sorted(mail_delays)[len(mail_delays)//2],1) if mail_delays else None,
    "email_delay_max": round(max(mail_delays),1) if mail_delays else None,
}, ensure_ascii=False))
print("=== SLOWEST CAPTURE EXAMPLES ===")
for item in sorted(examples, key=lambda x: x["capture_delay_minutes"] if x["capture_delay_minutes"] is not None else -1, reverse=True)[:10]:
    print(json.dumps(item, ensure_ascii=False))
c.close()
