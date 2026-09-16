import gzip
import io
import json
import urllib.request


stamp = "20260816133200"
base = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
toc_url = f"{base}{stamp}.toc.json.gz"
with urllib.request.urlopen(toc_url, timeout=30) as response:
    compressed = response.read()
records = []
with gzip.open(io.BytesIO(compressed), "rt", encoding="utf-8") as source:
    for line in source:
        line = line.strip()
        if line:
            records.append(json.loads(line))

ngram_url = f"{base}{stamp}.ngrams.txt.gz"
request = urllib.request.Request(ngram_url, method="HEAD")
with urllib.request.urlopen(request, timeout=30) as response:
    ngram_size = int(response.headers.get("Content-Length", 0))

language_counts = {}
for record in records:
    language = record.get("lang", "")
    language_counts[language] = language_counts.get(language, 0) + 1

print(
    json.dumps(
        {
            "stamp": stamp,
            "toc_compressed_bytes": len(compressed),
            "article_count": len(records),
            "ngram_compressed_bytes": ngram_size,
            "language_counts": sorted(language_counts.items(), key=lambda item: item[1], reverse=True)[:10],
            "sample": records[:3],
        },
        ensure_ascii=False,
        indent=2,
    )
)
