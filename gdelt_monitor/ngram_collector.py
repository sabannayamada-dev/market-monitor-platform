from __future__ import annotations

import gzip
import io
import json
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .collector import CollectorError, QueryTask


DEFAULT_ROOT = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/"
USER_AGENT = "personal-market-monitor/0.3 (+GDELT-Web-NGrams)"
LEGAL_SUFFIXES = {
    "co", "company", "corp", "corporation", "group", "holdings", "inc", "incorporated",
    "kk", "limited", "ltd", "plc",
}
AMBIGUOUS_SINGLE_WORD_ALIASES = {
    "base", "canon", "digital", "disco", "electric", "freee", "future", "global", "green", "grid",
    "human", "life", "network", "neural", "note", "open", "pilot", "plus", "screen", "shift", "start",
    "subaru", "system", "systems", "toto", "towa", "trend", "nano",
}
SAFE_SHORT_SINGLE_WORD_ALIASES = {
    "sony", "nec", "ntt", "tdk", "ihi", "hoya", "rohm", "nikon",
    "kddi", "eisai", "fanuc", "denso", "mazda", "honda",
}
AMBIGUOUS_PHRASE_ALIASES = {
    "future innovation", "future innovation group", "neural group",
}
JAPANESE_LEGAL_FORM_PATTERN = re.compile(
    "\u682a\u5f0f\u4f1a\u793e|\u6709\u9650\u4f1a\u793e|\u5408\u540c\u4f1a\u793e|"
    "\u30db\u30fc\u30eb\u30c7\u30a3\u30f3\u30b0\u30b9"
)


class NgramCollectorError(CollectorError):
    error_type = "ngram_error"


@dataclass(frozen=True)
class NgramFile:
    stamp: str
    toc_url: str
    ngram_url: str
    toc_bytes: int = 0


@dataclass
class NgramCollection:
    articles_by_query: dict[str, list[dict[str, Any]]]
    discovered_files: list[NgramFile]
    processed_files: list[dict[str, Any]]
    failed_files: list[dict[str, str]]
    head_requests: int
    downloaded_bytes: int


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def discover_files(
    start: datetime,
    end: datetime,
    root: str = DEFAULT_ROOT,
    timeout_seconds: int = 15,
    publication_lag_minutes: int = 5,
    request_spacing_seconds: float = 0.0,
    skip_stamps: set[str] | None = None,
) -> tuple[list[NgramFile], int]:
    first = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    last = (
        end.astimezone(timezone.utc).replace(second=0, microsecond=0)
        - timedelta(minutes=max(0, publication_lag_minutes))
    )
    files: list[NgramFile] = []
    requests = 0
    current = first
    skipped = skip_stamps or set()
    while current <= last:
        stamp = current.strftime("%Y%m%d%H%M00")
        if stamp in skipped:
            current += timedelta(minutes=1)
            continue
        toc_url = f"{root}{stamp}.toc.json.gz"
        requests += 1
        try:
            with urllib.request.urlopen(_request(toc_url, "HEAD"), timeout=timeout_seconds) as response:
                files.append(
                    NgramFile(
                        stamp=stamp,
                        toc_url=toc_url,
                        ngram_url=f"{root}{stamp}.ngrams.txt.gz",
                        toc_bytes=int(response.headers.get("Content-Length", 0)),
                    )
                )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise NgramCollectorError(f"Web NGrams TOC returned HTTP {exc.code}: {stamp}") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise NgramCollectorError(f"Web NGrams TOC discovery failed: {stamp}: {exc}") from exc
        current += timedelta(minutes=1)
        if current <= last and request_spacing_seconds > 0:
            time.sleep(request_spacing_seconds)
    return files, requests


def _ascii_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", value).casefold())


def _ascii_alias(value: str) -> str:
    words = _ascii_words(value)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if not words:
        return ""
    if len(words) == 1:
        if words[0] in AMBIGUOUS_SINGLE_WORD_ALIASES:
            return ""
        if len(words[0]) < 6 and words[0] not in SAFE_SHORT_SINGLE_WORD_ALIASES:
            return ""
    alias = " ".join(words)
    if alias in AMBIGUOUS_PHRASE_ALIASES:
        return ""
    return alias if len(alias.replace(" ", "")) >= 4 else ""


def _non_ascii_alias(value: str) -> str:
    normalized = _compact_unicode(value)
    return normalized if len(normalized) >= 4 else ""


def _compact_unicode(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = JAPANESE_LEGAL_FORM_PATTERN.sub("", normalized)
    normalized = re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)
    return normalized


def _alternation(values: Iterable[str], ascii_boundary: bool) -> re.Pattern[str] | None:
    ordered = sorted(set(filter(None, values)), key=lambda item: (-len(item), item))
    if not ordered:
        return None
    body = "(?:" + "|".join(re.escape(item) for item in ordered) + ")"
    if ascii_boundary:
        body = r"(?<![a-z0-9])" + body + r"(?![a-z0-9])"
    return re.compile(body)


