from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gdelt_monitor.database import NewsDatabase
from gdelt_monitor.global_news import _same_event, _trusted_domain, select_daily_world_news


def main() -> int:
    app = Path("/opt/patent-news-monitor/app")
    patent = json.loads((app / "patent_monitor_config.json").read_text(encoding="utf-8"))
    paper = json.loads((app / "research_paper_config.json").read_text(encoding="utf-8"))
    gdelt = json.loads((app / "gdelt_monitor_config.json").read_text(encoding="utf-8"))
    assert float(patent["company_search_time_guard_minutes"]) == 90.0
    assert float(patent["company_search_time_guard_minutes"]) + float(patent["postprocess_time_guard_minutes"]) <= 180.0
    arxiv = paper["sources"]["arxiv"]
    assert arxiv["retry_after_cooldown_once"] is True
    assert float(arxiv["retry_wait_minutes"]) == 20.0
    assert float(arxiv["request_spacing_seconds"]) == 10.0

    with tempfile.TemporaryDirectory() as directory:
        copied = Path(directory) / "gdelt.sqlite3"
        source = sqlite3.connect("file:/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3?mode=ro", uri=True)
        destination = sqlite3.connect(copied)
        source.backup(destination)
        source.close()
        destination.close()
        database = NewsDatabase(copied)
        settings = dict(gdelt["global_news_digest"])
        settings["ai_enabled"] = False
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        selected = select_daily_world_news(database, settings, since, "validation-2026-09-15")
        assert len(selected) == 2
        assert all(len(item["title"]) <= int(settings["maximum_title_characters"]) for item in selected)
        assert all(_trusted_domain(item["domain"]) for item in selected)
        assert not _same_event(selected[0], selected[1])
        print("WORLD_NEWS_VALIDATION=OK")
        for item in selected:
            print(f"{item['domain']} | {item['title']}")
    print("DAILY_FIX_CONFIGURATION=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
