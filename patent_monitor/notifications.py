from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from monitor_core.mail import SMTPMailer, SMTPSettings
from monitor_core.deepl_translation import translate_to_japanese

DIGEST_CHANNEL = "email_digest"
URGENT_CHANNEL = "email_urgent"
LEGACY_CHANNEL = "email"


def ensure_notification_schema(database_path: str | Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
           patent_id TEXT, channel TEXT, sent_at TEXT, run_id TEXT, recipient TEXT,
           PRIMARY KEY(patent_id, channel)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notification_candidates (
           patent_id TEXT PRIMARY KEY,
           first_seen_at TEXT NOT NULL,
           run_id TEXT NOT NULL,
           decision TEXT NOT NULL,
           final_score REAL NOT NULL DEFAULT 0,
           importance_score REAL NOT NULL DEFAULT 0,
           short_term_score REAL NOT NULL DEFAULT 0,
           materiality_score REAL NOT NULL DEFAULT 0,
           company_percentile REAL NOT NULL DEFAULT 0
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notification_batches (
           batch_key TEXT, channel TEXT, sent_at TEXT, run_id TEXT, recipient TEXT,
           PRIMARY KEY(batch_key, channel)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notification_outbox (
           patent_id TEXT PRIMARY KEY,
           queued_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           run_id TEXT NOT NULL,
           decision TEXT NOT NULL,
           payload_json TEXT NOT NULL
        )"""
    )
    connection.commit()
    connection.close()


def _patent_id(row: dict[str, Any]) -> str:
    return "".join(ch for ch in str(row.get("publication_number", "")).upper() if ch.isalnum())


def _number(row: dict[str, Any], name: str) -> float:
    try:
        return float(row.get(name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _decision(row: dict[str, Any]) -> str:
    return str(row.get("gemini_decision") or row.get("gpt_decision") or "").strip().lower()


def register_digest_candidate(database_path: str | Path, row: dict[str, Any], run_id: str) -> bool:
    """Record the denominator used to enforce the lifetime urgent-mail share."""
    explicit_decision = str(row.get("_decision") or "").strip().lower()
    decision = "technology" if explicit_decision == "technology" else _decision(row)
    patent_id = row.get("_patent_id") or _patent_id(row)
    if not patent_id or decision not in {"important", "urgent", "technology"}:
        return False
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    inserted = False
    if decision in {"important", "urgent"}:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO notification_candidates(
               patent_id,first_seen_at,run_id,decision,final_score,importance_score,
               short_term_score,materiality_score,company_percentile
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                patent_id, datetime.now().isoformat(timespec="seconds"), run_id, decision,
                _number(row, "final_score"), _number(row, "gemini_importance"),
                _number(row, "gemini_short_term_market_impact"),
                _number(row, "gemini_materiality"), _number(row, "company_percentile"),
            ),
        )
        inserted = cursor.rowcount > 0
    elif connection.execute(
        "SELECT 1 FROM notification_outbox WHERE patent_id=?", (patent_id,)
    ).fetchone() is None:
        inserted = True
    payload = {
        key: value for key, value in row.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    payload["_patent_id"] = patent_id
    payload["_decision"] = decision
    now = datetime.now().isoformat(timespec="seconds")
    connection.execute(
        """INSERT INTO notification_outbox(
           patent_id,queued_at,updated_at,run_id,decision,payload_json
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(patent_id) DO UPDATE SET
             updated_at=excluded.updated_at,
             run_id=excluded.run_id,
             decision=excluded.decision,
             payload_json=excluded.payload_json""",
        (patent_id, now, now, run_id, decision, json.dumps(payload, ensure_ascii=False)),
    )
    connection.commit()
    connection.close()
    return inserted


def pending_digest_rows(database_path: str | Path) -> list[dict[str, Any]]:
    """Return every queued important patent that has not reached a digest yet."""
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        """SELECT o.payload_json
           FROM notification_outbox o
           LEFT JOIN notifications n
             ON n.patent_id=o.patent_id AND n.channel IN (?,?)
           WHERE n.patent_id IS NULL
           ORDER BY o.queued_at, o.patent_id""",
        (DIGEST_CHANNEL, LEGACY_CHANNEL),
    ).fetchall()
    connection.close()
    result: list[dict[str, Any]] = []
    for (payload_json,) in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("_patent_id") and payload.get("_decision") in {"important", "urgent", "technology"}:
            result.append(payload)
    return result


def digest_rows(evaluation_csv: str | Path, database_path: str | Path, run_id: str) -> list[dict[str, str]]:
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    already = {
        row[0] for row in connection.execute(
            "SELECT patent_id FROM notifications WHERE channel IN (?,?)",
            (DIGEST_CHANNEL, LEGACY_CHANNEL),
        )
    }
    connection.close()
    with Path(evaluation_csv).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: list[dict[str, str]] = []
    for row in rows:
        patent_id = _patent_id(row)
        decision = _decision(row)
        if patent_id and decision in {"important", "urgent"}:
            row["_patent_id"] = patent_id
            row["_decision"] = decision
            register_digest_candidate(database_path, row, run_id)
            if patent_id not in already:
                result.append(row)
    return result


def important_rows(evaluation_csv: str | Path, database_path: str | Path) -> list[dict[str, str]]:
    """Backward-compatible alias used by older local integrations."""
    return digest_rows(evaluation_csv, database_path, "legacy")


def scored_item_to_email_row(item: Any) -> dict[str, Any]:
    gpt = item.gpt_result or {}
    gemini = item.gemini_result or {}
    row: dict[str, Any] = {
        "publication_number": item.patent.publication_number,
        "publication_date": item.patent.publication_date,
        "company_name": item.match.company_name,
        "applicants": " | ".join(item.patent.applicants),
        "title": item.patent.title,
        "final_score": item.final_score,
        "company_percentile": item.company_percentile,
        "gpt_decision": gpt.get("decision", ""),
        "gpt_summary": gpt.get("email_summary", ""),
        "gemini_decision": gemini.get("decision", ""),
        "gemini_importance": gemini.get("importance_score", ""),
        "gemini_short_term_market_impact": gemini.get("short_term_market_impact_score", ""),
        "gemini_materiality": gemini.get("materiality_score", ""),
        "gemini_summary": gemini.get("email_summary", ""),
        "learned_upside_probability": (
            item.learned_upside_probability if item.learned_model_samples else ""
        ),
        "source_url": item.patent.source_url,
        "technology_categories": " | ".join(
            item.patent.raw.get("technology_categories", [])
        ),
    }
    row["_patent_id"] = item.patent.identity
    row["_decision"] = _decision(row)
    return row


def qualifies_as_urgent(row: dict[str, Any], config: Any) -> tuple[bool, str]:
    # GPT alone cannot trigger an interrupting notification. Gemini must independently
    # return urgent and every materiality dimension must clear a deliberately high bar.
    if str(row.get("gemini_decision", "")).strip().lower() != "urgent":
        return False, "Gemini判定がurgentではない"
    checks = (
        ("重要度", _number(row, "gemini_importance"), float(config.urgent_min_importance)),
        ("短期材料性", _number(row, "gemini_short_term_market_impact"), float(config.urgent_min_short_term_market_impact)),
        ("材料性", _number(row, "gemini_materiality"), float(config.urgent_min_materiality)),
        ("企業内順位", _number(row, "company_percentile"), float(config.urgent_min_company_percentile)),
    )
    failed = [f"{name}={value:g}<{minimum:g}" for name, value, minimum in checks if value < minimum]
    if failed:
        return False, ", ".join(failed)
    minimum_probability = float(getattr(config, "urgent_min_learned_upside_probability", 0.0))
    probability = _number(row, "learned_upside_probability")
    if minimum_probability > 0 and probability < minimum_probability:
        return False, f"学習上昇確率={probability:.3f}<{minimum_probability:.3f}"
    return True, "厳格緊急基準を通過"


def urgent_budget_available(database_path: str | Path, max_share: float) -> tuple[bool, dict[str, int | float]]:
    ensure_notification_schema(database_path)
    share = max(0.0, min(0.01, float(max_share)))
    connection = sqlite3.connect(database_path)
    candidate_count = int(connection.execute("SELECT COUNT(*) FROM notification_candidates").fetchone()[0])
    urgent_count = int(connection.execute(
        "SELECT COUNT(*) FROM notifications WHERE channel=?", (URGENT_CHANNEL,)
    ).fetchone()[0])
    connection.close()
    allowed = math.floor(candidate_count * share + 1e-12)
    return urgent_count < allowed, {
        "candidate_count": candidate_count,
        "urgent_count": urgent_count,
        "allowed_urgent_count": allowed,
        "max_share": share,
    }


def was_notified(database_path: str | Path, patent_id: str, channel: str) -> bool:
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT 1 FROM notifications WHERE patent_id=? AND channel=?",
        (patent_id, channel),
    ).fetchone()
    connection.close()
    return row is not None


def daily_digest_was_sent(database_path: str | Path, digest_date: str) -> bool:
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT 1 FROM notification_batches WHERE batch_key=? AND channel=?",
        (digest_date, DIGEST_CHANNEL),
    ).fetchone()
    connection.close()
    return row is not None


def mark_daily_digest_sent(
    database_path: str | Path, digest_date: str, run_id: str, recipient: str,
) -> None:
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT OR IGNORE INTO notification_batches VALUES(?,?,?,?,?)",
        (digest_date, DIGEST_CHANNEL, datetime.now().isoformat(timespec="seconds"), run_id, recipient),
    )
    connection.commit()
    connection.close()


def send_patent_email(
    rows: list[dict[str, Any]], smtp_host: str, smtp_port: int, smtp_user: str,
    smtp_password: str, recipient: str, run_id: str, kind: str = "digest",
) -> None:
    if kind == "urgent" and not rows:
        return
    message = EmailMessage()
    source_texts = [str(row.get("title") or "") for row in rows]
    source_texts.extend(str(row.get("gemini_summary") or row.get("gpt_summary") or "") for row in rows)
    translations = translate_to_japanese(source_texts)
    if kind == "urgent":
        message["Subject"] = f"【至急・最重要特許】{rows[0].get('company_name', '')} - 特許材料性モニター"
        lines = ["最重要基準を通過した特許を検知しました。", ""]
    else:
        technology_count = sum(row.get("_decision") == "technology" for row in rows)
        important_count = len(rows) - technology_count
        message["Subject"] = (
            f"【日次】重要特許{important_count}件・注目技術特許{technology_count}件 "
            "- 特許材料性モニター"
        )
        lines = [
            f"本日の重要特許は{important_count}件、注目技術の新着特許は"
            f"{technology_count}件です。",
            "",
        ]
        if not rows:
            lines.append("本日は日次ダイジェストの掲載対象となる特許がありませんでした。")
    message["From"] = smtp_user
    message["To"] = recipient

    def append_rows(section_rows: list[dict[str, Any]], technology_section: bool) -> None:
        if not section_rows:
            lines.extend(["該当なし", ""])
            return
        for index, row in enumerate(section_rows, 1):
            probability = row.get("learned_upside_probability", "")
            probability_text = (
                f" / 60日超過上昇確率={float(probability) * 100:.1f}%"
                if probability not in {"", None} else ""
            )
            original_title = str(row.get("title") or "")
            translated_title = translations.get(original_title, original_title)
            original_summary = str(row.get("gemini_summary") or row.get("gpt_summary") or "")
            translated_summary = translations.get(original_summary, original_summary)
            display_name = row.get("company_name") or row.get("applicants") or "出願人不明"
            technology = str(row.get("technology_categories") or "")
            label = "技術新着" if technology_section else str(row.get("_decision", "")).upper()
            lines.extend([
                f"{index}. [{label}] {display_name}",
                *([f"   技術分野: {technology}"] if technology else []),
                f"   {translated_title} ({row.get('publication_number', '')})",
                *([f"   原題: {original_title}"] if translated_title != original_title else []),
            ])
            if technology_section:
                lines.append(f"   アルゴリズム評価={row.get('final_score', '')}")
            else:
                lines.append(
                    f"   Gemini重要度={row.get('gemini_importance', '')} "
                    f"短期材料性={row.get('gemini_short_term_market_impact', '')}{probability_text}"
                )
            if translated_summary:
                lines.append(f"   {translated_summary}")
            if translated_summary != original_summary and original_summary:
                lines.append(f"   原文要約: {original_summary}")
            lines.extend([f"   {row.get('source_url', '')}", ""])

    if kind == "urgent":
        append_rows(rows, False)
    else:
        company_rows = [row for row in rows if row.get("_decision") != "technology"]
        technology_rows = [row for row in rows if row.get("_decision") == "technology"]
        lines.extend(["【1. 企業監視で検出した重要特許】", ""])
        append_rows(company_rows, False)
        lines.extend(["【2. 重点技術から検出した新着特許】", ""])
        append_rows(technology_rows, True)
    message.set_content("\n".join(lines))
    SMTPMailer(
        SMTPSettings(smtp_host, smtp_port, smtp_user, smtp_password, recipient)
    ).send_message(message)


def mark_notified(
    database_path: str | Path, rows: list[dict[str, Any]], run_id: str,
    recipient: str, channel: str = DIGEST_CHANNEL,
) -> None:
    if not rows:
        return
    ensure_notification_schema(database_path)
    connection = sqlite3.connect(database_path)
    connection.executemany(
        "INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?)",
        [
            (row["_patent_id"], channel, datetime.now().isoformat(timespec="seconds"), run_id, recipient)
            for row in rows
        ],
    )
    connection.commit()
    connection.close()


def test_smtp(host: str, port: int, user: str, password: str, recipient: str) -> None:
    message = EmailMessage()
    message["Subject"] = "特許材料性モニター 接続確認"
    message["From"] = user
    message["To"] = recipient
    message.set_content("メール通知の接続確認に成功しました。")
    SMTPMailer(SMTPSettings(host, port, user, password, recipient)).send_message(message)


def send_status_email(host: str, port: int, user: str, password: str, recipient: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.set_content(body)
    SMTPMailer(SMTPSettings(host, port, user, password, recipient)).send_message(message)
