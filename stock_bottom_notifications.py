from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from monitor_core.mail import SMTPMailer, SMTPSettings

SMTPConfig = SMTPSettings


def ensure_schema(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS monitor_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bottom_signals (
            signal_key TEXT PRIMARY KEY,
            algorithm_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            stable_date TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            run_id TEXT NOT NULL,
            delivery_status TEXT NOT NULL,
            sent_at TEXT,
            payload_json TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_bottom_signals_delivery
        ON bottom_signals(delivery_status, stable_date, symbol);

        CREATE TABLE IF NOT EXISTS monitor_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            algorithm_version TEXT NOT NULL,
            target_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            detected_count INTEGER NOT NULL DEFAULT 0,
            new_signal_count INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    connection.commit()
    connection.close()


def signal_key(algorithm_version: str, symbol: str, stable_date: str) -> str:
    return f"{algorithm_version}|{symbol.upper()}|{stable_date or 'unknown'}"


def baseline_exists(database_path: str | Path, algorithm_version: str) -> bool:
    ensure_schema(database_path)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT 1 FROM monitor_state WHERE state_key=?",
        (f"baseline:{algorithm_version}",),
    ).fetchone()
    connection.close()
    return row is not None


def initialize_baseline(
    database_path: str | Path,
    algorithm_version: str,
    rows: list[dict[str, Any]],
) -> int:
    ensure_schema(database_path)
    now = datetime.now().isoformat(timespec="seconds")
    connection = sqlite3.connect(database_path)
    marker = f"baseline:{algorithm_version}"
    if connection.execute(
        "SELECT 1 FROM monitor_state WHERE state_key=?", (marker,)
    ).fetchone():
        connection.close()
        return 0
    inserted = 0
    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        stable_date = str(row.get("stable_date", "") or "unknown")
        if not symbol:
            continue
        cursor = connection.execute(
            """INSERT OR IGNORE INTO bottom_signals(
               signal_key,algorithm_version,symbol,stable_date,first_seen_at,last_seen_at,
               run_id,delivery_status,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                signal_key(algorithm_version, symbol, stable_date),
                algorithm_version,
                symbol,
                stable_date,
                now,
                now,
                "baseline",
                "baseline",
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        inserted += max(0, cursor.rowcount)
    connection.execute(
        "INSERT INTO monitor_state(state_key,state_value,updated_at) VALUES(?,?,?)",
        (marker, json.dumps({"signal_count": inserted}), now),
    )
    connection.commit()
    connection.close()
    return inserted


def record_signal(
    database_path: str | Path,
    algorithm_version: str,
    run_id: str,
    row: dict[str, Any],
    initial_status: str = "pending",
) -> bool:
    ensure_schema(database_path)
    symbol = str(row.get("symbol", "")).strip().upper()
    stable_date = str(row.get("stable_date", "") or "unknown")
    if not symbol:
        return False
    now = datetime.now().isoformat(timespec="seconds")
    key = signal_key(algorithm_version, symbol, stable_date)
    payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    connection = sqlite3.connect(database_path)
    cursor = connection.execute(
        """INSERT OR IGNORE INTO bottom_signals(
           signal_key,algorithm_version,symbol,stable_date,first_seen_at,last_seen_at,
           run_id,delivery_status,payload_json
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            key, algorithm_version, symbol, stable_date, now, now,
            run_id, initial_status, payload,
        ),
    )
    inserted = cursor.rowcount > 0
    if not inserted:
        connection.execute(
            """UPDATE bottom_signals
               SET last_seen_at=?, run_id=?, payload_json=?
               WHERE signal_key=?""",
            (now, run_id, payload, key),
        )
    connection.commit()
    connection.close()
    return inserted and initial_status == "pending"


def pending_signals(database_path: str | Path) -> list[dict[str, Any]]:
    ensure_schema(database_path)
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        """SELECT signal_key,payload_json FROM bottom_signals
           WHERE delivery_status='pending'
           ORDER BY stable_date DESC, symbol"""
    ).fetchall()
    connection.close()
    result: list[dict[str, Any]] = []
    for key, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_signal_key"] = key
            result.append(payload)
    return result


