from datetime import datetime, timedelta, timezone
import json
import urllib.error
import urllib.request


candidate = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
target = candidate.replace(minute=(candidate.minute // 15) * 15)
stamp = target.strftime("%Y%m%d%H%M00")
url = (
    "https://storage.googleapis.com/data.gdeltproject.org/"
    f"gdeltv5/weblegacy/ngrams/{stamp}.toc.json.gz"
)
request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "personal-market-monitor/0.3"})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        result = {
            "result": "available",
            "status": response.status,
            "stamp": stamp,
            "url": url,
            "content_length": response.headers.get("Content-Length"),
            "content_type": response.headers.get("Content-Type"),
            "last_modified": response.headers.get("Last-Modified"),
        }
except urllib.error.HTTPError as exc:
    result = {"result": "http_error", "status": exc.code, "stamp": stamp, "url": url}
print(json.dumps(result, ensure_ascii=False, indent=2))
