from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/patent-news-monitor/app")

from gdelt_monitor.config import RuntimePaths, load_config
from gdelt_monitor.database import NewsDatabase
from gdelt_monitor.global_news import rank_toc_records


def main() -> int:
    os.environ.setdefault("GDELT_CONFIG_PATH", "/opt/patent-news-monitor/app/gdelt_monitor_config.json")
    os.environ.setdefault("GDELT_STATE_DB", "/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
    os.environ.setdefault("GDELT_TOC_CACHE_DIR", "/var/lib/gdelt-news-monitor/toc_cache")
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    settings = config.get("global_news_digest", {})
    database = NewsDatabase(paths.database)
    now = datetime.now(timezone.utc)
    now_jst = now.astimezone(ZoneInfo("Asia/Tokyo"))
    day_start = datetime(now_jst.year, now_jst.month, now_jst.day, tzinfo=ZoneInfo("Asia/Tokyo"))
    minimum_stamp = day_start.astimezone(timezone.utc).strftime("%Y%m%d%H%M00")
    files = 0
    inserted = 0
    for path in sorted(paths.toc_cache.glob("*.toc.json.gz")):
        stamp = path.name.split(".", 1)[0]
        if stamp < minimum_stamp:
            continue
        records = []
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as source:
            for line in source:
                if line.strip():
                    records.append(json.loads(line))
        inserted += database.record_global_news_candidates(
            rank_toc_records(records, stamp, now, settings)
        )
        files += 1
    candidates = database.global_news_candidates_since(day_start.astimezone(timezone.utc).isoformat(timespec="seconds"), 30)
    print(f"CACHE_FILES={files}")
    print(f"CANDIDATES_INSERTED={inserted}")
    print(f"DAILY_DISTINCT_CANDIDATES={len(candidates)}")
    for item in candidates[:5]:
        print(f"TOP score={item['rule_score']:.0f} topic={item['topic']} domain={item['domain']} title={item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
