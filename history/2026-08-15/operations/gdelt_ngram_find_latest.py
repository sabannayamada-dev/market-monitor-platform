from datetime import datetime, timedelta, timezone
import json
import urllib.error
import urllib.request


base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
attempts = []
found = None
for minutes_back in range(31):
    target = base - timedelta(minutes=minutes_back)
    stamp = target.strftime("%Y%m%d%H%M00")
    url = (
        "https://storage.googleapis.com/data.gdeltproject.org/"
        f"gdeltv5/weblegacy/ngrams/{stamp}.toc.json.gz"
    )
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "personal-market-monitor/0.3"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            found = {
                "stamp": stamp,
                "url": url,
                "content_length": int(response.headers.get("Content-Length", 0)),
                "content_type": response.headers.get("Content-Type"),
                "last_modified": response.headers.get("Last-Modified"),
            }
            attempts.append({"stamp": stamp, "status": response.status})
            break
    except urllib.error.HTTPError as exc:
        attempts.append({"stamp": stamp, "status": exc.code})
print(json.dumps({"attempt_count": len(attempts), "found": found, "attempts": attempts}, indent=2))
