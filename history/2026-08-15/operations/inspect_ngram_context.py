import gzip
import json
import urllib.request
from pathlib import Path


ROOT = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
targets = {
    "20260816123100": "https://www.tomshardware.com/pc-components/cpus/the-pc-age-began-45-years-ago-with-the-breakthrough-intel-8088-processor-8-bit-bus-fueled-45-years-of-x86-dominance",
    "20260816134700": "https://www.ksmu.org/people/ryan-benk",
    "20260816130200": "https://screenrant.com/life-is-strange-adaptation-arcadia-bay-set-photos/",
    "20260816141600": "https://www.sonypictures.com/corp/divisions.html",
}
cache = Path("/tmp/gdelt-pdca-20260816/quality_toc_cache")
result = {}
for stamp, target_url in targets.items():
    toc_path = cache / f"{stamp}.toc.json.gz"
    with gzip.open(toc_path, "rt", encoding="utf-8", errors="replace") as source:
        record = next(json.loads(line) for line in source if json.loads(line).get("url") == target_url)
    doc_id = int(record["ID"])
    request = urllib.request.Request(
        f"{ROOT}{stamp}.ngrams.txt.gz",
        headers={"User-Agent": "personal-market-monitor/0.3 (+quality-audit)"},
    )
    quadgrams = []
    with urllib.request.urlopen(request, timeout=60) as response:
        with gzip.open(response, "rt", encoding="utf-8", errors="replace") as source:
            for line in source:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) == 3 and int(parts[0]) == doc_id:
                    quadgrams.append(parts[1])
    result[stamp] = {"title": record.get("title"), "doc_id": doc_id, "quadgrams": quadgrams}
print(json.dumps(result, ensure_ascii=False, indent=2))
