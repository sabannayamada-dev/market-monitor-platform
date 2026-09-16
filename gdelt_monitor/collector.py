from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclasses.dataclass(frozen=True)
class QueryTask:
    query_id: str
    name: str
    query: str
    profile: str
    severity: int


class CollectorError(RuntimeError):
    error_type = "other_error"


class NonJsonResponse(CollectorError):
    error_type = "non_json"


class RateLimited(CollectorError):
    def __init__(self, status_code: int, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.error_type = f"http_{status_code}"


class RequestTimeout(CollectorError):
    error_type = "timeout"


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def build_tasks(config: dict[str, Any]) -> list[QueryTask]:
    tasks: list[QueryTask] = []
    for item in config.get("broad_queries", []):
        name = str(item["name"])
        query = str(item["query"])
        profile = str(item.get("profile", name))
        tasks.append(
            QueryTask(stable_id(name, query), name, query, profile, int(item.get("severity", 30)))
        )
    return tasks


def gdelt_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def fetch_once(
    task: QueryTask,
    start: datetime,
    end: datetime,
    max_records: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    params = {
        "query": task.query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max(1, min(250, max_records))),
        "startdatetime": gdelt_timestamp(start),
        "enddatetime": gdelt_timestamp(end),
        "sort": "DateDesc",
    }
    request = urllib.request.Request(
        ENDPOINT + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "personal-market-monitor/0.2 (+low-rate research use)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8-sig", "replace")
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            retry_after: int | None = None
            try:
                retry_after = int(float(exc.headers.get("Retry-After", ""))) if exc.headers else None
            except (TypeError, ValueError):
                retry_after = None
            suffix = f"; Retry-After={retry_after}s" if retry_after is not None else ""
            try:
                response_preview = " ".join(exc.read(240).decode("utf-8", "replace").split())
            except Exception:
                response_preview = ""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            diagnostics = f"; Content-Type={content_type!r}"
            if response_preview:
                diagnostics += f"; preview={response_preview!r}"
            raise RateLimited(
                exc.code,
                f"GDELT returned HTTP {exc.code}{suffix}{diagnostics}",
                retry_after,
            ) from exc
        raise CollectorError(f"GDELT returned HTTP {exc.code}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RequestTimeout("GDELT request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RequestTimeout("GDELT request timed out") from exc
        raise CollectorError(f"GDELT connection failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = " ".join(body[:240].split())
        raise NonJsonResponse(
            f"GDELT returned non-JSON; content_type={content_type!r}; preview={preview!r}"
        ) from exc
    articles = payload.get("articles")
    if articles is None:
        raise NonJsonResponse("GDELT JSON did not contain an articles field")
    return [article for article in articles if isinstance(article, dict)]


def fetch_with_backoff(
    task: QueryTask,
    start: datetime,
    end: datetime,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    retries = max(0, int(settings.get("max_retries", 1)))
    delay = max(1.0, float(settings.get("initial_backoff_seconds", 60)))
    for attempt in range(retries + 1):
        try:
            return fetch_once(
                task, start, end,
                int(settings.get("max_records_per_query", 25)),
                int(settings.get("request_timeout_seconds", 30)),
            )
        except RateLimited:
            raise
        except (CollectorError, RequestTimeout):
            if attempt >= retries:
                raise
            time.sleep(delay)
            delay = min(float(settings.get("max_backoff_seconds", 600)), delay * 2)
    raise AssertionError("unreachable")
