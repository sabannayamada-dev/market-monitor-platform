from datetime import datetime, timedelta, timezone
import json
import urllib.parse
import urllib.request


now = datetime.now(timezone.utc)
day_prefix = now.strftime("%Y%m%d")
object_prefix = f"gdeltv5/weblegacy/ngrams/{day_prefix}"
start_stamp = (now - timedelta(hours=2)).strftime("%Y%m%d%H%M00")
params = urllib.parse.urlencode(
    {
        "prefix": object_prefix,
        "startOffset": f"gdeltv5/weblegacy/ngrams/{start_stamp}",
        "maxResults": 500,
        "fields": "items(name,size,updated)",
    }
)
url = "https://storage.googleapis.com/storage/v1/b/data.gdeltproject.org/o?" + params
request = urllib.request.Request(url, headers={"User-Agent": "personal-market-monitor/0.3"})
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
items = [item for item in payload.get("items", []) if item["name"].endswith(".toc.json.gz")]
print(
    json.dumps(
        {
            "toc_count": len(items),
            "latest": items[-1] if items else None,
            "recent": items[-5:],
        },
        ensure_ascii=False,
        indent=2,
    )
)
