from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__)


def child() -> int:
    sys.path.insert(0, "/opt/patent-news-monitor/app")
    from gdelt_monitor.config import RuntimePaths, load_config
    from gdelt_monitor.database import NewsDatabase
    from gdelt_monitor.global_news import select_daily_world_news

    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Tokyo"))
    start = datetime(2026, 9, 7, tzinfo=ZoneInfo("Asia/Tokyo"))
    digest_key = "validation-2026-09-07"
    database = NewsDatabase(paths.database)
    with database.connection() as connection:
        connection.execute("DELETE FROM global_news_digests WHERE digest_date=?", (digest_key,))
    selected = select_daily_world_news(
        database, config.get("global_news_digest", {}),
        start.astimezone(timezone.utc).isoformat(timespec="seconds"), digest_key,
    )
    print(json.dumps([
        {key: item.get(key) for key in (
            "title", "japanese_title", "summary", "importance", "reason", "topic", "domain", "selection_method"
        )}
        for item in selected
    ], ensure_ascii=False, indent=2))
    with database.connection() as connection:
        connection.execute("DELETE FROM global_news_digests WHERE digest_date=?", (digest_key,))
    return 0 if len(selected) == 2 else 1


def main() -> int:
    if "--child" in sys.argv:
        return child()
    environment = os.environ.copy()
    for raw in Path("/etc/patent-news-monitor.env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            environment[name.strip()] = value.strip()
    return subprocess.run(
        ["/opt/patent-news-monitor/venv/bin/python", str(SCRIPT), "--child"],
        env=environment, user="app", group="app",
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
