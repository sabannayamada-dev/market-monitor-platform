from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class RunJournal:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_runs (
                    service_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    exit_code INTEGER,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(service_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_service_runs_latest
                ON service_runs(service_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS service_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_service_events_run
                ON service_events(service_id, run_id, event_id);
                """
            )

    def start(self, service_id: str, run_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO service_runs(service_id,run_id,started_at,status,phase) VALUES(?,?,?,?,?)",
                (service_id, run_id, datetime.now().isoformat(timespec="seconds"), "running", "startup"),
            )

    def recover_interrupted(self, service_id: str) -> int:
        """Close runs left open after a killed process once the service lock is held."""
        completed_at = datetime.now().isoformat(timespec="seconds")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT run_id,detail_json FROM service_runs WHERE service_id=? AND status='running'",
                (service_id,),
            ).fetchall()
            for run_id, detail_text in rows:
                try:
                    detail = json.loads(detail_text or "{}")
                except (TypeError, json.JSONDecodeError):
                    detail = {}
                detail["recovery"] = "次回起動時に前回の未完了記録を自動終了"
                connection.execute(
                    """UPDATE service_runs
                       SET completed_at=?,status='interrupted',phase='finished',exit_code=143,detail_json=?
                       WHERE service_id=? AND run_id=? AND status='running'""",
                    (completed_at, json.dumps(detail, ensure_ascii=False), service_id, run_id),
                )
        return len(rows)

    def phase(self, service_id: str, run_id: str, phase: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE service_runs SET phase=? WHERE service_id=? AND run_id=?",
                (phase, service_id, run_id),
            )

    def event(
        self,
        service_id: str,
        run_id: str,
        event_type: str,
        message: str,
        level: str = "info",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO service_events(
                   service_id,run_id,created_at,level,event_type,message,detail_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    service_id,
                    run_id,
                    datetime.now().isoformat(timespec="seconds"),
                    level,
                    event_type,
                    message[:4000],
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )

    def finish(
        self,
        service_id: str,
        run_id: str,
        status: str,
        exit_code: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE service_runs
                   SET completed_at=?,status=?,phase='finished',exit_code=?,detail_json=?
                   WHERE service_id=? AND run_id=?""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    status,
                    exit_code,
                    json.dumps(detail or {}, ensure_ascii=False),
                    service_id,
                    run_id,
                ),
            )

    def latest(self, service_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM service_runs WHERE service_id=? ORDER BY started_at DESC LIMIT 1",
                (service_id,),
            ).fetchone()
        return dict(row) if row else None

    def history(self, service_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM service_runs WHERE service_id=? ORDER BY started_at DESC LIMIT ?",
                (service_id, max(1, min(100, limit))),
            ).fetchall()
        return [dict(row) for row in rows]
