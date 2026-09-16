import csv
import gzip
import io
import json
import re
import sqlite3
import unicodedata
import urllib.request


stamps = [
    "20260816134700", "20260816134600", "20260816133200",
    "20260816133100", "20260816131700", "20260816131600",
    "20260816130200", "20260816130100", "20260816124700",
]
base = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
keywords = [
    "recall", "cyberattack", "fraud", "lawsuit", "investigation", "factory shutdown",
    "data breach", "food poisoning", "hygiene", "contamination", "boycott", "viral video",
    "misconduct", "large order", "partnership", "approval", "breakthrough", "new contract",
    "acquisition", "sanctions", "export restriction", "war", "strike", "supply disruption",
    "port closure",
]
keyword_patterns = {
    term: re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.IGNORECASE)
    for term in keywords
}


def folded(value):
    return unicodedata.normalize("NFKC", value).casefold()


def word_text(value):
    return " " + re.sub(r"[^0-9a-z]+", " ", folded(value)).strip() + " "


aliases = []
stock_connection = sqlite3.connect("/var/lib/stock-bottom-monitor/stock_cache.db")
stock_names = dict(stock_connection.execute("SELECT symbol,name FROM stocks WHERE name<>''"))
stock_connection.close()
ambiguous_short_names = {
    "screen", "disco", "pilot", "note", "base", "shift", "trend", "future", "global",
    "digital", "system", "systems", "network", "open", "plus", "life", "start", "freee",
}
legal_suffixes = {
    "co", "company", "corp", "corporation", "inc", "incorporated", "ltd", "limited", "plc",
}
with open("/opt/patent-news-monitor/app/patent_company_master.csv", encoding="utf-8-sig", newline="") as source:
    for row in csv.DictReader(source):
        if str(row.get("target", "1")).strip().lower() not in {"1", "true"}:
            continue
        ticker = str(row.get("ticker", "")).strip()
        symbol = ticker if "." in ticker else ticker[:4] + ".T" if ticker[:4].isdigit() else ticker
        values = [row.get("company_name", ""), stock_names.get(symbol, "")]
        for field in ("aliases", "subsidiaries"):
            values.extend(part.strip() for part in str(row.get(field, "")).split("|") if part.strip())
        expanded_values = []
        for value in dict.fromkeys(value for value in values if value):
            expanded_values.append(value)
            if value.isascii():
                words = re.findall(r"[A-Za-z0-9]+", value)
                while words and words[-1].casefold() in legal_suffixes:
                    words.pop()
                while words and words[-1].casefold() in {"co"}:
                    words.pop()
                core = " ".join(words)
                # Auto-derived one-word aliases (for example Human, Green, Electric)
                # are too ambiguous for global news.  Keep only multi-word cores;
                # curated one-word aliases can still come from the master CSV.
                if core and len(words) >= 2:
                    expanded_values.append(core)
        for value in dict.fromkeys(expanded_values):
            is_ascii = value.isascii()
            normalized = word_text(value).strip() if is_ascii else folded(value).replace(" ", "")
            if len(normalized) >= 4:
                aliases.append((row["company_id"], row["company_name"], value, normalized, is_ascii))


def finish_document(stamp, doc_id, quads, found_keywords, results):
    if doc_id is None or not found_keywords:
        return
    raw_text = "\n".join(quads)
    ascii_text = word_text(raw_text)
    compact_text = folded(raw_text).replace(" ", "")
    companies = {}
    for company_id, company_name, alias, normalized, is_ascii in aliases:
        if company_id in companies:
            continue
        matched = f" {normalized} " in ascii_text if is_ascii else normalized in compact_text
        if matched:
            companies[company_id] = {"company": company_name, "alias": alias}
    if companies:
        results[(stamp, int(doc_id))] = {
            "keywords": sorted(found_keywords),
            "companies": list(companies.values()),
        }


results = {}
for stamp in stamps:
    current_id = None
    quads = []
    found_keywords = set()
    with urllib.request.urlopen(f"{base}{stamp}.ngrams.txt.gz", timeout=60) as response:
        with gzip.open(response, "rt", encoding="utf-8", errors="replace") as source:
            for line in source:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) != 3:
                    continue
                doc_id, quadgram, _count = parts
                if current_id is not None and doc_id != current_id:
                    finish_document(stamp, current_id, quads, found_keywords, results)
                    quads = []
                    found_keywords = set()
                current_id = doc_id
                quads.append(quadgram)
                found_keywords.update(
                    term for term, pattern in keyword_patterns.items() if pattern.search(quadgram)
                )
    finish_document(stamp, current_id, quads, found_keywords, results)

toc = {}
for stamp in stamps:
    with urllib.request.urlopen(f"{base}{stamp}.toc.json.gz", timeout=30) as response:
        toc_bytes = response.read()
    with gzip.open(io.BytesIO(toc_bytes), "rt", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                record = json.loads(line)
                toc[(stamp, int(record["ID"]))] = record

output = []
for key, match in results.items():
    record = toc.get(key, {})
    output.append({**match, "stamp": key[0], "lang": record.get("lang"), "title": record.get("title"), "url": record.get("url")})
unique_titles = {item.get("title") for item in output if item.get("title")}
print(json.dumps({"file_count": len(stamps), "matched_count": len(output), "unique_title_count": len(unique_titles), "matches": output}, ensure_ascii=False, indent=2))
