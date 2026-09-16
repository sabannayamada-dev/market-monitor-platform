from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    planned_queries INTEGER NOT NULL DEFAULT 0,
    executed_queries INTEGER NOT NULL DEFAULT 0,
    successful_queries INTEGER NOT NULL DEFAULT 0,
    empty_responses INTEGER NOT NULL DEFAULT 0,
    non_json_responses INTEGER NOT NULL DEFAULT 0,
    http_403 INTEGER NOT NULL DEFAULT 0,
    http_429 INTEGER NOT NULL DEFAULT 0,
    timeouts INTEGER NOT NULL DEFAULT 0,
    other_errors INTEGER NOT NULL DEFAULT 0,
    fetched_articles INTEGER NOT NULL DEFAULT 0,
    inserted_articles INTEGER NOT NULL DEFAULT 0,
    duplicate_articles INTEGER NOT NULL DEFAULT 0,
    company_matches INTEGER NOT NULL DEFAULT 0,
    scored_candidates INTEGER NOT NULL DEFAULT 0,
    gpt_sent INTEGER NOT NULL DEFAULT 0,
    gpt_passed INTEGER NOT NULL DEFAULT 0,
    gemini_sent INTEGER NOT NULL DEFAULT 0,
    final_candidates INTEGER NOT NULL DEFAULT 0,
    urgent_candidates INTEGER NOT NULL DEFAULT 0,
    carried_over INTEGER NOT NULL DEFAULT 0,
    error_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_collection_runs_started ON collection_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS query_runs (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_name TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, query_id)
);

CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    original_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    seen_date TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    source_country TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT '',
    tone REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_seen_date ON articles(seen_date DESC);

CREATE TABLE IF NOT EXISTS article_discoveries (
    article_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_name TEXT NOT NULL,
    profile TEXT NOT NULL,
    severity INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(article_id, query_id)
);

CREATE TABLE IF NOT EXISTS article_company_matches (
    article_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    ticker TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL,
    confidence REAL NOT NULL,
    matched_at TEXT NOT NULL,
    PRIMARY KEY(article_id, company_id)
);

