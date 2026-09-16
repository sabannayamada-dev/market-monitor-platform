import json
import sqlite3
import time
from pathlib import Path

from gdelt_monitor.analysis import load_company_aliases
from gdelt_monitor.collector import build_tasks
from gdelt_monitor.config import load_config
from gdelt_monitor.ngram_collector import DEFAULT_ROOT, DocumentMatcher, NgramFile, _scan_file


config = load_config(Path("/tmp/gdelt-pdca-20260816/validation_config.json"))
connection = sqlite3.connect("/tmp/gdelt-pdca-20260816/emergency_validation.sqlite3")
stamp = connection.execute(
    "SELECT stamp FROM ngram_toc_files ORDER BY stamp DESC LIMIT 1"
).fetchone()[0]
connection.close()
aliases = load_company_aliases(
    Path("/opt/patent-news-monitor/app/patent_company_master.csv"),
    Path("/opt/patent-news-monitor/app/data/current_market_cap_snapshot.csv"),
)
tasks = build_tasks(config)
matcher = DocumentMatcher(tasks, aliases)
file = NgramFile(
    stamp,
    f"{DEFAULT_ROOT}{stamp}.toc.json.gz",
    f"{DEFAULT_ROOT}{stamp}.ngrams.txt.gz",
)
started = time.monotonic()
articles, matched_documents, downloaded_bytes = _scan_file(
    file,
    matcher,
    60,
    Path("/tmp/gdelt-pdca-20260816/emergency_toc_cache"),
)
samples = []
for task in tasks:
    for article in articles[task.query_id][:2]:
        samples.append(
            {
                "query": task.profile,
                "title": article["title"],
                "companies": [item["company_name"] for item in article["_company_matches"]],
            }
        )
print(
    json.dumps(
        {
            "stamp": stamp,
            "alias_count": len(aliases),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "downloaded_bytes": downloaded_bytes,
            "matched_documents": matched_documents,
            "articles_by_query": {
                task.profile: len(articles[task.query_id]) for task in tasks
            },
            "samples": samples,
        },
        ensure_ascii=False,
        indent=2,
    )
)
