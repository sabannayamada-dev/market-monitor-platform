from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import PaperRecord, ScoredPaper


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    doi TEXT NOT NULL DEFAULT '',
    arxiv_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    published_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    journal TEXT NOT NULL DEFAULT '',
    categories_json TEXT NOT NULL DEFAULT '[]',
    landing_url TEXT NOT NULL DEFAULT '',
    pdf_url TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi) WHERE doi <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id <> '';
CREATE INDEX IF NOT EXISTS idx_papers_first_seen ON papers(first_seen_at DESC);

CREATE TABLE IF NOT EXISTS source_records (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(source, source_id),
    FOREIGN KEY(paper_id) REFERENCES papers(paper_id)
);

CREATE TABLE IF NOT EXISTS paper_scores (
    paper_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    profile_label TEXT NOT NULL,
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    FOREIGN KEY(paper_id) REFERENCES papers(paper_id)
);

CREATE TABLE IF NOT EXISTS ai_reviews (
    paper_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    relevant INTEGER NOT NULL DEFAULT 0,
    importance INTEGER NOT NULL DEFAULT 0,
    japanese_title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    novelty TEXT NOT NULL DEFAULT '',
    applications TEXT NOT NULL DEFAULT '',
    caution TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY(paper_id) REFERENCES papers(paper_id)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    scored INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    ai_sent INTEGER NOT NULL DEFAULT 0,
    ai_completed INTEGER NOT NULL DEFAULT 0,
    ai_failed INTEGER NOT NULL DEFAULT 0,
    emailed INTEGER NOT NULL DEFAULT 0,
    http_403 INTEGER NOT NULL DEFAULT 0,
    http_429 INTEGER NOT NULL DEFAULT 0,
    cooldown_skipped INTEGER NOT NULL DEFAULT 0,
    error_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, source)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_key TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    paper_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_items (
    notification_key TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    PRIMARY KEY(notification_key, paper_id)
);