def mark_sent(database_path: str | Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    now = datetime.now().isoformat(timespec="seconds")
    connection = sqlite3.connect(database_path)
    connection.executemany(
        """UPDATE bottom_signals
           SET delivery_status='sent', sent_at=?, last_error=''
           WHERE signal_key=?""",
        [(now, row["_signal_key"]) for row in rows],
    )
    connection.commit()
    connection.close()


def mark_delivery_error(
    database_path: str | Path, rows: list[dict[str, Any]], error: str,
) -> None:
    if not rows:
        return
    connection = sqlite3.connect(database_path)
    connection.executemany(
        "UPDATE bottom_signals SET last_error=? WHERE signal_key=?",
        [(error[:1500], row["_signal_key"]) for row in rows],
    )
    connection.commit()
    connection.close()


def start_run(
    database_path: str | Path, run_id: str, algorithm_version: str, target_count: int,
) -> None:
    ensure_schema(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        """INSERT INTO monitor_runs(
           run_id,started_at,algorithm_version,target_count,status
           ) VALUES(?,?,?,?,?)""",
        (
            run_id,
            datetime.now().isoformat(timespec="seconds"),
            algorithm_version,
            target_count,
            "running",
        ),
    )
    connection.commit()
    connection.close()


def finish_run(database_path: str | Path, run_id: str, **values: Any) -> None:
    allowed = {
        "completed_count", "updated_count", "detected_count", "new_signal_count",
        "sent_count", "error_count", "status", "detail_json",
    }
    assignments = ["completed_at=?"]
    parameters: list[Any] = [datetime.now().isoformat(timespec="seconds")]
    for key, value in values.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            parameters.append(value)
    parameters.append(run_id)
    connection = sqlite3.connect(database_path)
    connection.execute(
        f"UPDATE monitor_runs SET {', '.join(assignments)} WHERE run_id=?",
        parameters,
    )
    connection.commit()
    connection.close()


def _number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "不明"


def send_digest(config: SMTPConfig, rows: list[dict[str, Any]], run_id: str) -> None:
    if not rows:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    message = EmailMessage()
    message["Subject"] = f"【底検知】新規{len(rows)}社 {today}"
    message["From"] = config.user
    message["To"] = config.recipient
    lines = [
        f"独自アルゴリズムで新たに底判定となった企業は{len(rows)}社です。",
        f"実行ID: {run_id}",
        "",
    ]
    for index, row in enumerate(rows, 1):
        currency = str(row.get("currency", ""))
        lines.extend(
            [
                f"{index}. {row.get('name') or row.get('symbol')} ({row.get('symbol', '')})",
                f"   底判定日: {row.get('stable_date') or '不明'}",
                f"   判定スコア: {_number(row.get('score_at_stable'))}",
                f"   現在値: {_number(row.get('current_price'), 2)} {currency}",
                f"   直前高値からの下落率: {_number(row.get('drawdown_from_peak_percent'))}%",
                f"   判定: {row.get('bottom_verdict') or row.get('reason') or '底打ち候補'}",
                f"   {row.get('source_url', '')}",
                "",
            ]
        )
    lines.extend(
        [
            "この通知は独自アルゴリズムによる機械判定であり、投資助言ではありません。",
            "売買前に最新の株価、開示情報、流動性、業績を別途確認してください。",
        ]
    )
    message.set_content("\n".join(lines))
    SMTPMailer(config).send_message(message)


def send_failure(config: SMTPConfig, error: str, run_id: str) -> None:
    if not config.configured:
        return
    message = EmailMessage()
    message["Subject"] = "【エラー】底検知モニターの日次処理に失敗"
    message["From"] = config.user
    message["To"] = config.recipient
    message.set_content(f"実行ID: {run_id}\n\n{error[:5000]}")
    SMTPMailer(config).send_message(message)