def _query_terms(query: str) -> tuple[list[str], list[str]]:
    ascii_terms: list[str] = []
    unicode_terms: list[str] = []
    for quoted, bare in re.findall(r'"([^"]+)"|([^\s()"]+)', query):
        value = unicodedata.normalize("NFKC", quoted or bare).casefold().strip()
        if value in {"or", "and", "not"}:
            continue
        if value.startswith("sourcelang:"):
            continue
        if value.isascii():
            ascii_terms.append(" ".join(_ascii_words(value)))
        else:
            unicode_terms.append(_compact_unicode(value))
    return (
        list(dict.fromkeys(filter(None, ascii_terms))),
        list(dict.fromkeys(filter(None, unicode_terms))),
    )


def _normalized_language(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    aliases = {
        "ja": "japanese",
        "jp": "japanese",
        "jpn": "japanese",
        "japanese": "japanese",
        "en": "english",
        "eng": "english",
        "english": "english",
    }
    return aliases.get(normalized, normalized)


def _query_source_language(query: str) -> str:
    match = re.search(r"(?i)(?:^|[\s(])sourcelang:([a-z-]+)(?=$|[\s)])", query)
    return _normalized_language(match.group(1)) if match else ""


class DocumentMatcher:
    def __init__(self, tasks: list[QueryTask], aliases: list[Any]):
        self.tasks = tasks
        self.query_patterns = {}
        self.query_source_languages = {}
        for task in tasks:
            ascii_terms, unicode_terms = _query_terms(task.query)
            self.query_patterns[task.query_id] = (
                _alternation(ascii_terms, True),
                _alternation(unicode_terms, False),
            )
            self.query_source_languages[task.query_id] = _query_source_language(task.query)
        self.ascii_aliases: dict[str, list[Any]] = {}
        self.non_ascii_aliases: dict[str, list[Any]] = {}
        for alias in aliases:
            raw = str(alias.alias)
            without_japanese_legal_form = JAPANESE_LEGAL_FORM_PATTERN.sub(
                "", unicodedata.normalize("NFKC", raw)
            ).strip()
            is_ascii = without_japanese_legal_form.isascii()
            normalized = (
                _ascii_alias(without_japanese_legal_form)
                if is_ascii
                else _non_ascii_alias(raw)
            )
            if not normalized:
                continue
            target = self.ascii_aliases if is_ascii else self.non_ascii_aliases
            target.setdefault(normalized, []).append(alias)
        self.ascii_alias_pattern = _alternation(self.ascii_aliases, True)
        self.non_ascii_alias_pattern = _alternation(self.non_ascii_aliases, False)

    def filter_queries_by_language(self, query_hits: set[str], language: str) -> set[str]:
        actual = _normalized_language(language)
        return {
            query_id
            for query_id in query_hits
            if not self.query_source_languages.get(query_id)
            or self.query_source_languages[query_id] == actual
        }

    def scan_line(
        self,
        quadgram: str,
        query_hits: set[str],
        company_hits: dict[str, Any],
    ) -> None:
        found_queries, found_companies = self.scan_document([quadgram])
        query_hits.update(found_queries)
        for company_id, alias in found_companies.items():
            company_hits.setdefault(company_id, alias)

    def scan_document(self, quadgrams: list[str]) -> tuple[set[str], dict[str, Any]]:
        raw_text = "\n".join(quadgrams)
        return self._scan_text(raw_text)

    def scan_title(self, title: str) -> tuple[set[str], dict[str, Any]]:
        return self._scan_text(title)

    def _scan_text(
        self, raw_text: str, require_query: bool = True
    ) -> tuple[set[str], dict[str, Any]]:
        ascii_line = " ".join(_ascii_words(raw_text))
        compact_unicode = _compact_unicode(raw_text)
        query_hits: set[str] = set()
        for query_id, (ascii_pattern, unicode_pattern) in self.query_patterns.items():
            if (
                (ascii_pattern is not None and ascii_pattern.search(ascii_line))
                or (unicode_pattern is not None and unicode_pattern.search(compact_unicode))
            ):
                query_hits.add(query_id)
        if require_query and not query_hits:
            return query_hits, {}
        company_hits: dict[str, Any] = {}
        if self.ascii_alias_pattern is not None:
            for match in self.ascii_alias_pattern.finditer(ascii_line):
                for alias in self.ascii_aliases.get(match.group(0), []):
                    company_hits.setdefault(alias.company_id, alias)
        if self.non_ascii_alias_pattern is not None:
            compact = compact_unicode
            if compact:
                for match in self.non_ascii_alias_pattern.finditer(compact):
                    for alias in self.non_ascii_aliases.get(match.group(0), []):
                        company_hits.setdefault(alias.company_id, alias)
        return query_hits, company_hits


def load_toc(
    file: NgramFile,
    timeout_seconds: int,
    cache_dir: Path | None = None,
) -> tuple[dict[int, dict[str, Any]], int]:
    cache_path = cache_dir / f"{file.stamp}.toc.json.gz" if cache_dir else None
    if cache_path is not None and cache_path.is_file():
        payload = cache_path.read_bytes()
        network_bytes = 0
    else:
        try:
            with urllib.request.urlopen(_request(file.toc_url), timeout=timeout_seconds) as response:
                payload = response.read()
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise NgramCollectorError(f"Web NGrams TOC download failed: {file.stamp}: {exc}") from exc
        network_bytes = len(payload)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(cache_path)
    records: dict[int, dict[str, Any]] = {}
    with gzip.open(io.BytesIO(payload), "rt", encoding="utf-8", errors="replace") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            records[int(record["ID"])] = record
    return records, network_bytes


def _article(record: dict[str, Any], stamp: str, companies: dict[str, Any]) -> dict[str, Any]:
    url = str(record.get("url") or "")
    return {
        "url": url,
        "title": str(record.get("title") or ""),
        "seendate": str(record.get("date") or stamp),
        "domain": urlsplit(url).netloc.casefold(),
        "language": str(record.get("lang") or ""),
        "_ngram_stamp": stamp,
        "_dedupe_by_title": True,
        "_company_matches": [
            {
                "company_id": alias.company_id,
                "company_name": alias.company_name,
                "ticker": alias.ticker,
                "alias": alias.alias,
            }
            for alias in companies.values()
        ],
    }


def _scan_file(
    file: NgramFile,
    matcher: DocumentMatcher,
    timeout_seconds: int,
    cache_dir: Path | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    toc, downloaded = load_toc(file, timeout_seconds, cache_dir)
    articles = {task.query_id: [] for task in matcher.tasks}
    current_id: int | None = None
    quadgrams: list[str] = []
    matched_documents = 0

    def finish() -> None:
        nonlocal matched_documents
        query_hits, company_hits = matcher.scan_document(quadgrams)
        if current_id is None or not query_hits or not company_hits:
            return
        record = toc.get(current_id)
        if not record or (not record.get("url") and not record.get("title")):
            return
        query_hits = matcher.filter_queries_by_language(
            query_hits, str(record.get("lang") or "")
        )
        if not query_hits:
            return
        title_queries, title_companies = matcher.scan_title(str(record.get("title") or ""))
        query_hits &= title_queries
        company_hits = {
            company_id: alias
            for company_id, alias in company_hits.items()
            if company_id in title_companies
        }
        if not query_hits or not company_hits:
            return
        item = _article(record, file.stamp, company_hits)
        for query_id in query_hits:
            articles[query_id].append(item)
        matched_documents += 1

    try:
        with urllib.request.urlopen(_request(file.ngram_url), timeout=timeout_seconds) as response:
            downloaded += int(response.headers.get("Content-Length", 0))
            with gzip.open(response, "rt", encoding="utf-8", errors="replace") as source:
                for line in source:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) != 3:
                        continue
                    doc_id = int(parts[0])
                    if current_id is not None and doc_id != current_id:
                        finish()
                        quadgrams = []
                    current_id = doc_id
                    quadgrams.append(parts[1])
        finish()
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, ValueError) as exc:
        raise NgramCollectorError(f"Web NGrams data processing failed: {file.stamp}: {exc}") from exc
    return articles, matched_documents, downloaded


