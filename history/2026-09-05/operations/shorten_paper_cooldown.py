from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from research_paper_monitor.database import PaperDatabase
from research_paper_monitor.rate_limit import load_state


def main() -> int:
    database = PaperDatabase("/var/lib/research-paper-monitor/research_papers.sqlite3")
    state = load_state(database, "arxiv")
    last = str(state.get("last_429_at") or "")
    if not last:
        print("ARXIV_COOLDOWN=UNCHANGED_NO_429")
        return 0
    limited_at = datetime.fromisoformat(last)
    if limited_at.tzinfo is None:
        limited_at = limited_at.replace(tzinfo=timezone.utc)
    shortened_until = limited_at.astimezone(timezone.utc) + timedelta(minutes=20)
    state["cooldown_until"] = shortened_until.isoformat(timespec="seconds")
    state["last_reason"] = "HTTP 429/403: 20分休止（設定変更を適用）"
    now = datetime.now(timezone.utc)
    database.set_state(
        "source_rate_limit:arxiv",
        json.dumps(state, ensure_ascii=False),
        now.isoformat(timespec="seconds"),
    )
    status = "EXPIRED" if shortened_until <= now else "ACTIVE"
    remaining = max(0, int((shortened_until - now).total_seconds()))
    print(f"ARXIV_COOLDOWN={status}")
    print(f"REMAINING_SECONDS={remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
