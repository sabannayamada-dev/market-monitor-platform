from datetime import datetime, timedelta, timezone
import json
import urllib.error
import urllib.request


ROOT = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
found = []
statuses = {}

for minutes_back in range(61):
    target = base - timedelta(minutes=minutes_back)
    stamp = target.strftime("%Y%m%d%H%M00")
    request = urllib.request.Request(
        f"{ROOT}{stamp}.toc.json.gz",
        method="HEAD",
        headers={"User-Agent": "personal-market-monitor/0.3"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            found.append(
                {
                    "stamp": stamp,
                    "toc_bytes": int(response.headers.get("Content-Length", 0)),
                }
            )
            statuses[str(response.status)] = statuses.get(str(response.status), 0) + 1
    except urllib.error.HTTPError as exc:
        statuses[str(exc.code)] = statuses.get(str(exc.code), 0) + 1

for item in found:
    request = urllib.request.Request(
        f"{ROOT}{item['stamp']}.ngrams.txt.gz",
        method="HEAD",
        headers={"User-Agent": "personal-market-monitor/0.3"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        item["ngram_bytes"] = int(response.headers.get("Content-Length", 0))

print(
    json.dumps(
        {
            "window_start": (base - timedelta(minutes=60)).isoformat(),
            "window_end": base.isoformat(),
            "head_requests": 61 + len(found),
            "statuses": statuses,
            "found_count": len(found),
            "files": found,
        },
        indent=2,
    )
)