def collect_web_ngrams(
    tasks: list[QueryTask],
    aliases: list[Any],
    start: datetime,
    end: datetime,
    settings: dict[str, Any],
    already_processed: set[str] | None = None,
    cache_dir: Path | None = None,
) -> NgramCollection:
    root = str(settings.get("web_ngrams_root") or DEFAULT_ROOT)
    timeout = int(settings.get("request_timeout_seconds", 60))
    processed = set(already_processed or set())
    files, head_requests = discover_files(
        start,
        end,
        root=root,
        timeout_seconds=timeout,
        publication_lag_minutes=int(settings.get("publication_lag_minutes", 5)),
        request_spacing_seconds=float(settings.get("head_request_spacing_seconds", 0.0)),
        skip_stamps=processed,
    )
    pending = [file for file in files if file.stamp not in processed]
    matcher = DocumentMatcher(tasks, aliases)
    articles_by_query = {task.query_id: [] for task in tasks}
    processed_files: list[dict[str, Any]] = []
    failed_files: list[dict[str, str]] = []
    downloaded_bytes = 0
    for index, file in enumerate(pending):
        try:
            file_articles, matched_documents, file_bytes = _scan_file(
                file, matcher, timeout, cache_dir
            )
            downloaded_bytes += file_bytes
            for query_id, articles in file_articles.items():
                articles_by_query[query_id].extend(articles)
            processed_files.append(
                {"stamp": file.stamp, "matched_documents": matched_documents, "bytes": file_bytes}
            )
        except NgramCollectorError as exc:
            failed_files.append({"stamp": file.stamp, "message": str(exc)})
        if index < len(pending) - 1:
            spacing = float(settings.get("file_download_spacing_seconds", 0.0))
            if spacing > 0:
                time.sleep(spacing)
    return NgramCollection(
        articles_by_query=articles_by_query,
        discovered_files=files,
        processed_files=processed_files,
        failed_files=failed_files,
        head_requests=head_requests,
        downloaded_bytes=downloaded_bytes,
    )
