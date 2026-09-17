from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .models import PaperRecord


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class CollectionError(RuntimeError):
    pass


class RateLimited(CollectionError):
    def __init__(self, status_code: int, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.partial_records: list[PaperRecord] = []


def _retry_after_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _get(url: str, timeout: int, user_agent: str, retries: int = 2) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {403, 406, 429}:
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After", "") if exc.headers else "")
                suffix = f"; Retry-After={retry_after}s" if retry_after is not None else ""
                raise RateLimited(exc.code, f"HTTP {exc.code}{suffix}", retry_after) from exc
            raise CollectionError(f"HTTP {exc.code}: {url}") from exc
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            break
    raise CollectionError(f"request failed: {type(last_error).__name__}") from last_error


def reconstruct_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in value.items():
        for index in indexes or []:
            positions.append((int(index), str(word)))
    return " ".join(word for _, word in sorted(positions))


def parse_openalex_work(item: dict[str, Any]) -> PaperRecord:
    ids = item.get("ids") or {}
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    locations = item.get("locations") or []
    pdf_url = str((item.get("best_oa_location") or {}).get("pdf_url") or "")
    if not pdf_url:
        pdf_url = next((str(loc.get("pdf_url")) for loc in locations if loc.get("pdf_url")), "")
    authors = tuple(
        str((entry.get("author") or {}).get("display_name") or "").strip()
        for entry in item.get("authorships") or []
        if (entry.get("author") or {}).get("display_name")
    )
    topics: list[str] = []
    for topic in item.get("topics") or []:
        for value in (
            topic.get("display_name"),
            (topic.get("subfield") or {}).get("display_name"),
            (topic.get("field") or {}).get("display_name"),
        ):
            if value and value not in topics:
                topics.append(str(value))
    arxiv_id = str(ids.get("arxiv") or "")
    if arxiv_id:
        arxiv_id = arxiv_id.rstrip("/").rsplit("/", 1)[-1]
    return PaperRecord(
        source="openalex",
        source_id=str(item.get("id") or "").rstrip("/").rsplit("/", 1)[-1],
        title=str(item.get("title") or item.get("display_name") or "").strip(),
        abstract=reconstruct_abstract(item.get("abstract_inverted_index")),
        authors=authors,
        published_at=str(item.get("publication_date") or ""),
        updated_at=str(item.get("updated_date") or ""),
        doi=str(item.get("doi") or ids.get("doi") or ""),
        arxiv_id=arxiv_id,
        journal=str(source.get("display_name") or ""),
        categories=tuple(topics),
        landing_url=str(primary.get("landing_page_url") or item.get("doi") or item.get("id") or ""),
        pdf_url=pdf_url,
        raw=item,
    )


def collect_openalex(config: dict[str, Any], profiles: list[dict[str, Any]], start_date: str) -> list[PaperRecord]:
    settings = config.get("sources", {}).get("openalex", {})
    if not settings.get("enabled", True):
        return []
    api_key = str(settings.get("api_key") or "").strip()
    if not api_key:
        import os
        api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    base_url = str(settings.get("base_url", "https://api.openalex.org/works"))
    timeout = int(settings.get("timeout_seconds", 30))
    per_page = min(100, max(1, int(settings.get("per_page", 100))))
    max_pages = max(1, int(settings.get("max_pages_per_profile", 2)))
    user_agent = str(settings.get("user_agent", "research-paper-monitor/1.0"))
    records: list[PaperRecord] = []
    for profile in profiles:
        queries = profile.get("openalex_queries") or [profile.get("openalex_query")]
        for raw_query in queries:
            query = str(raw_query or "").strip()
            if not query:
                continue
            cursor = "*"
            for _ in range(max_pages):
                params = {
                    "search": query,
                    "filter": f"from_publication_date:{start_date}",
                    "sort": "publication_date:desc",
                    "per_page": str(per_page),
                    "cursor": cursor,
                }
                if api_key:
                    params["api_key"] = api_key
                url = base_url + "?" + urllib.parse.urlencode(params)
                try:
                    payload = json.loads(_get(url, timeout, user_agent).decode("utf-8-sig"))
                except RateLimited as exc:
                    exc.partial_records = records
                    raise
                for item in payload.get("results") or []:
                    record = parse_openalex_work(item)
                    if record.source_id and record.title:
                        records.append(record)
                cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
                if not cursor:
                    break
    return records


def _text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _descendant_text(element: ET.Element, name: str, languages: tuple[str, ...] = ("ja", "en")) -> str:
    containers = [item for item in element.iter() if _local_name(item.tag) == name]
    for container in containers:
        children = list(container)
        if children:
            by_name = {_local_name(child.tag): " ".join("".join(child.itertext()).split()) for child in children}
            for language in languages:
                if by_name.get(language):
                    return by_name[language]
        value = " ".join("".join(container.itertext()).split())
        if value:
            return value
    return ""


def parse_arxiv_feed(payload: bytes) -> list[PaperRecord]:
    root = ET.fromstring(payload)
    records: list[PaperRecord] = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_url = _text(entry, f"{ATOM}id")
        match = re.search(r"/abs/([^/?#]+)", entry_url)
        versioned_id = match.group(1) if match else entry_url.rstrip("/").rsplit("/", 1)[-1]
        arxiv_id = re.sub(r"v\d+$", "", versioned_id)
        links = {link.attrib.get("type", ""): link.attrib.get("href", "") for link in entry.findall(f"{ATOM}link")}
        doi = _text(entry, f"{ARXIV}doi")
        records.append(
            PaperRecord(
                source="arxiv",
                source_id=arxiv_id,
                title=" ".join(_text(entry, f"{ATOM}title").split()),
                abstract=" ".join(_text(entry, f"{ATOM}summary").split()),
                authors=tuple(_text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")),
                published_at=_text(entry, f"{ATOM}published"),
                updated_at=_text(entry, f"{ATOM}updated"),
                doi=doi,
                arxiv_id=arxiv_id,
                journal=_text(entry, f"{ARXIV}journal_ref"),
                categories=tuple(cat.attrib.get("term", "") for cat in entry.findall(f"{ATOM}category")),
                landing_url=entry_url,
                pdf_url=links.get("application/pdf", ""),
                raw={"entry_id": entry_url, "versioned_id": versioned_id},
            )
        )
    return records


def collect_arxiv(config: dict[str, Any], profiles: list[dict[str, Any]], start: datetime, end: datetime) -> list[PaperRecord]:
    settings = config.get("sources", {}).get("arxiv", {})
    if not settings.get("enabled", True):
        return []
    base_url = str(settings.get("base_url", "https://export.arxiv.org/api/query"))
    timeout = int(settings.get("timeout_seconds", 45))
    max_results = min(500, max(1, int(settings.get("max_results_per_profile", 100))))
    spacing = max(3.0, float(settings.get("request_spacing_seconds", 3)))
    retry_once = bool(settings.get("retry_after_cooldown_once", False))
    retry_wait_seconds = max(60.0, float(settings.get("retry_wait_minutes", 20)) * 60.0)
    user_agent = str(settings.get("user_agent", "research-paper-monitor/1.0"))
    date_range = f"submittedDate:[{start.astimezone(timezone.utc):%Y%m%d%H%M} TO {end.astimezone(timezone.utc):%Y%m%d%H%M}]"
    records: list[PaperRecord] = []
    used = 0
    retry_used = False
    for profile in profiles:
        categories = [str(value) for value in profile.get("arxiv_categories") or [] if value]
        if not categories:
            continue
        category_query = " OR ".join(f"cat:{value}" for value in categories)
        raw_queries = profile.get("arxiv_queries") or [""]
        terms = list(
            dict.fromkeys(
                term.strip()
                for raw_query in raw_queries
                for term in str(raw_query).split("|")
                if term.strip()
            )
        )
        if used:
            time.sleep(spacing)
        keyword_query = " OR ".join(f'all:"{term}"' for term in terms)
        search_query = f"({category_query}) AND {date_range}"
        if keyword_query:
            search_query = f"({category_query}) AND ({keyword_query}) AND {date_range}"
        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": str(max_results),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        while True:
            try:
                records.extend(parse_arxiv_feed(_get(base_url + "?" + urllib.parse.urlencode(params), timeout, user_agent)))
                break
            except RateLimited as exc:
                if retry_once and not retry_used:
                    retry_used = True
                    # Keep the daily service inside its 45-minute systemd budget even
                    # when a server supplies an excessively long Retry-After value.
                    wait_seconds = retry_wait_seconds
                    print(
                        f"arxiv: HTTP {exc.status_code}; {wait_seconds / 60:g}分待機後に1回だけ再試行します",
                        flush=True,
                    )
                    time.sleep(wait_seconds)
                    continue
                exc.partial_records = records
                raise
        used += 1
    return records


def parse_jstage_feed(payload: bytes) -> list[PaperRecord]:
    root = ET.fromstring(payload)
    status = _descendant_text(root, "status", languages=("ja", "en"))
    if status and status != "0":
        message = _descendant_text(root, "message", languages=("ja", "en")) or status
        if status == "ERR_003":
            raise RateLimited(429, f"J-STAGE {status}: {message}")
        if status == "ERR_001":
            return []
        raise CollectionError(f"J-STAGE {status}: {message}")
    records: list[PaperRecord] = []
    for entry in (item for item in root.iter() if _local_name(item.tag) == "entry"):
        title = _descendant_text(entry, "article_title")
        landing_url = _descendant_text(entry, "article_link")
        doi = _descendant_text(entry, "doi", languages=("en", "ja"))
        joi = _descendant_text(entry, "joi", languages=("en", "ja"))
        updated = _descendant_text(entry, "updated", languages=("en", "ja"))
        journal = _descendant_text(entry, "material_title")
        authors: list[str] = []
        for author in (item for item in entry.iter() if _local_name(item.tag) == "author"):
            language_container = next(
                (child for language in ("ja", "en") for child in list(author) if _local_name(child.tag) == language),
                author,
            )
            names = [
                " ".join("".join(item.itertext()).split())
                for item in language_container.iter()
                if _local_name(item.tag) == "name"
            ]
            if not names:
                names = [" ".join("".join(language_container.itertext()).split())]
            authors.extend(value for value in names if value and value not in authors)
        source_id = doi or joi or landing_url or title
        if title and source_id:
            records.append(
                PaperRecord(
                    source="jstage",
                    source_id=source_id,
                    title=title,
                    authors=tuple(authors),
                    published_at=updated,
                    updated_at=updated,
                    doi=doi,
                    journal=journal,
                    categories=("J-STAGE",),
                    landing_url=landing_url,
                    raw={"joi": joi, "provided_by": "J-STAGE/JST"},
                )
            )
    return records


def _jstage_date(value: str) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collect_jstage(config: dict[str, Any], profiles: list[dict[str, Any]], start: datetime, end: datetime) -> list[PaperRecord]:
    settings = config.get("sources", {}).get("jstage", {})
    if not settings.get("enabled", False):
        return []
    base_url = str(settings.get("base_url", "https://api.jstage.jst.go.jp/searchapi/do"))
    timeout = int(settings.get("timeout_seconds", 45))
    count = min(1000, max(1, int(settings.get("max_results_per_query", 200))))
    spacing = max(1.0, float(settings.get("request_spacing_seconds", 2)))
    user_agent = str(settings.get("user_agent", "research-paper-monitor/1.0"))
    records: list[PaperRecord] = []
    used = 0
    for profile in profiles:
        for raw_query in profile.get("jstage_queries") or []:
            query = str(raw_query or "").strip()
            if not query:
                continue
            if used:
                time.sleep(spacing)
            params = {
                "service": "3",
                "article": query,
                "pubyearfrom": str(start.year),
                "pubyearto": str(end.year),
                "count": str(count),
            }
            try:
                batch = parse_jstage_feed(_get(base_url + "?" + urllib.parse.urlencode(params), timeout, user_agent))
            except RateLimited as exc:
                exc.partial_records = records
                raise
            for record in batch:
                published = _jstage_date(record.published_at)
                if published and start.astimezone(timezone.utc) <= published.astimezone(timezone.utc) <= end.astimezone(timezone.utc):
                    records.append(record)
            used += 1
    return records