CREATE TABLE IF NOT EXISTS service_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def normalize_doi(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text.removeprefix("doi:").strip()


def normalize_arxiv_id(value: str) -> str:
    text = str(value or "").strip().casefold().rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", text)


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def candidate_id(record: PaperRecord) -> str:
    doi = normalize_doi(record.doi)
    if doi:
        return "doi:" + doi
    arxiv_id = normalize_arxiv_id(record.arxiv_id)
    if arxiv_id:
        return "arxiv:" + arxiv_id
    first_author = normalize_title(record.authors[0] if record.authors else "")
    fingerprint = normalize_title(record.title) + "|" + first_author
    return "paper:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


class PaperDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_runs)")}
            for name in ("http_403", "http_429", "cooldown_skipped"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE collection_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def upsert_paper(self, record: PaperRecord, now: str) -> tuple[str, str]:
        doi = normalize_doi(record.doi)
        arxiv_id = normalize_arxiv_id(record.arxiv_id)
        normalized_title = normalize_title(record.title)
        proposed = candidate_id(record)
        with self.connection() as connection:
            existing_source = connection.execute(
                "SELECT paper_id FROM source_records WHERE source=? AND source_id=?",
                (record.source, record.source_id),
            ).fetchone()
            existing = existing_source
            if existing is None and doi:
                existing = connection.execute("SELECT paper_id FROM papers WHERE doi=?", (doi,)).fetchone()
            if existing is None and arxiv_id:
                existing = connection.execute("SELECT paper_id FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
            # A title/author fallback is only safe when neither durable identifier exists.
            # Different DOI/arXiv identifiers can legitimately have identical titles.
            if existing is None and not doi and not arxiv_id:
                first_author = normalize_title(record.authors[0] if record.authors else "")
                rows = connection.execute(
                    "SELECT paper_id,authors_json FROM papers WHERE normalized_title=?", (normalized_title,)
                ).fetchall()
                for row in rows:
                    authors = json.loads(row["authors_json"])
                    if first_author and authors and normalize_title(authors[0]) == first_author:
                        existing = row
                        break
            paper_id = str(existing["paper_id"]) if existing else proposed
            status = "inserted" if existing is None else "duplicate" if existing_source else "merged"
            if existing is None:
                connection.execute(
                    """INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        paper_id, doi, arxiv_id, record.title, normalized_title, record.abstract,
                        json.dumps(record.authors, ensure_ascii=False), record.published_at,
                        record.updated_at, record.journal,
                        json.dumps(record.categories, ensure_ascii=False), record.landing_url,
                        record.pdf_url, now, now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE papers SET doi=CASE WHEN doi='' THEN ? ELSE doi END,
                       arxiv_id=CASE WHEN arxiv_id='' THEN ? ELSE arxiv_id END,
                       abstract=CASE WHEN length(?)>length(abstract) THEN ? ELSE abstract END,
                       journal=CASE WHEN journal='' THEN ? ELSE journal END,
                       landing_url=CASE WHEN landing_url='' THEN ? ELSE landing_url END,
                       pdf_url=CASE WHEN pdf_url='' THEN ? ELSE pdf_url END,
                       updated_at=CASE WHEN ?>updated_at THEN ? ELSE updated_at END,last_seen_at=?
                       WHERE paper_id=?""",
                    (
                        doi, arxiv_id, record.abstract, record.abstract, record.journal,
                        record.landing_url, record.pdf_url, record.updated_at,
                        record.updated_at, now, paper_id,
                    ),
                )
            connection.execute(
                """INSERT INTO source_records VALUES(?,?,?,?,?,?)
                   ON CONFLICT(source,source_id) DO UPDATE SET
                   paper_id=excluded.paper_id,raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at""",
                (record.source, record.source_id, paper_id, json.dumps(record.raw, ensure_ascii=False), now, now),
            )
        return paper_id, status

    def paper(self, paper_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        if row is None:
            raise KeyError(paper_id)
        result = dict(row)
        result["authors"] = json.loads(result.pop("authors_json"))
        result["categories"] = json.loads(result.pop("categories_json"))
        return result

    def unscored_paper_ids(self) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT p.paper_id
                   FROM papers p
                   LEFT JOIN paper_scores s ON s.paper_id=p.paper_id
                   WHERE s.paper_id IS NULL
                   ORDER BY p.first_seen_at,p.paper_id"""
            ).fetchall()
        return [str(row["paper_id"]) for row in rows]

    def save_score(self, score: ScoredPaper, now: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO paper_scores VALUES(?,?,?,?,?,?)""",
                (score.paper_id, score.profile_id, score.profile_label, score.score, json.dumps(score.reasons, ensure_ascii=False), now),
            )

    def candidate_rows(self, paper_ids: list[str], threshold: float, limit: int) -> list[dict[str, Any]]:
        if not paper_ids:
            return []
        placeholders = ",".join("?" for _ in paper_ids)
        query = f"""SELECT p.*,s.profile_id,s.profile_label,s.score,s.reasons_json,
                    a.status ai_status,a.relevant,a.importance,a.japanese_title,a.summary,
                    a.novelty,a.applications,a.caution
                    FROM papers p JOIN paper_scores s ON s.paper_id=p.paper_id
                    LEFT JOIN ai_reviews a ON a.paper_id=p.paper_id
                    WHERE p.paper_id IN ({placeholders}) AND s.score>=?
                    ORDER BY s.score DESC,p.published_at DESC LIMIT ?"""
        with self.connection() as connection:
            rows = connection.execute(query, (*paper_ids, threshold, limit)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json"))
            item["categories"] = json.loads(item.pop("categories_json"))
            item["reasons"] = json.loads(item.pop("reasons_json"))
            result.append(item)
        return result

    def has_ai_review(self, paper_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1 FROM ai_reviews WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone() is not None

    def save_ai_review(self, paper_id: str, model: str, review: dict[str, Any], usage: dict[str, int], cost: float, now: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO ai_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id, "openai", model, "completed", int(bool(review["relevant"])),
                    int(review["importance"]), review["japanese_title"], review["summary"],
                    review["novelty"], review["applications"], review["caution"],
                    int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                    cost, json.dumps(review, ensure_ascii=False), now,
                ),
            )

    def ai_usage(self, since: str) -> dict[str, float]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) requests,COALESCE(SUM(estimated_cost_usd),0) cost FROM ai_reviews WHERE reviewed_at>=?",
                (since,),
            ).fetchone()
        return {"requests": int(row["requests"]), "cost": float(row["cost"])}

    def get_state(self, key: str) -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT state_value FROM service_state WHERE state_key=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def set_state(self, key: str, value: str, now: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO service_state VALUES(?,?,?)", (key, value, now)
            )

    def start_run(self, run_id: str, started: str, window_start: str, window_end: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id,started_at,status,window_start,window_end) VALUES(?,?,?,?,?)",
                (run_id, started, "running", window_start, window_end),
            )

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any], errors: list[str]) -> None:
        fields = ("fetched", "inserted", "updated", "duplicates", "scored", "candidates", "ai_sent", "ai_completed", "ai_failed", "emailed", "http_403", "http_429", "cooldown_skipped")
        with self.connection() as connection:
            connection.execute(
                f"UPDATE collection_runs SET completed_at=?,status=?,{','.join(f'{x}=?' for x in fields)},error_json=? WHERE run_id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), status, *(int(stats.get(x, 0)) for x in fields), json.dumps(errors, ensure_ascii=False), run_id),
            )

    def record_source_run(self, run_id: str, source: str, status: str, fetched: int = 0, error: Exception | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO source_runs VALUES(?,?,?,?,?,?)",
                (run_id, source, status, fetched, type(error).__name__ if error else "", str(error or "")),
            )

    def queue_notification(self, key: str, subject: str, body: str, paper_ids: list[str], now: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO notification_outbox(notification_key,subject,body,paper_ids_json,created_at) VALUES(?,?,?,?,?)",
                (key, subject, body, json.dumps(paper_ids), now),
            )

    def pending_notifications(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM notification_outbox WHERE status='pending' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def mark_notification_sent(self, key: str, paper_ids: list[str], now: str) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE notification_outbox SET status='sent',attempts=attempts+1,sent_at=?,last_error='' WHERE notification_key=?", (now, key))
            connection.executemany("INSERT OR IGNORE INTO notification_items VALUES(?,?)", [(key, paper_id) for paper_id in paper_ids])

    def mark_notification_failed(self, key: str, error: str) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE notification_outbox SET attempts=attempts+1,last_error=? WHERE notification_key=?", (error[:1000], key))