CREATE TABLE IF NOT EXISTS article_scores (
    article_id TEXT PRIMARY KEY,
    score REAL NOT NULL,
    direction TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_reviews (
    article_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT 'unknown',
    summary TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY(article_id, provider)
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    article_ids_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS service_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ngram_files (
    stamp TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    matched_documents INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ngram_files_processed ON ngram_files(processed_at DESC);

CREATE TABLE IF NOT EXISTS ngram_toc_files (
    stamp TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    article_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ngram_toc_files_scanned ON ngram_toc_files(scanned_at DESC);

CREATE TABLE IF NOT EXISTS emergency_candidates (
    candidate_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_state TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    title_key TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT '',
    seen_date TEXT NOT NULL DEFAULT '',
    ngram_stamp TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_emergency_candidates_event
    ON emergency_candidates(event_key,event_state,discovered_at DESC);

CREATE TABLE IF NOT EXISTS emergency_alerts (
    alert_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_state TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    alerted_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_emergency_alerts_event
    ON emergency_alerts(event_key,event_state,alerted_at DESC);

CREATE TABLE IF NOT EXISTS emergency_ai_reviews (
    review_id TEXT PRIMARY KEY,
    evidence_key TEXT NOT NULL UNIQUE,
    event_key TEXT NOT NULL,
    event_state TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    event_confirmed INTEGER NOT NULL DEFAULT 0,
    same_event INTEGER NOT NULL DEFAULT 0,
    market_impact REAL NOT NULL DEFAULT 0,
    urgency REAL NOT NULL DEFAULT 0,
    is_speculation INTEGER NOT NULL DEFAULT 1,
    event_category TEXT NOT NULL DEFAULT '',
    affected_assets_json TEXT NOT NULL DEFAULT '[]',
    japanese_summary TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emergency_ai_reviews_at
    ON emergency_ai_reviews(reviewed_at DESC);

CREATE TABLE IF NOT EXISTS emergency_scout_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    head_requests INTEGER NOT NULL DEFAULT 0,
    discovered_files INTEGER NOT NULL DEFAULT 0,
    scanned_files INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    ready_alerts INTEGER NOT NULL DEFAULT 0,
    error_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_emergency_scout_runs_started
    ON emergency_scout_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS global_news_candidates (
    candidate_id TEXT PRIMARY KEY,
    title_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT '',
    seen_date TEXT NOT NULL DEFAULT '',
    rule_score REAL NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    ngram_stamp TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_news_candidates_at
    ON global_news_candidates(discovered_at DESC,rule_score DESC);

CREATE TABLE IF NOT EXISTS global_news_digests (
    digest_date TEXT PRIMARY KEY,
    selected_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adaptive_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_interval_minutes REAL,
    new_interval_minutes REAL NOT NULL,
    previous_cooldown_hours REAL,
    new_cooldown_hours REAL NOT NULL,
    previous_query_spacing_seconds REAL,
    new_query_spacing_seconds REAL NOT NULL,
    next_collection_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adaptive_events_at ON adaptive_events(event_at DESC);
"""


class NewsDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start_run(self, summary: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO collection_runs(
                   run_id,started_at,status,window_start,window_end,planned_queries
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    summary["run_id"], summary["started_at"], "running",
                    summary["window_start"], summary["window_end"], summary["planned_queries"],
                ),
            )

    def finish_run(self, summary: dict[str, Any]) -> None:
        fields = [
            "executed_queries", "successful_queries", "empty_responses",
            "non_json_responses", "http_403", "http_429", "timeouts",
            "other_errors", "fetched_articles", "inserted_articles",
            "duplicate_articles", "company_matches", "scored_candidates",
            "gpt_sent", "gpt_passed", "gemini_sent", "final_candidates",
            "urgent_candidates", "carried_over",
        ]
        assignments = ",".join(f"{field}=?" for field in fields)
        values = [summary.get(field, 0) for field in fields]
        with self.connection() as connection:
            connection.execute(
                f"""UPDATE collection_runs SET completed_at=?,status=?,{assignments},error_json=?
                    WHERE run_id=?""",
                [summary["completed_at"], summary["status"], *values,
                 json.dumps(summary.get("errors", []), ensure_ascii=False), summary["run_id"]],
            )

    def recent_runs(self, limit: int = 24) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def aggregate_runs(self, since: str) -> dict[str, int]:
        fields = [
            "planned_queries", "executed_queries", "successful_queries", "empty_responses",
            "non_json_responses", "http_403", "http_429", "timeouts", "other_errors",
            "fetched_articles", "inserted_articles", "duplicate_articles", "company_matches",
            "scored_candidates", "gpt_sent", "gpt_passed", "gemini_sent",
            "final_candidates", "urgent_candidates", "carried_over",
        ]
        expression = ",".join(f"COALESCE(SUM({field}),0) AS {field}" for field in fields)
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {expression}, COUNT(*) AS run_count FROM collection_runs WHERE started_at>=?",
                (since,),
            ).fetchone()
        return {key: int(row[key] or 0) for key in [*fields, "run_count"]}

    def get_state(self, key: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state_value FROM service_state WHERE state_key=?", (key,)
            ).fetchone()
        return str(row["state_value"]) if row else None

    def set_state(self, key: str, value: str, updated_at: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO service_state(state_key,state_value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                   state_value=excluded.state_value,updated_at=excluded.updated_at""",
                (key, value, updated_at),
            )

    def processed_ngram_stamps(self, stamps: list[str]) -> set[str]:
        if not stamps:
            return set()
        placeholders = ",".join("?" for _ in stamps)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT stamp FROM ngram_files WHERE stamp IN ({placeholders})", stamps
            ).fetchall()
        return {str(row["stamp"]) for row in rows}

    def processed_ngram_stamps_since(self, stamp: str) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT stamp FROM ngram_files WHERE stamp>=?", (stamp,)
            ).fetchall()
        return {str(row["stamp"]) for row in rows}

    def record_ngram_file(
        self,
        stamp: str,
        processed_at: str,
        matched_documents: int,
        downloaded_bytes: int,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO ngram_files(stamp,processed_at,matched_documents,downloaded_bytes)
                   VALUES(?,?,?,?) ON CONFLICT(stamp) DO UPDATE SET
                   processed_at=excluded.processed_at,
                   matched_documents=excluded.matched_documents,
                   downloaded_bytes=excluded.downloaded_bytes""",
                (stamp, processed_at, matched_documents, downloaded_bytes),
            )

    def scanned_toc_stamps_since(self, stamp: str) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT stamp FROM ngram_toc_files WHERE stamp>=?", (stamp,)
            ).fetchall()
        return {str(row["stamp"]) for row in rows}

    def record_toc_file(
        self,
        stamp: str,
        scanned_at: str,
        article_count: int,
        candidate_count: int,
        downloaded_bytes: int,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO ngram_toc_files(
                       stamp,scanned_at,article_count,candidate_count,downloaded_bytes
                   ) VALUES(?,?,?,?,?) ON CONFLICT(stamp) DO UPDATE SET
                   scanned_at=excluded.scanned_at,
                   article_count=excluded.article_count,
                   candidate_count=excluded.candidate_count,
                   downloaded_bytes=excluded.downloaded_bytes""",
                (stamp, scanned_at, article_count, candidate_count, downloaded_bytes),
            )

    def record_emergency_candidate(self, candidate: dict[str, Any]) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO emergency_candidates(
                       candidate_id,event_key,event_type,event_state,entity,title,title_key,url,
                       domain,source_language,seen_date,ngram_stamp,discovered_at,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate["candidate_id"], candidate["event_key"], candidate["event_type"],
                    candidate["event_state"], candidate.get("entity", ""), candidate["title"],
                    candidate["title_key"], candidate.get("url", ""), candidate.get("domain", ""),
                    candidate.get("source_language", ""), candidate.get("seen_date", ""),
                    candidate["ngram_stamp"], candidate["discovered_at"],
                    json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return cursor.rowcount > 0

    def emergency_candidates_since(self, since: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM emergency_candidates WHERE discovered_at>=?
                   ORDER BY discovered_at""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def emergency_alert_exists_since(
        self, event_key: str, event_state: str, since: str
    ) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM emergency_alerts
                   WHERE event_key=? AND event_state=? AND alerted_at>=? LIMIT 1""",
                (event_key, event_state, since),
            ).fetchone()
        return row is not None

    def record_emergency_alert(self, alert: dict[str, Any], alerted_at: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO emergency_alerts(
                       alert_id,event_key,event_type,event_state,entity,alerted_at,evidence_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    alert["alert_id"], alert["event_key"], alert["event_type"],
                    alert["event_state"], alert.get("entity", ""), alerted_at,
                    json.dumps(alert.get("evidence", []), ensure_ascii=False),
                ),
            )

    def emergency_ai_review(self, evidence_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM emergency_ai_reviews WHERE evidence_key=?", (evidence_key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def emergency_ai_usage_since(self, since: str) -> dict[str, float]:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS requests,
                          COALESCE(SUM(estimated_cost_usd),0) AS cost_usd
                   FROM emergency_ai_reviews
                   WHERE provider='openai' AND reviewed_at>=?""",
                (since,),
            ).fetchone()
        return {"requests": int(row["requests"]), "cost_usd": float(row["cost_usd"])}

    def record_emergency_ai_review(self, review: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO emergency_ai_reviews(
                       review_id,evidence_key,event_key,event_state,provider,model,status,
                       event_confirmed,same_event,market_impact,urgency,is_speculation,
                       event_category,affected_assets_json,japanese_summary,reason,
                       input_tokens,output_tokens,estimated_cost_usd,raw_json,reviewed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review["review_id"], review["evidence_key"], review["event_key"],
                    review["event_state"], review.get("provider", "openai"), review["model"],
                    review["status"], int(bool(review.get("event_confirmed"))),
                    int(bool(review.get("same_event"))), float(review.get("market_impact", 0)),
                    float(review.get("urgency", 0)), int(bool(review.get("is_speculation", True))),
                    str(review.get("event_category", "")),
                    json.dumps(review.get("affected_assets", []), ensure_ascii=False),
                    str(review.get("japanese_summary", "")), str(review.get("reason", "")),
                    int(review.get("input_tokens", 0)), int(review.get("output_tokens", 0)),
                    float(review.get("estimated_cost_usd", 0)),
                    json.dumps(review.get("raw", {}), ensure_ascii=False), review["reviewed_at"],
                ),
            )

    def record_emergency_scout_run(self, run: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO emergency_scout_runs(
                       run_id,started_at,completed_at,status,head_requests,discovered_files,
                       scanned_files,downloaded_bytes,candidates_found,ready_alerts,error_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"], run["started_at"], run["completed_at"], run["status"],
                    run.get("head_requests", 0), run.get("discovered_files", 0),
                    run.get("scanned_files", 0), run.get("downloaded_bytes", 0),
                    run.get("candidates_found", 0), run.get("ready_alerts", 0),
                    json.dumps(run.get("errors", []), ensure_ascii=False),
                ),
            )

    def record_global_news_candidates(self, candidates: list[dict[str, Any]]) -> int:
        inserted = 0
        with self.connection() as connection:
            for item in candidates:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO global_news_candidates(
                       candidate_id,title_key,title,url,domain,source_language,seen_date,
                       rule_score,topic,ngram_stamp,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["candidate_id"], item["title_key"], item["title"], item.get("url", ""),
                        item.get("domain", ""), item.get("source_language", ""), item.get("seen_date", ""),
                        float(item.get("rule_score", 0)), item.get("topic", ""), item["ngram_stamp"],
                        item["discovered_at"],
                    ),
                )
                inserted += max(0, cursor.rowcount)
        return inserted

    def global_news_candidates_since(self, since: str, limit: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM global_news_candidates WHERE discovered_at>=?
                   ORDER BY rule_score DESC,discovered_at DESC""",
                (since,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        for row in rows:
            item = dict(row)
            if item["title_key"] in seen_titles:
                continue
            seen_titles.add(item["title_key"])
            output.append(item)
            if len(output) >= limit:
                break
        return output

    def global_news_digest(self, digest_date: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM global_news_digests WHERE digest_date=?", (digest_date,)
            ).fetchone()
        return dict(row) if row is not None else None

    def record_global_news_digest(
        self, digest_date: str, selected: list[dict[str, Any]], model: str, status: str
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO global_news_digests(
                   digest_date,selected_json,model,status,reviewed_at) VALUES(?,?,?,?,?)""",
                (
                    digest_date, json.dumps(selected, ensure_ascii=False), model, status,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    def delete_global_news_before(self, before: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM global_news_candidates WHERE discovered_at<?", (before,))

    def record_adaptive_event(
        self, event_at: str, event_type: str,
        previous_interval_minutes: float | None, new_interval_minutes: float,
        previous_cooldown_hours: float | None, new_cooldown_hours: float,
        previous_query_spacing_seconds: float | None, new_query_spacing_seconds: float,
        next_collection_at: str, reason: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO adaptive_events(
                   event_at,event_type,previous_interval_minutes,new_interval_minutes,
                   previous_cooldown_hours,new_cooldown_hours,previous_query_spacing_seconds,
                   new_query_spacing_seconds,next_collection_at,reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_at, event_type, previous_interval_minutes, new_interval_minutes,
                    previous_cooldown_hours, new_cooldown_hours, previous_query_spacing_seconds,
                    new_query_spacing_seconds, next_collection_at, reason,
                ),
            )

    def adaptive_events_since(self, since: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM adaptive_events
                   WHERE event_at>=? AND event_type<>'initialized'
                   ORDER BY event_at""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_adaptive_rate_signal(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM adaptive_events
                   WHERE event_type LIKE '%429%' OR reason LIKE '%429%'
                   LIMIT 1"""
            ).fetchone()
        return row is not None

    def recent_errors_since(self, since: str, limit: int = 10) -> list[str]:
        messages: list[str] = []
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT error_json FROM collection_runs
                   WHERE started_at>=? AND error_json<>'[]'
                   ORDER BY started_at DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        for row in rows:
            try:
                errors = json.loads(row["error_json"] or "[]")
            except json.JSONDecodeError:
                errors = [{"message": row["error_json"]}]
            for error in errors:
                message = f"{error.get('type', 'error')}: {error.get('query', '')} {error.get('message', '')}".strip()
                if message and message not in messages:
                    messages.append(message[:500])
        return messages[:limit]
