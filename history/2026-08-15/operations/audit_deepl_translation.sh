set -eu
export PYTHONPATH=/opt/patent-news-monitor/app
export PYTHONPYCACHEPREFIX=/tmp/deepl-audit-pycache
python3 - <<'PY'
from unittest.mock import patch

from monitor_core.deepl_translation import translate_to_japanese
from gdelt_monitor.notifications import build_digest
from patent_monitor.notifications import send_patent_email

source = "Global markets reopen after emergency closure"
translated = translate_to_japanese([source])[source]
assert translated and translated != source

subject, body = build_digest(
    [{
        "title": source, "companies": ["TEST"], "score": 99.0,
        "direction": "neutral", "domain": "example.com", "seen_date": "2026-08-17",
        "url": "https://example.com", "article_id": "test",
    }],
    {"executed_queries": 1, "successful_queries": 1},
    "test window",
)
assert translated in body and source in body and "原題:" in body

patent_title = "Low-loss semiconductor manufacturing method"
patent_summary = "This invention reduces manufacturing defects and energy consumption."
captured = {}
def capture(_mailer, message):
    captured["body"] = message.get_content()

with patch("patent_monitor.notifications.SMTPMailer.send_message", capture):
    send_patent_email(
        [{
            "_decision": "important", "company_name": "TEST", "title": patent_title,
            "publication_number": "TEST-1", "gemini_importance": 99,
            "gemini_short_term_market_impact": 90, "gemini_summary": patent_summary,
            "source_url": "https://example.com/patent",
        }],
        "unused", 465, "sender@example.com", "unused", "recipient@example.com", "test-run",
    )
assert patent_title in captured["body"] and patent_summary in captured["body"]
assert "原題:" in captured["body"] and "原文要約:" in captured["body"]
print("live_translation_ok")
print("gdelt_render_ok")
print("patent_render_ok_no_email_sent")
PY
stat -c 'cache_mode=%a cache_owner=%U:%G cache_bytes=%s' /var/lib/market-monitor/deepl_translation.sqlite3
systemctl is-active gdelt-news-monitor.timer
systemctl is-active patent-monitor-daily.timer
