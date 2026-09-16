from datetime import datetime, timedelta, timezone
import json

from gdelt_monitor.collector import QueryTask, RateLimited, fetch_once


end = datetime.now(timezone.utc)
start = end - timedelta(minutes=15)
task = QueryTask("pdca-1", "PDCA最小負荷", "recall", "diagnostic", 1)
try:
    articles = fetch_once(task, start, end, max_records=1, timeout_seconds=30)
except RateLimited as exc:
    print(
        json.dumps(
            {
                "result": "rate_limited",
                "status_code": exc.status_code,
                "retry_after_seconds": exc.retry_after_seconds,
                "message": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
else:
    print(
        json.dumps(
            {
                "result": "success",
                "article_count": len(articles),
                "sample": articles[:1],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
