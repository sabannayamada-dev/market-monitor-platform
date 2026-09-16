from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

from analyze_patent_monitor_run import analyze as analyze_run
from audit_patent_monitor_backfill import write_csv
from patent_monitor.pipeline import (
    EPOOPSProvider,
    PatentPipeline,
    PatentRecord,
    PipelineConfig,
    read_companies,
    write_json_atomic,
)
from patent_monitor.secure_store import load_credentials


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs" / "patent_monitor_backfill_collector_v2"


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")


def load_collector_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if state_path.exists():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
    today = date.today().isoformat()
    return {
        "mode": "idle",
        "created_at": _now(),
        "updated_at": _now(),
        "next_end_date": today,
        "window_days": 30,
        "adaptive_company_windows": True,
        "low_volume_window_days": 90,
        "split_window_days": 7,
        "min_window_days": 1,
        "low_volume_max_records": 10,
        "company_window_days": {},
        "company_covered_start": {},
        "max_per_company": 50,
        "detail_max_records": 0,
        "company_search_name_limit": 12,
        "total_windows_completed": 0,
        "total_records_seen": 0,
        "total_records_kept": 0,
        "last_window": {"start": "", "end": "", "status": "", "run_id": ""},
        "stop_requested": False,
    }


def save_collector_state(path: str | Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    write_json_atomic(path, state)


def _collector_connection(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_collector_db(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = _collector_connection(db_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collected_patents (
                publication_number TEXT PRIMARY KEY,
                publication_date TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                applicants TEXT NOT NULL DEFAULT '',
                matched_company_id TEXT NOT NULL DEFAULT '',
                matched_company_name TEXT NOT NULL DEFAULT '',
                cpc_codes TEXT NOT NULL DEFAULT '',
                ipc_codes TEXT NOT NULL DEFAULT '',
                technology_score REAL NOT NULL DEFAULT 0,
                final_score REAL NOT NULL DEFAULT 0,
                route TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_windows (
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                input_records INTEGER NOT NULL DEFAULT 0,
                kept_records INTEGER NOT NULL DEFAULT 0,
                matched_count INTEGER NOT NULL DEFAULT 0,
                high_technology_unmatched_count INTEGER NOT NULL DEFAULT 0,
                suggested_alias_patch_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (window_start, window_end)
            )
            """
        )
        existing_window_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(collector_windows)")
        }
        for column, definition in (
            ("epo_failure_count", "INTEGER NOT NULL DEFAULT 0"),
            ("epo_throttle_count", "INTEGER NOT NULL DEFAULT 0"),
            ("capped_company_count", "INTEGER NOT NULL DEFAULT 0"),
            ("search_stats_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("detail_stats_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if column not in existing_window_columns:
                connection.execute(f"ALTER TABLE collector_windows ADD COLUMN {column} {definition}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_company_progress (
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                company_id TEXT NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (window_start, window_end, company_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_staged_records (
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                record_identity TEXT NOT NULL,
                company_id TEXT NOT NULL DEFAULT '',
                record_json TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                PRIMARY KEY (window_start, window_end, record_identity)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_collector_progress_window
            ON collector_company_progress(window_start, window_end, status)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_company_skip_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                company_id TEXT NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actual_window_start TEXT NOT NULL DEFAULT '',
                actual_window_end TEXT NOT NULL DEFAULT '',
                searched_names_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL DEFAULT '',
                skipped_at TEXT NOT NULL,
                elapsed_seconds INTEGER NOT NULL DEFAULT 0,
                runtime_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_company_skip_history_window
            ON collector_company_skip_history(window_start, window_end, company_id, skipped_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS epo_raw_payloads (
                payload_hash TEXT PRIMARY KEY,
                content_gzip BLOB NOT NULL,
                response_size INTEGER NOT NULL,
                first_retrieved_at TEXT NOT NULL,
                parser_version TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS epo_raw_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_hash TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                source_endpoint TEXT NOT NULL,
                query_window_start TEXT NOT NULL DEFAULT '',
                query_window_end TEXT NOT NULL DEFAULT '',
                company_id TEXT NOT NULL DEFAULT '',
                company_name TEXT NOT NULL DEFAULT '',
                publication_number TEXT NOT NULL DEFAULT '',
                constituent TEXT NOT NULL DEFAULT '',
                request_context_json TEXT NOT NULL,
                FOREIGN KEY(payload_hash) REFERENCES epo_raw_payloads(payload_hash)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_epo_raw_observation_window
            ON epo_raw_observations(query_window_start, query_window_end, company_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS normalized_patent_records (
                patent_id TEXT PRIMARY KEY,
                publication_number TEXT NOT NULL,
                application_number TEXT NOT NULL DEFAULT '',
                family_id TEXT NOT NULL DEFAULT '',
                publication_date TEXT NOT NULL DEFAULT '',
                filing_date TEXT NOT NULL DEFAULT '',
                priority_date TEXT NOT NULL DEFAULT '',
                retrieved_at TEXT NOT NULL DEFAULT '',
                source_endpoint TEXT NOT NULL DEFAULT '',
                query_window_start TEXT NOT NULL DEFAULT '',
                query_window_end TEXT NOT NULL DEFAULT '',
                payload_hash TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                citation_count INTEGER NOT NULL DEFAULT 0,
                family_size INTEGER NOT NULL DEFAULT 1,
                claim_count INTEGER NOT NULL DEFAULT 0,
                legal_status TEXT NOT NULL DEFAULT '',
                record_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_company_quality_history (
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                company_id TEXT NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                searched_names_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0,
                capped INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                throttle_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (window_start, window_end, company_id)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _completed_company_ids(db: str | Path, start: date, end: date) -> set[str]:
    connection = _collector_connection(db)
    try:
        rows = connection.execute(
            """
            SELECT company_id
            FROM collector_company_progress
            WHERE window_start = ? AND window_end = ? AND status IN ('completed', 'skipped_stalled')
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def _save_raw_epo_payload(db: str | Path, metadata: dict[str, Any], content: bytes) -> None:
    payload_hash = str(metadata.get("payload_hash") or hashlib.sha256(content).hexdigest())
    retrieved_at = str(metadata.get("retrieved_at") or _now())
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO epo_raw_payloads (
                payload_hash, content_gzip, response_size, first_retrieved_at, parser_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload_hash,
                gzip.compress(content, compresslevel=6),
                len(content),
                retrieved_at,
                str(metadata.get("parser_version") or ""),
            ),
        )
        connection.execute(
            """
            INSERT INTO epo_raw_observations (
                payload_hash, retrieved_at, source_endpoint, query_window_start, query_window_end,
                company_id, company_name, publication_number, constituent, request_context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload_hash,
                retrieved_at,
                str(metadata.get("source_endpoint") or ""),
                str(metadata.get("query_window_start") or ""),
                str(metadata.get("query_window_end") or ""),
                str(metadata.get("company_id") or ""),
                str(metadata.get("company_name") or ""),
                str(metadata.get("publication_number") or ""),
                str(metadata.get("constituent") or ""),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _persist_normalized_records(
    db: str | Path,
    records: list[PatentRecord],
    start: date,
    end: date,
) -> int:
    now = _now()
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            if not record.query_window_start:
                record.query_window_start = start.isoformat()
            if not record.query_window_end:
                record.query_window_end = end.isoformat()
            connection.execute(
                """
                INSERT INTO normalized_patent_records (
                    patent_id, publication_number, application_number, family_id,
                    publication_date, filing_date, priority_date, retrieved_at,
                    source_endpoint, query_window_start, query_window_end, payload_hash,
                    parser_version, citation_count, family_size, claim_count, legal_status,
                    record_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(patent_id) DO UPDATE SET
                    publication_number=excluded.publication_number,
                    application_number=excluded.application_number,
                    family_id=excluded.family_id,
                    publication_date=excluded.publication_date,
                    filing_date=excluded.filing_date,
                    priority_date=excluded.priority_date,
                    retrieved_at=CASE WHEN normalized_patent_records.retrieved_at=''
                        THEN excluded.retrieved_at ELSE normalized_patent_records.retrieved_at END,
                    source_endpoint=excluded.source_endpoint,
                    query_window_start=excluded.query_window_start,
                    query_window_end=excluded.query_window_end,
                    payload_hash=excluded.payload_hash,
                    parser_version=excluded.parser_version,
                    citation_count=excluded.citation_count,
                    family_size=excluded.family_size,
                    claim_count=excluded.claim_count,
                    legal_status=excluded.legal_status,
                    record_json=excluded.record_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    record.identity,
                    record.publication_number,
                    record.application_number,
                    record.family_id,
                    record.publication_date,
                    record.filing_date,
                    record.priority_date,
                    record.retrieved_at,
                    record.source_endpoint,
                    record.query_window_start,
                    record.query_window_end,
                    record.payload_hash,
                    record.parser_version,
                    record.citation_count,
                    record.family_size,
                    record.claim_count,
                    record.legal_status,
                    json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"), default=str),
                    now,
                    now,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(records)


def _restore_window_context(
    db: str | Path,
    records: list[PatentRecord],
    start: date,
    end: date,
) -> int:
    """Restore window metadata when resuming records staged by an older collector."""
    window_start = start.isoformat()
    window_end = end.isoformat()
    publication_numbers: set[str] = set()
    for record in records:
        if not record.query_window_start:
            record.query_window_start = window_start
        if not record.query_window_end:
            record.query_window_end = window_end
        if record.publication_number:
            publication_numbers.add(record.publication_number)

    if not publication_numbers:
        return 0

    connection = _collector_connection(db)
    repaired = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        for publication_number in publication_numbers:
            cursor = connection.execute(
                """
                UPDATE epo_raw_observations
                SET query_window_start = ?, query_window_end = ?
                WHERE publication_number = ?
                  AND (query_window_start = '' OR query_window_end = '')
                """,
                (window_start, window_end, publication_number),
            )
            repaired += max(0, cursor.rowcount)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repaired


def _stage_company_records(
    db: str | Path,
    start: date,
    end: date,
    company_id: str,
    company_name: str,
    records: list[PatentRecord],
    searched_names: list[str] | None = None,
) -> int:
    now = _now()
    window_start = start.isoformat()
    window_end = end.isoformat()
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            payload = json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"), default=str)
            connection.execute(
                """
                INSERT INTO collector_staged_records (
                    window_start, window_end, record_identity, company_id, record_json, saved_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(window_start, window_end, record_identity) DO UPDATE SET
                    company_id=excluded.company_id,
                    record_json=excluded.record_json,
                    saved_at=excluded.saved_at
                """,
                (window_start, window_end, record.identity, company_id, payload, now),
            )
        connection.execute(
            """
            INSERT INTO collector_company_progress (
                window_start, window_end, company_id, company_name, status, record_count, completed_at
            )
            VALUES (?, ?, ?, ?, 'completed', ?, ?)
            ON CONFLICT(window_start, window_end, company_id) DO UPDATE SET
                company_name=excluded.company_name,
                status=excluded.status,
                record_count=excluded.record_count,
                completed_at=excluded.completed_at
            """,
            (window_start, window_end, company_id, company_name, len(records), now),
        )
        connection.execute(
            """
            INSERT INTO collector_company_quality_history (
                window_start, window_end, company_id, company_name, searched_names_json,
                status, record_count, started_at, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
            ON CONFLICT(window_start, window_end, company_id) DO UPDATE SET
                company_name=excluded.company_name,
                searched_names_json=excluded.searched_names_json,
                status=excluded.status,
                record_count=excluded.record_count,
                retry_count=collector_company_quality_history.retry_count+1,
                completed_at=excluded.completed_at,
                updated_at=excluded.updated_at
            """,
            (
                window_start,
                window_end,
                company_id,
                company_name,
                json.dumps(searched_names or [], ensure_ascii=False),
                len(records),
                now,
                now,
                now,
            ),
        )
        completed_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM collector_company_progress
                WHERE window_start = ? AND window_end = ? AND status IN ('completed', 'skipped_stalled')
                """,
                (window_start, window_end),
            ).fetchone()[0]
        )
        connection.commit()
        return completed_count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_company_skip(
    db: str | Path,
    start: date,
    end: date,
    company_id: str,
    company_name: str,
    status: str,
    reason: str,
    actual_start: date,
    actual_end: date,
    searched_names: list[str],
    started_at: str,
    elapsed_seconds: int,
    runtime: dict[str, Any],
) -> None:
    now = _now()
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO collector_company_progress (
                window_start, window_end, company_id, company_name, status, record_count, completed_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(window_start, window_end, company_id) DO UPDATE SET
                company_name=excluded.company_name,
                status=excluded.status,
                record_count=excluded.record_count,
                completed_at=excluded.completed_at
            """,
            (start.isoformat(), end.isoformat(), company_id, company_name, status, now),
        )
        connection.execute(
            """
            INSERT INTO collector_company_quality_history (
                window_start, window_end, company_id, company_name, searched_names_json,
                status, record_count, capped, failure_count, retry_count, throttle_count,
                started_at, completed_at, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 1, 0, 0, ?, ?, ?, ?)
            ON CONFLICT(window_start, window_end, company_id) DO UPDATE SET
                company_name=excluded.company_name,
                searched_names_json=excluded.searched_names_json,
                status=excluded.status,
                error=excluded.error,
                completed_at=excluded.completed_at,
                updated_at=excluded.updated_at
            """,
            (
                start.isoformat(),
                end.isoformat(),
                company_id,
                company_name,
                json.dumps(searched_names, ensure_ascii=False),
                status,
                started_at,
                now,
                reason,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO collector_company_skip_history (
                window_start, window_end, company_id, company_name, status, reason,
                actual_window_start, actual_window_end, searched_names_json,
                started_at, skipped_at, elapsed_seconds, runtime_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                start.isoformat(),
                end.isoformat(),
                company_id,
                company_name,
                status,
                reason,
                actual_start.isoformat(),
                actual_end.isoformat(),
                json.dumps(searched_names, ensure_ascii=False),
                started_at,
                now,
                elapsed_seconds,
                json.dumps(runtime, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _archive_search_quality(
    db: str | Path,
    start: date,
    end: date,
    stats: dict[str, Any],
    started_at: str,
) -> None:
    now = _now()
    companies = list(stats.get("companies") or [])
    skipped = list(stats.get("skipped_companies") or [])
    rows = companies + [
        {
            **item,
            "success": False,
            "record_count": int(item.get("record_count") or 0),
            "capped": bool(item.get("capped")),
            "searched_names": item.get("searched_names") or [],
            "error": item.get("reason") or item.get("error") or "not processed before collection stopped",
        }
        for item in skipped
        if item.get("company_id")
    ]
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in rows:
            company_id = str(item.get("company_id") or "")
            if not company_id:
                continue
            status = "completed" if item.get("success") else str(item.get("status") or "failed_or_skipped")
            connection.execute(
                """
                INSERT INTO collector_company_quality_history (
                    window_start, window_end, company_id, company_name, searched_names_json,
                    status, record_count, capped, failure_count, retry_count, throttle_count,
                    started_at, completed_at, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(window_start, window_end, company_id) DO UPDATE SET
                    company_name=excluded.company_name,
                    searched_names_json=CASE WHEN excluded.searched_names_json='[]'
                        THEN collector_company_quality_history.searched_names_json
                        ELSE excluded.searched_names_json END,
                    status=excluded.status,
                    record_count=excluded.record_count,
                    capped=excluded.capped,
                    failure_count=excluded.failure_count,
                    throttle_count=collector_company_quality_history.throttle_count+excluded.throttle_count,
                    completed_at=excluded.completed_at,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    company_id,
                    str(item.get("company_name") or ""),
                    json.dumps(item.get("searched_names") or [], ensure_ascii=False),
                    status,
                    int(item.get("record_count") or 0),
                    int(bool(item.get("capped"))),
                    int(not bool(item.get("success"))),
                    int(stats.get("throttle_pause_count") or 0),
                    started_at,
                    now if item.get("success") or status == "skipped_stalled" else "",
                    str(item.get("error") or "")[:1500],
                    now,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _migrate_zero_result_checkpoint(
    db: str | Path,
    checkpoint_path: str | Path,
    start: date,
    end: date,
) -> int:
    path = Path(checkpoint_path)
    if not path.exists():
        return 0
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if checkpoint.get("start_date") != start.isoformat() or checkpoint.get("end_date") != end.isoformat():
        return 0
    before = _completed_company_ids(db, start, end)
    for item in checkpoint.get("companies", []):
        company_id = str(item.get("company_id") or "").strip()
        if not company_id or item.get("success") is not True:
            continue
        try:
            record_count = int(item.get("record_count") or 0)
        except (TypeError, ValueError):
            continue
        if record_count == 0:
            _stage_company_records(
                db,
                start,
                end,
                company_id,
                str(item.get("company_name") or ""),
                [],
            )
    after = _completed_company_ids(db, start, end)
    return len(after - before)


def _load_staged_records(db: str | Path, start: date, end: date) -> list[PatentRecord]:
    connection = _collector_connection(db)
    try:
        rows = connection.execute(
            """
            SELECT record_json FROM collector_staged_records
            WHERE window_start = ? AND window_end = ?
            ORDER BY record_identity
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    allowed_fields = {item.name for item in fields(PatentRecord)}
    records: list[PatentRecord] = []
    for row in rows:
        value = json.loads(str(row[0]))
        records.append(PatentRecord(**{key: item for key, item in value.items() if key in allowed_fields}))
    return records


def _clear_window_staging(db: str | Path, start: date, end: date) -> None:
    connection = _collector_connection(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parameters = (start.isoformat(), end.isoformat())
        connection.execute(
            "DELETE FROM collector_staged_records WHERE window_start = ? AND window_end = ?",
            parameters,
        )
        connection.execute(
            "DELETE FROM collector_company_progress WHERE window_start = ? AND window_end = ?",
            parameters,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def save_minimal_patents(
    db: str | Path,
    evaluation_csv: str | Path,
    run_id: str,
    high_technology_threshold: float = 24.0,
) -> int:
    rows = _read_csv_rows(evaluation_csv)
    now = _now()
    kept = 0
    connection = _collector_connection(db)
    try:
        for row in rows:
            route = row.get("route", "")
            technology = float(row.get("technology_score") or 0)
            keep = (
                bool(row.get("company_id"))
                or technology >= high_technology_threshold
                or route.startswith(("gpt", "gemini"))
                or route.endswith("_limit")
                or route.endswith("_error")
            )
            if not keep:
                continue
            publication_number = row.get("publication_number", "").strip()
            if not publication_number:
                continue
            kept += 1
            connection.execute(
                """
                INSERT INTO collected_patents (
                    publication_number, publication_date, title, applicants,
                    matched_company_id, matched_company_name, cpc_codes, ipc_codes,
                    technology_score, final_score, route, source_url,
                    first_seen_at, last_seen_at, run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(publication_number) DO UPDATE SET
                    publication_date=excluded.publication_date,
                    title=excluded.title,
                    applicants=excluded.applicants,
                    matched_company_id=excluded.matched_company_id,
                    matched_company_name=excluded.matched_company_name,
                    cpc_codes=excluded.cpc_codes,
                    ipc_codes=excluded.ipc_codes,
                    technology_score=excluded.technology_score,
                    final_score=excluded.final_score,
                    route=excluded.route,
                    source_url=excluded.source_url,
                    last_seen_at=excluded.last_seen_at,
                    run_id=excluded.run_id
                """,
                (
                    publication_number,
                    row.get("publication_date", ""),
                    row.get("title", ""),
                    row.get("applicants", ""),
                    row.get("company_id", ""),
                    row.get("company_name", ""),
                    row.get("cpc_codes", ""),
                    row.get("ipc_codes", ""),
                    float(row.get("technology_score") or 0),
                    float(row.get("final_score") or 0),
                    row.get("route", ""),
                    row.get("source_url", ""),
                    now,
                    now,
                    run_id,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return kept


def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.write_bytes(source.read_bytes())


def _record_window(
    db: Path,
    start: date,
    end: date,
    status: str,
    started_at: str,
    completed_at: str = "",
    input_records: int = 0,
    kept_records: int = 0,
    matched_count: int = 0,
    high_technology_unmatched_count: int = 0,
    suggested_alias_patch_count: int = 0,
    error: str = "",
    search_stats: dict[str, Any] | None = None,
    detail_stats: dict[str, Any] | None = None,
) -> None:
    search_stats = search_stats or {}
    detail_stats = detail_stats or {}
    connection = _collector_connection(db)
    try:
        connection.execute(
            """
            INSERT INTO collector_windows (
                window_start, window_end, status, started_at, completed_at, input_records,
                kept_records, matched_count, high_technology_unmatched_count,
                suggested_alias_patch_count, error, epo_failure_count, epo_throttle_count,
                capped_company_count, search_stats_json, detail_stats_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_start, window_end) DO UPDATE SET
                status=excluded.status,
                completed_at=excluded.completed_at,
                input_records=excluded.input_records,
                kept_records=excluded.kept_records,
                matched_count=excluded.matched_count,
                high_technology_unmatched_count=excluded.high_technology_unmatched_count,
                suggested_alias_patch_count=excluded.suggested_alias_patch_count,
                error=excluded.error,
                epo_failure_count=excluded.epo_failure_count,
                epo_throttle_count=excluded.epo_throttle_count,
                capped_company_count=excluded.capped_company_count,
                search_stats_json=excluded.search_stats_json,
                detail_stats_json=excluded.detail_stats_json
            """,
            (
                start.isoformat(),
                end.isoformat(),
                status,
                started_at,
                completed_at,
                input_records,
                kept_records,
                matched_count,
                high_technology_unmatched_count,
                suggested_alias_patch_count,
                error[:1500],
                int(search_stats.get("failure_count") or 0),
                int(search_stats.get("throttle_pause_count") or 0),
                len(search_stats.get("capped_companies") or []),
                json.dumps(search_stats, ensure_ascii=False, separators=(",", ":"), default=str),
                json.dumps(detail_stats, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _existing_window_status(db: Path, start: date, end: date) -> str:
    connection = _collector_connection(db)
    try:
        row = connection.execute(
            "SELECT status FROM collector_windows WHERE window_start = ? AND window_end = ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


def _window_area_kind(existing_status: str) -> str:
    if existing_status in {"interrupted", "error", "running"}:
        return "欠損補完"
    if existing_status == "completed":
        return "完了済み再処理"
    return "新規領域"


def _session_days(completed_windows: int, window_days: int) -> int:
    return completed_windows * window_days


def _adaptive_rate_initial(state: dict[str, Any], configured_rate: int) -> int:
    saved = int(state.get("collector_ops_requests_per_minute") or configured_rate)
    return max(1, min(configured_rate, saved))


def _adaptive_rate_after_throttle(current_rate: int) -> int:
    return max(1, current_rate - 1)


def _adaptive_rate_after_stable_window(current_rate: int, configured_rate: int) -> int:
    return min(configured_rate, current_rate + 1)


def _append_retry_rows(path: Path, window_start: date, window_end: date, search_stats: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    for default_reason, key in (("hit_max_per_company", "capped_companies"), ("stopped_by_time_guard", "skipped_companies")):
        for row in search_stats.get(key, []):
            reason = str(row.get("status") or row.get("reason") or default_reason)
            rows.append(
                {
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "company_id": row.get("company_id", ""),
                    "company_name": row.get("company_name", ""),
                    "record_count": row.get("record_count", ""),
                    "max_per_company": search_stats.get("max_per_company", ""),
                    "searched_names": " | ".join(row.get("searched_names", [])),
                    "retry_reason": reason,
                }
            )
    if not rows:
        return
    existing = _read_csv_rows(path) if path.exists() else []
    merged: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in [*existing, *rows]:
        key = (
            str(row.get("window_start", "")),
            str(row.get("window_end", "")),
            str(row.get("company_id", "")),
            str(row.get("company_name", "")),
            str(row.get("retry_reason", "")),
        )
        merged[key] = row
    write_csv(
        path,
        list(merged.values()),
        ["window_start", "window_end", "company_id", "company_name", "record_count", "max_per_company", "searched_names", "retry_reason"],
    )


def _range_days(start: date, end: date) -> int:
    return (end - start).days + 1


def _backward_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = end
    step = max(1, days)
    while cursor >= start:
        chunk_start = max(start, cursor - timedelta(days=step - 1))
        chunks.append((chunk_start, cursor))
        cursor = chunk_start - timedelta(days=1)
    return list(reversed(chunks))


def _empty_company_search_stats(start: date, end: date, max_per_company: int, name_limit: int) -> dict[str, Any]:
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "max_per_company": max_per_company,
        "max_applicant_names": name_limit,
        "companies": [],
        "capped_companies": [],
        "skipped_companies": [],
        "failure_count": 0,
        "stopped_by_deadline": False,
        "stopped_by_request": False,
        "stopped_by_throttle": False,
        "throttle_error": "",
        "throttle_pause_count": 0,
        "current_requests_per_minute": 0,
        "unique_records": 0,
        "total_target_companies": 0,
        "resumed_company_count": 0,
        "adaptive_company_windows": True,
        "staged_windows": [],
    }


def _merge_company_search_stats(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["companies"].extend(source.get("companies") or [])
    target["capped_companies"].extend(source.get("capped_companies") or [])
    target["skipped_companies"].extend(source.get("skipped_companies") or [])
    target["failure_count"] = int(target.get("failure_count") or 0) + int(source.get("failure_count") or 0)
    target["throttle_pause_count"] = int(target.get("throttle_pause_count") or 0) + int(source.get("throttle_pause_count") or 0)
    target["stopped_by_deadline"] = bool(target.get("stopped_by_deadline") or source.get("stopped_by_deadline"))
    target["stopped_by_request"] = bool(target.get("stopped_by_request") or source.get("stopped_by_request"))
    target["stopped_by_throttle"] = bool(target.get("stopped_by_throttle") or source.get("stopped_by_throttle"))
    if source.get("throttle_error"):
        target["throttle_error"] = source["throttle_error"]
    if source.get("current_requests_per_minute"):
        target["current_requests_per_minute"] = source["current_requests_per_minute"]


def _load_staged_records_for_company(
    db: str | Path,
    start: date,
    end: date,
    company_id: str,
) -> list[PatentRecord]:
    connection = _collector_connection(db)
    try:
        rows = connection.execute(
            """
            SELECT record_json FROM collector_staged_records
            WHERE window_start = ? AND window_end = ? AND company_id = ?
            """,
            (start.isoformat(), end.isoformat(), company_id),
        ).fetchall()
    finally:
        connection.close()
    records: list[PatentRecord] = []
    valid_fields = {field.name for field in fields(PatentRecord)}
    for (payload,) in rows:
        try:
            data = json.loads(payload)
            records.append(PatentRecord(**{key: data[key] for key in data if key in valid_fields}))
        except (TypeError, ValueError):
            continue
    return records


def _search_company_range_adaptive(
    provider: EPOOPSProvider,
    db: str | Path,
    company: Any,
    start: date,
    end: date,
    max_per_company: int,
    name_limit: int,
    split_window_days: int,
    min_window_days: int,
    company_stall_skip_seconds: int,
    stop_requested: Callable[[], bool],
    progress: Callable[[str, float], None],
    aggregate_stats: dict[str, Any],
    staged_windows: set[tuple[str, str]],
) -> tuple[list[PatentRecord], bool]:
    if company.company_id in _completed_company_ids(db, start, end):
        records = _load_staged_records_for_company(db, start, end, company.company_id)
        aggregate_stats["resumed_company_count"] = int(aggregate_stats.get("resumed_company_count") or 0) + 1
        return records, False

    records = provider.search_companies(
        [company],
        start.isoformat(),
        end.isoformat(),
        max_per_company=max_per_company,
        max_applicant_names=name_limit,
        deadline_monotonic=(
            time.monotonic() + company_stall_skip_seconds
            if company_stall_skip_seconds > 0
            else None
        ),
        progress=lambda message: progress(message, 0.15),
        stop_requested=stop_requested,
    )
    stats = dict(provider.last_company_search_stats or {})
    _merge_company_search_stats(aggregate_stats, stats)
    skipped_current = [
        item for item in stats.get("skipped_companies") or []
        if item.get("company_id") == company.company_id and item.get("status") == "skipped_stalled"
    ]
    if skipped_current:
        names = company.search_names(name_limit)
        reason = str(skipped_current[-1].get("reason") or "skipped_stalled")
        _record_company_skip(
            db,
            start,
            end,
            company.company_id,
            company.company_name,
            "skipped_stalled",
            reason,
            start,
            end,
            names,
            _now(),
            company_stall_skip_seconds,
            {
                "company_id": company.company_id,
                "company_name": company.company_name,
                "actual_window_start": start.isoformat(),
                "actual_window_end": end.isoformat(),
                "reason": reason,
            },
        )
        progress(f"会社を時間超過でスキップ: {company.company_name} ({reason})", 0.15)
        return [], False
    capped = bool(stats.get("capped_companies"))
    if stats.get("stopped_by_request") or stats.get("stopped_by_throttle"):
        return records, capped

    span = _range_days(start, end)
    if capped and span > max(1, min_window_days):
        next_days = split_window_days if span > split_window_days else min_window_days
        split_records: list[PatentRecord] = []
        any_capped = False
        for chunk_start, chunk_end in _backward_chunks(start, end, next_days):
            chunk_records, chunk_capped = _search_company_range_adaptive(
                provider,
                db,
                company,
                chunk_start,
                chunk_end,
                max_per_company,
                name_limit,
                split_window_days,
                min_window_days,
                company_stall_skip_seconds,
                stop_requested,
                progress,
                aggregate_stats,
                staged_windows,
            )
            split_records.extend(chunk_records)
            any_capped = any_capped or chunk_capped
            if stop_requested() or aggregate_stats.get("stopped_by_request") or aggregate_stats.get("stopped_by_throttle"):
                break
        return split_records, any_capped

    _stage_company_records(
        db,
        start,
        end,
        company.company_id,
        company.company_name,
        records,
        company.search_names(name_limit),
    )
    staged_windows.add((start.isoformat(), end.isoformat()))
    for item in aggregate_stats.get("companies") or []:
        if item.get("company_id") == company.company_id and item.get("record_count") == len(records):
            item["actual_window_start"] = start.isoformat()
            item["actual_window_end"] = end.isoformat()
    return records, capped


def _search_companies_adaptive(
    provider: EPOOPSProvider,
    db: str | Path,
    companies: list[Any],
    start: date,
    end: date,
    state: dict[str, Any],
    state_path: Path | None,
    max_per_company: int,
    name_limit: int,
    base_window_days: int,
    low_volume_window_days: int,
    low_volume_max_records: int,
    split_window_days: int,
    min_window_days: int,
    company_stall_skip_seconds: int,
    stop_requested: Callable[[], bool],
    progress: Callable[[str, float], None],
) -> tuple[list[PatentRecord], dict[str, Any]]:
    target_companies = [company for company in companies if company.target]
    stats = _empty_company_search_stats(start, end, max_per_company, name_limit)
    stats["total_target_companies"] = len(target_companies)
    company_window_days = dict(state.get("company_window_days") or {})
    company_covered_start = dict(state.get("company_covered_start") or {})
    staged_windows: set[tuple[str, str]] = set()
    found: dict[str, PatentRecord] = {}

    for index, company in enumerate(target_companies, 1):
        if stop_requested() or stats.get("stopped_by_request") or stats.get("stopped_by_throttle"):
            stats["stopped_by_request"] = bool(stop_requested() or stats.get("stopped_by_request"))
            stats["skipped_companies"].extend(
                {"company_id": item.company_id, "company_name": item.company_name}
                for item in target_companies[index - 1 :]
            )
            break
        covered_start_text = str(company_covered_start.get(company.company_id) or "")
        if covered_start_text:
            try:
                covered_start = date.fromisoformat(covered_start_text)
            except ValueError:
                covered_start = None
            if covered_start is not None and end >= covered_start:
                stats["skipped_companies"].append({
                    "company_id": company.company_id,
                    "company_name": company.company_name,
                    "reason": f"covered by expanded window through {covered_start.isoformat()}",
                })
                continue

        preferred_days = int(company_window_days.get(company.company_id) or base_window_days)
        preferred_days = max(base_window_days, min(low_volume_window_days, preferred_days))
        actual_start = end - timedelta(days=preferred_days - 1)
        state["runtime"] = {
            "phase": "company_search",
            "status": "running",
            "updated_at": _now(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "company_index": index,
            "company_total": len(target_companies),
            "company_id": company.company_id,
            "company_name": company.company_name,
            "actual_window_start": actual_start.isoformat(),
            "actual_window_end": end.isoformat(),
            "preferred_days": preferred_days,
            "window_new_records": 0,
            "window_total_records": len(found),
            "message": (
                f"Adaptive EPO search {index}/{len(target_companies)}社: "
                f"{company.company_name} {actual_start}..{end} ({preferred_days}日)"
            ),
        }
        if state_path is not None:
            save_collector_state(state_path, state)
        progress(
            f"Adaptive EPO search {index}/{len(target_companies)}社: "
            f"{company.company_name} {actual_start}..{end} ({preferred_days}日)",
            0.10,
        )
        before_unique_records = len(found)
        company_records, still_capped = _search_company_range_adaptive(
            provider,
            db,
            company,
            actual_start,
            end,
            max_per_company,
            name_limit,
            split_window_days,
            min_window_days,
            company_stall_skip_seconds,
            stop_requested,
            progress,
            stats,
            staged_windows,
        )
        for record in company_records:
            found[record.identity] = record
        window_new_records = len(found) - before_unique_records
        progress(
            f"Adaptive EPO window total {index}/{len(target_companies)} company: "
            f"{company.company_name} company_candidates={len(company_records)} "
            f"window_new={window_new_records} window_total={len(found)} "
            f"capped={int(bool(still_capped))}",
            0.10,
        )
        state["runtime"] = {
            **dict(state.get("runtime") or {}),
            "status": "completed",
            "updated_at": _now(),
            "record_count": len(company_records),
            "company_candidate_records": len(company_records),
            "window_new_records": window_new_records,
            "window_total_records": len(found),
            "still_capped": bool(still_capped),
        }
        if state_path is not None:
            save_collector_state(state_path, state)

        if still_capped:
            company_window_days[company.company_id] = base_window_days
            company_covered_start.pop(company.company_id, None)
        elif len(company_records) <= low_volume_max_records:
            company_window_days[company.company_id] = low_volume_window_days
            if preferred_days > base_window_days:
                company_covered_start[company.company_id] = actual_start.isoformat()
        else:
            company_window_days[company.company_id] = base_window_days
            company_covered_start.pop(company.company_id, None)

    state["company_window_days"] = company_window_days
    state["company_covered_start"] = company_covered_start
    stats["unique_records"] = len(found)
    stats["staged_windows"] = [
        {"window_start": item[0], "window_end": item[1]} for item in sorted(staged_windows)
    ]
    provider.last_company_search_stats = stats
    return list(found.values()), stats


def _window_was_interrupted(result: dict[str, Any], stop_requested: Callable[[], bool]) -> bool:
    family_count = int(result.get("family_count") or 0)
    scored_count = int(result.get("scored_count") or 0)
    return bool(result.get("interrupted")) or (stop_requested() and scored_count < family_count)


def _stop_window(
    db_path: Path,
    state_path: Path,
    summary_path: Path,
    output_dir: Path,
    state: dict[str, Any],
    window_start: date,
    window_end: date,
    started_at: str,
    input_records: int,
    area_kind: str,
    days_completed: int,
    reason: str,
    progress: Callable[[str, float], None],
    result: dict[str, Any] | None = None,
    search_stats: dict[str, Any] | None = None,
    detail_summary: dict[str, Any] | None = None,
) -> None:
    _record_window(
        db_path,
        window_start,
        window_end,
        "interrupted",
        started_at,
        _now(),
        input_records,
        0,
        0,
        0,
        0,
        reason,
        search_stats=search_stats,
        detail_stats=detail_summary,
    )
    state["last_window"] = {
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "status": "interrupted",
        "run_id": (result or {}).get("run_id", ""),
        "area_kind": area_kind,
    }
    state["mode"] = "idle"
    state["current_area_kind"] = area_kind
    state["session_days_completed"] = days_completed
    save_collector_state(state_path, state)
    write_json_atomic(
        summary_path,
        {
            "created_at": _now(),
            "collector_dir": str(output_dir.resolve()),
            "state": state,
            "last_result": result or {},
            "company_search": search_stats or {},
            "detail_enrichment": detail_summary,
            "collector_ops_requests_per_minute": state.get("collector_ops_requests_per_minute", ""),
            "interrupted": True,
            "interruption_reason": reason,
        },
    )
    progress(
        f"放置収集 中断: {window_start}〜{window_end} / {area_kind} / "
        f"今回処理済み {days_completed}日（この窓は次回やり直し）",
        1.0,
    )


def run_collector(
    args: argparse.Namespace,
    stop_requested: Callable[[], bool] | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    stop_requested = stop_requested or (lambda: False)
    downstream_progress = progress or (lambda _message, _ratio: None)
    output_dir = Path(args.output_dir or DEFAULT_OUTPUT)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "collector_state.json"
    db_path = output_dir / "collector.sqlite3"
    log_path = output_dir / "collector.log"
    summary_path = output_dir / "latest_summary.json"
    retry_queue_path = output_dir / "capped_companies_retry_queue.csv"
    init_collector_db(db_path)
    state = load_collector_state(state_path)

    def progress(message: str, ratio: float) -> None:
        now = _now()
        state["last_progress"] = {
            "updated_at": now,
            "message": str(message),
            "ratio": float(ratio),
        }
        runtime = dict(state.get("runtime") or {})
        if runtime.get("status") == "running":
            runtime["updated_at"] = now
            runtime["message"] = str(message)
            runtime["ratio"] = float(ratio)
            state["runtime"] = runtime
        try:
            save_collector_state(state_path, state)
        except OSError:
            pass
        downstream_progress(message, ratio)

    state.update(
        {
            "mode": "running",
            "window_days": int(args.window_days),
            "adaptive_company_windows": bool(args.adaptive_company_windows),
            "low_volume_window_days": int(args.low_volume_window_days),
            "split_window_days": int(args.split_window_days),
            "min_window_days": int(args.min_window_days),
            "low_volume_max_records": int(args.low_volume_max_records),
            "max_per_company": int(args.max_per_company),
            "detail_max_records": int(args.detail_max_records),
            "company_search_name_limit": int(args.company_search_name_limit),
            "company_stall_skip_seconds": int(args.company_stall_skip_seconds),
            "stop_requested": False,
        }
    )
    save_collector_state(state_path, state)

    def log(message: str) -> None:
        line = f"{_now()} {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    try:
        secrets = load_credentials()
    except Exception:
        secrets = {}
    missing = [key for key in ("epo_ops_key", "epo_ops_secret") if not secrets.get(key)]
    if missing:
        state["mode"] = "error"
        state["last_window"]["status"] = "missing_credentials"
        save_collector_state(state_path, state)
        raise RuntimeError("Missing EPO credentials: " + ", ".join(missing))

    config = PipelineConfig.from_json(args.config)
    config.daily_digest_enabled = False
    config.urgent_notifications_enabled = False
    config.random_reject_audit_rate = 0.0
    config.ranked_ai_enabled = bool(args.ranked_ai_enabled)
    config.ranked_gpt_top_rate = float(args.gpt_top_rate)
    config.ranked_gpt_audit_rate = float(args.gpt_audit_rate)
    config.ranked_gemini_top_rate = float(args.gemini_top_rate)
    config.annual_ai_budget_jpy = float(args.annual_ai_budget_jpy)
    config.ops_detail_max_records = int(args.detail_max_records)
    config.ops_company_search_name_limit = int(args.company_search_name_limit)
    config.ops_claims_fulltext_top_rate = float(args.claims_fulltext_top_rate)
    companies = read_companies(args.company_master)
    target_company_count = sum(1 for company in companies if company.target)
    configured_requests_per_minute = max(1, min(9, int(config.ops_requests_per_minute)))
    collector_requests_per_minute = _adaptive_rate_initial(state, configured_requests_per_minute)
    state["collector_ops_requests_per_minute"] = collector_requests_per_minute
    save_collector_state(state_path, state)
    provider = EPOOPSProvider(
        secrets["epo_ops_key"],
        secrets["epo_ops_secret"],
        collector_requests_per_minute,
        config.request_timeout_seconds,
    )
    def raw_payload_callback(metadata: dict[str, Any], content: bytes) -> None:
        _save_raw_epo_payload(db_path, metadata, content)
        runtime = dict(state.get("runtime") or {})
        if runtime.get("status") == "running":
            runtime.update(
                {
                    "updated_at": _now(),
                    "last_epo_at": str(metadata.get("retrieved_at") or ""),
                    "last_epo_endpoint": str(metadata.get("source_endpoint") or ""),
                    "last_epo_company_id": str(metadata.get("company_id") or ""),
                    "last_epo_company_name": str(metadata.get("company_name") or ""),
                    "last_epo_publication_number": str(metadata.get("publication_number") or ""),
                    "last_epo_constituent": str(metadata.get("constituent") or ""),
                }
            )
            state["runtime"] = runtime
            try:
                save_collector_state(state_path, state)
            except OSError:
                pass

    provider.set_raw_payload_callback(raw_payload_callback)
    if config.ops_detail_cache_enabled:
        provider.set_detail_cache(config.ops_detail_cache_path or (output_dir / "epo_detail_cache.sqlite3"))

    completed_this_session = 0
    window_days = int(args.window_days)
    try:
        while True:
            if stop_requested() or state.get("stop_requested"):
                state["mode"] = "idle"
                save_collector_state(state_path, state)
                break
            window_end = date.fromisoformat(str(state["next_end_date"]))
            window_start = window_end - timedelta(days=window_days - 1)
            started_at = _now()
            existing_status = _existing_window_status(db_path, window_start, window_end)
            area_kind = _window_area_kind(existing_status)
            days_before = _session_days(completed_this_session, window_days)
            state["current_area_kind"] = area_kind
            state["session_days_completed"] = days_before
            state["last_window"] = {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "status": "running",
                "run_id": "",
                "area_kind": area_kind,
            }
            save_collector_state(state_path, state)
            _record_window(db_path, window_start, window_end, "running", started_at)
            start_message = (
                f"放置収集: {window_start}〜{window_end} / {area_kind} / "
                f"今回処理済み {days_before}日 / EPO {collector_requests_per_minute} req/min"
            )
            log(start_message)
            progress(start_message, 0.0)
            try:
                migrated_count = _migrate_zero_result_checkpoint(
                    db_path,
                    output_dir / "company_search_checkpoint.json",
                    window_start,
                    window_end,
                )
                if migrated_count:
                    log(
                        f"Legacy checkpoint migrated: {migrated_count} zero-result companies "
                        f"for {window_start}..{window_end}"
                    )
                if args.adaptive_company_windows:
                    records, search_stats = _search_companies_adaptive(
                        provider,
                        db_path,
                        companies,
                        window_start,
                        window_end,
                        state,
                        state_path,
                        max_per_company=int(args.max_per_company),
                        name_limit=int(args.company_search_name_limit),
                        base_window_days=int(args.window_days),
                        low_volume_window_days=int(args.low_volume_window_days),
                        low_volume_max_records=int(args.low_volume_max_records),
                        split_window_days=int(args.split_window_days),
                        min_window_days=int(args.min_window_days),
                        company_stall_skip_seconds=int(args.company_stall_skip_seconds),
                        stop_requested=stop_requested,
                        progress=progress,
                    )
                    save_collector_state(state_path, state)
                    write_json_atomic(output_dir / "company_search_checkpoint.json", search_stats)
                else:
                    completed_company_ids = _completed_company_ids(db_path, window_start, window_end)
                    if completed_company_ids:
                        log(
                            f"Resume checkpoint: {len(completed_company_ids)}/{target_company_count} companies "
                            f"already durable for {window_start}..{window_end}"
                        )

                    def persist_company(company: Any, company_records: list[PatentRecord]) -> None:
                        completed_count = _stage_company_records(
                            db_path,
                            window_start,
                            window_end,
                            company.company_id,
                            company.company_name,
                            company_records,
                            company.search_names(int(args.company_search_name_limit)),
                        )
                        if completed_count % 25 == 0 or company_records:
                            log(
                                f"Company checkpoint: {completed_count}/{target_company_count} "
                                f"{company.company_name} records={len(company_records)}"
                            )

                    provider.search_companies(
                        companies,
                        window_start.isoformat(),
                        window_end.isoformat(),
                        max_per_company=int(args.max_per_company),
                        max_applicant_names=int(args.company_search_name_limit),
                        progress=lambda message: progress(message, 0.15),
                        checkpoint_path=output_dir / "company_search_checkpoint.json",
                        stop_requested=stop_requested,
                        completed_company_ids=completed_company_ids,
                        company_records_checkpoint=persist_company,
                    )
                    records = _load_staged_records(db_path, window_start, window_end)
                    search_stats = provider.last_company_search_stats
                repaired_observations = _restore_window_context(
                    db_path, records, window_start, window_end
                )
                if repaired_observations:
                    log(
                        f"Restored query window metadata for {repaired_observations} "
                        "raw EPO observations"
                    )
                _archive_search_quality(
                    db_path,
                    window_start,
                    window_end,
                    search_stats,
                    started_at,
                )
                search_stats["durable_company_count"] = len(
                    _completed_company_ids(db_path, window_start, window_end)
                )
                search_stats["durable_record_count"] = len(records)
                if stop_requested() or search_stats.get("stopped_by_request") or search_stats.get("stopped_by_throttle"):
                    reason = "stopped during EPO company search; next run will retry the same window"
                    if search_stats.get("stopped_by_throttle"):
                        collector_requests_per_minute = _adaptive_rate_after_throttle(collector_requests_per_minute)
                        state["collector_ops_requests_per_minute"] = collector_requests_per_minute
                        state["collector_rate_note"] = "EPO throttle/robot detection; decreased by 1 req/min"
                        save_collector_state(state_path, state)
                        progress(
                            f"EPOレート調整: スロットル検知のため次回 {collector_requests_per_minute} req/min に下げます",
                            0.15,
                        )
                        reason = "paused by EPO throttle/robot detection during company search; retry this window later"
                    _stop_window(
                        db_path,
                        state_path,
                        summary_path,
                        output_dir,
                        state,
                        window_start,
                        window_end,
                        started_at,
                        len(records),
                        area_kind,
                        days_before,
                        reason,
                        progress,
                        search_stats=search_stats,
                    )
                    break
                detail_summary = None
                if config.ops_enrich_details:
                    state["runtime"] = {
                        "phase": "detail_enrichment",
                        "status": "running",
                        "updated_at": _now(),
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                        "record_count": len(records),
                        "message": f"EPO detail enrichment for {len(records)} staged records",
                    }
                    save_collector_state(state_path, state)
                    detail_summary = provider.enrich_records(
                        records,
                        companies,
                        max_records=int(args.detail_max_records),
                        match_threshold=config.fuzzy_match_threshold,
                        match_margin=config.fuzzy_match_margin,
                        progress=lambda message: progress(message, 0.35),
                        stop_requested=stop_requested,
                        enrich_claims=config.ops_enrich_claims,
                        claims_top_rate=config.ops_claims_fulltext_top_rate,
                        enrich_family_legal=config.ops_enrich_family_legal,
                        enrich_forward_citations=config.ops_enrich_forward_citations,
                        forward_citation_max_records=config.ops_forward_citation_max_records,
                    )
                    if stop_requested() or detail_summary.get("stopped_by_request"):
                        _stop_window(
                            db_path,
                            state_path,
                            summary_path,
                            output_dir,
                            state,
                            window_start,
                            window_end,
                            started_at,
                            len(records),
                            area_kind,
                            days_before,
                            "stopped during EPO detail enrichment; next run will retry the same window",
                            progress,
                            search_stats=search_stats,
                            detail_summary=detail_summary,
                        )
                        break
                state["runtime"] = {
                    "phase": "scoring_finalizing",
                    "status": "running",
                    "updated_at": _now(),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "record_count": len(records),
                    "message": "Persisting normalized records and running AI/scoring",
                }
                save_collector_state(state_path, state)
                _persist_normalized_records(db_path, records, window_start, window_end)
                pipeline = PatentPipeline(
                    config,
                    companies,
                    output_dir / "patent_monitor_backfill_collector.sqlite3",
                    output_dir,
                    openai_api_key=secrets.get("openai_api_key", ""),
                    gemini_api_key=secrets.get("gemini_api_key", ""),
                    progress=lambda message, ratio: progress(message, 0.35 + ratio * 0.45),
                    stop_requested=stop_requested,
                )
                try:
                    result = pipeline.run(records, f"backfill_collector_{window_start}_{window_end}")
                finally:
                    pipeline.database.close()
                if result.get("ai_budget_stopped"):
                    log(
                        "AI_BUDGET_STOP "
                        f"annual_spend={float(result.get('annual_ai_spend_jpy') or 0):.4f} JPY "
                        f"budget={float(result.get('annual_ai_budget_jpy') or 0):.2f} JPY "
                        f"reason={result.get('ai_budget_stop_reason', '')}"
                    )
                else:
                    log(
                        "AI review completed: "
                        f"GPT={int(result.get('gpt_count') or 0)} "
                        f"Gemini={int(result.get('gemini_count') or 0)} "
                        f"annual_spend={float(result.get('annual_ai_spend_jpy') or 0):.4f} JPY"
                    )
                if _window_was_interrupted(result, stop_requested):
                    _stop_window(
                        db_path,
                        state_path,
                        summary_path,
                        output_dir,
                        state,
                        window_start,
                        window_end,
                        started_at,
                        len(records),
                        area_kind,
                        days_before,
                        f"stopped before scoring completed: scored_count={result.get('scored_count', 0)} "
                        f"family_count={result.get('family_count', 0)} run_id={result.get('run_id', '')}",
                        progress,
                        result=result,
                        search_stats=search_stats,
                        detail_summary=detail_summary,
                    )
                    break
                run_dir = Path(result["summary_json"]).parent
                diagnostic = analyze_run(
                    SimpleNamespace(
                        run_dir=str(run_dir),
                        previous_run_dir="",
                        output_root=str(output_dir),
                        company_master=args.company_master,
                        config=args.config,
                        output_dir=str(run_dir),
                        high_technology_threshold=float(args.high_technology_threshold),
                        near_miss_threshold=float(args.near_miss_threshold),
                        alias_patch_threshold=float(args.alias_patch_threshold),
                        alias_patch_gap=float(args.alias_patch_gap),
                    )
                )
                kept = save_minimal_patents(
                    db_path,
                    result["evaluation_csv"],
                    result["run_id"],
                    high_technology_threshold=float(args.high_technology_threshold),
                )
                for filename in ("suggested_alias_patches.csv", "unmatched_applicant_candidates.csv"):
                    _copy_if_exists(run_dir / filename, output_dir / filename)
                _append_retry_rows(retry_queue_path, window_start, window_end, search_stats)
                matched_count = int(diagnostic["run"]["matched_count"])
                high_unmatched = int(diagnostic["findings"]["high_technology_unmatched_count"])
                alias_count = int(diagnostic["findings"].get("suggested_alias_patch_count", 0))
                _record_window(
                    db_path,
                    window_start,
                    window_end,
                    "completed",
                    started_at,
                    _now(),
                    len(records),
                    kept,
                    matched_count,
                    high_unmatched,
                    alias_count,
                    search_stats=search_stats,
                    detail_stats=detail_summary,
                )
                state["last_window"] = {
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "status": "completed",
                    "run_id": result["run_id"],
                    "area_kind": area_kind,
                }
                state["next_end_date"] = (window_start - timedelta(days=1)).isoformat()
                state["total_windows_completed"] = int(state.get("total_windows_completed", 0)) + 1
                state["total_records_seen"] = int(state.get("total_records_seen", 0)) + len(records)
                state["total_records_kept"] = int(state.get("total_records_kept", 0)) + kept
                collector_requests_per_minute = _adaptive_rate_after_stable_window(
                    collector_requests_per_minute,
                    configured_requests_per_minute,
                )
                state["collector_ops_requests_per_minute"] = collector_requests_per_minute
                state["collector_rate_note"] = "window completed without throttle; increased by 1 req/min if below configured rate"
                completed_this_session += 1
                days_after = _session_days(completed_this_session, window_days)
                state["session_days_completed"] = days_after
                state["current_area_kind"] = ""
                save_collector_state(state_path, state)
                summary_state = dict(state)
                if int(args.max_windows) > 0 and completed_this_session >= int(args.max_windows):
                    summary_state["mode"] = "idle"
                summary = {
                    "created_at": _now(),
                    "collector_dir": str(output_dir.resolve()),
                    "state": summary_state,
                    "last_result": result,
                    "detail_enrichment": detail_summary,
                    "company_search": search_stats,
                    "diagnostics": diagnostic,
                    "minimal_saved_count": kept,
                    "collector_ops_requests_per_minute": state.get("collector_ops_requests_per_minute", ""),
                }
                write_json_atomic(summary_path, summary)
                log(
                    f"Window completed: {window_start}..{window_end} "
                    f"records={len(records)} kept={kept}; next_end={state['next_end_date']}"
                )
                state["runtime"] = {
                    "phase": "window_completed",
                    "status": "completed",
                    "updated_at": _now(),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "record_count": len(records),
                    "kept_records": kept,
                    "message": f"Window completed; next_end={state['next_end_date']}",
                }
                save_collector_state(state_path, state)
                _clear_window_staging(db_path, window_start, window_end)
                for staged in search_stats.get("staged_windows") or []:
                    try:
                        staged_start = date.fromisoformat(str(staged.get("window_start") or ""))
                        staged_end = date.fromisoformat(str(staged.get("window_end") or ""))
                    except ValueError:
                        continue
                    if (staged_start, staged_end) != (window_start, window_end):
                        _clear_window_staging(db_path, staged_start, staged_end)
                progress(
                    f"放置収集 完了: {window_start}〜{window_end} / {area_kind} / "
                    f"今回処理済み {days_after}日 / 次回EPO {collector_requests_per_minute} req/min",
                    1.0,
                )
                if int(args.max_windows) > 0 and completed_this_session >= int(args.max_windows):
                    state["mode"] = "idle"
                    save_collector_state(state_path, state)
                    break
            except Exception as exc:
                _record_window(db_path, window_start, window_end, "error", started_at, _now(), error=f"{type(exc).__name__}: {exc}")
                state["mode"] = "error"
                state["last_window"]["status"] = "error"
                state["runtime"] = {
                    **dict(state.get("runtime") or {}),
                    "status": "error",
                    "updated_at": _now(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                save_collector_state(state_path, state)
                raise
    finally:
        if state.get("mode") == "running":
            state["mode"] = "idle"
            save_collector_state(state_path, state)
    return {
        "collector_dir": str(output_dir.resolve()),
        "state_json": str(state_path.resolve()),
        "database": str(db_path.resolve()),
        "summary_json": str(summary_path.resolve()),
        "completed_windows": completed_this_session,
        "next_end_date": state.get("next_end_date", ""),
        "mode": state.get("mode", "idle"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuously collect older EPO patent windows until stopped.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--company-master", default=str(ROOT / "patent_company_master.csv"))
    parser.add_argument("--config", default=str(ROOT / "patent_monitor_config.json"))
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--adaptive-company-windows", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--low-volume-window-days", type=int, default=90)
    parser.add_argument("--split-window-days", type=int, default=7)
    parser.add_argument("--min-window-days", type=int, default=1)
    parser.add_argument("--low-volume-max-records", type=int, default=10)
    parser.add_argument("--max-per-company", type=int, default=50)
    parser.add_argument("--detail-max-records", type=int, default=0)
    parser.add_argument("--company-search-name-limit", type=int, default=12)
    parser.add_argument("--company-stall-skip-seconds", type=int, default=1800)
    parser.add_argument("--max-windows", type=int, default=0, help="0 means continue until stopped.")
    parser.add_argument("--high-technology-threshold", type=float, default=24.0)
    parser.add_argument("--near-miss-threshold", type=float, default=0.82)
    parser.add_argument("--alias-patch-threshold", type=float, default=0.90)
    parser.add_argument("--alias-patch-gap", type=float, default=0.20)
    parser.add_argument("--ranked-ai-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gpt-top-rate", type=float, default=0.12)
    parser.add_argument("--gpt-audit-rate", type=float, default=0.005)
    parser.add_argument("--gemini-top-rate", type=float, default=0.12)
    parser.add_argument("--annual-ai-budget-jpy", type=float, default=6000.0)
    parser.add_argument("--claims-fulltext-top-rate", type=float, default=0.02)
    return parser


def main() -> int:
    result = run_collector(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
