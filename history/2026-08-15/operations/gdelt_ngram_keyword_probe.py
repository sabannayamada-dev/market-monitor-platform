import gzip
import io
import json
import re
import urllib.request


stamp = "20260816133200"
base = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
terms = [
    "recall", "cyberattack", "fraud", "lawsuit", "investigation", "factory shutdown",
    "data breach", "food poisoning", "hygiene", "contamination", "boycott", "viral video",
    "misconduct", "large order", "partnership", "approval", "breakthrough", "new contract",
    "acquisition", "sanctions", "export restriction", "war", "strike", "supply disruption",
    "port closure",
]
patterns = {
    term: re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.IGNORECASE)
    for term in terms
}

matches = {}
with urllib.request.urlopen(f"{base}{stamp}.ngrams.txt.gz", timeout=60) as response:
    with gzip.open(response, "rt", encoding="utf-8", errors="replace") as source:
        for line in source:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            doc_id, quadgram, count = parts
            found = [term for term, pattern in patterns.items() if pattern.search(quadgram)]
            if found:
                item = matches.setdefault(int(doc_id), {"terms": set(), "mentions": 0})
                item["terms"].update(found)
                item["mentions"] += int(count or 0)

with urllib.request.urlopen(f"{base}{stamp}.toc.json.gz", timeout=30) as response:
    toc_compressed = response.read()
toc = {}
with gzip.open(io.BytesIO(toc_compressed), "rt", encoding="utf-8") as source:
    for line in source:
        if line.strip():
            record = json.loads(line)
            toc[int(record["ID"])] = record

results = []
for doc_id, match in matches.items():
    record = toc.get(doc_id, {})
    results.append(
        {
            "ID": doc_id,
            "date": record.get("date"),
            "lang": record.get("lang"),
            "title": record.get("title"),
            "url": record.get("url"),
            "terms": sorted(match["terms"]),
            "mentions": match["mentions"],
        }
    )
language_counts = {}
for item in results:
    language_counts[item["lang"]] = language_counts.get(item["lang"], 0) + 1
print(
    json.dumps(
        {
            "article_count": len(toc),
            "keyword_document_count": len(results),
            "language_counts": sorted(language_counts.items(), key=lambda item: item[1], reverse=True),
            "samples": results[:30],
        },
        ensure_ascii=False,
        indent=2,
    )
)
