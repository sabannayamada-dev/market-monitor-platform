import gzip
import io
import json
from pathlib import Path
import re
import urllib.request

from gdelt_monitor.analysis import load_company_aliases, normalize_text


stamp = "20260816133200"
base = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
with urllib.request.urlopen(f"{base}{stamp}.toc.json.gz", timeout=30) as response:
    compressed = response.read()
records = []
with gzip.open(io.BytesIO(compressed), "rt", encoding="utf-8") as source:
    records = [json.loads(line) for line in source if line.strip()]

config = json.loads(Path("/opt/patent-news-monitor/app/gdelt_monitor_config.json").read_text(encoding="utf-8"))
terms = []
for item in config["broad_queries"]:
    query = item["query"]
    terms.extend(re.findall(r'"([^"]+)"|\b(?!OR\b)([A-Za-z]+)\b', query))
terms = sorted({normalize_text(phrase or word) for phrase, word in terms if phrase or word})
aliases = load_company_aliases(Path("/opt/patent-news-monitor/app/patent_company_master.csv"))

keyword_hits = []
company_hits = []
combined_hits = []
for record in records:
    title = normalize_text(record.get("title", ""))
    matched_terms = [term for term in terms if term and term in title]
    matched_companies = []
    seen = set()
    for alias in aliases:
        if alias.company_id not in seen and alias.normalized_alias in title:
            seen.add(alias.company_id)
            matched_companies.append(alias.company_name)
    item = {
        "title": record.get("title"),
        "url": record.get("url"),
        "lang": record.get("lang"),
        "terms": matched_terms,
        "companies": matched_companies,
    }
    if matched_terms:
        keyword_hits.append(item)
    if matched_companies:
        company_hits.append(item)
    if matched_terms and matched_companies:
        combined_hits.append(item)

print(
    json.dumps(
        {
            "article_count": len(records),
            "keyword_count": len(keyword_hits),
            "company_count": len(company_hits),
            "combined_count": len(combined_hits),
            "keyword_samples": keyword_hits[:10],
            "company_samples": company_hits[:10],
            "combined_samples": combined_hits[:10],
        },
        ensure_ascii=False,
        indent=2,
    )
)
