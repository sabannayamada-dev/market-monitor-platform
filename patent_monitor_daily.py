from __future__ import annotations

import json
import os
import csv
import sqlite3
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from monitor_core.locking import acquire_lock_file
from patent_monitor.market_feedback import MarketFeedbackService
from patent_monitor.notifications import (
    DIGEST_CHANNEL,
    URGENT_CHANNEL,
    daily_digest_was_sent,
    digest_rows,
    mark_daily_digest_sent,
    mark_notified,
    pending_digest_rows,
    qualifies_as_urgent,
    register_digest_candidate,
    scored_item_to_email_row,
    send_patent_email,
    urgent_budget_available,
    was_notified,
)
from patent_monitor.pipeline import EPOOPSProvider, PatentPipeline, PipelineConfig, read_companies
from patent_monitor.secure_store import load_credentials
from patent_monitor.supabase_state import SupabaseStateConfig, SupabaseStateStore


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / ".patent_monitor_gui_state.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "patent_monitor"
LOCK_PATH = DEFAULT_OUTPUT / ".daily_run.lock"


def log(path: Path, message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_value(name: str, fallback: Any) -> Any:
    value = os.getenv(name)
    return value if value is not None and value.strip() else fallback


def company_search_blocks_postprocess(search_stats: dict[str, Any]) -> bool:
    """Only an explicit stop or API throttle blocks the reserved postprocess phase."""
    return bool(
        search_stats.get("stopped_by_request")
        or search_stats.get("stopped_by_throttle")
    )


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_runtime_state() -> dict[str, Any]:
    # A committed GUI state contains Windows absolute paths, so Actions intentionally ignores it.
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true" or not STATE_PATH.exists():
        return {}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_runtime_overrides(config: PipelineConfig, state: dict[str, Any]) -> None:
    config.gemini_threshold = float(env_value("PATENT_PRIMARY_THRESHOLD", state.get("threshold", config.gemini_threshold)))
    config.company_percentile_threshold = float(
        env_value("PATENT_COMPANY_PERCENTILE", state.get("percentile", config.company_percentile_threshold))
    )
    config.random_reject_audit_rate = float(
        env_value("PATENT_REJECT_AUDIT_RATE", state.get("audit_rate", config.random_reject_audit_rate * 100))
    ) / 100
    config.gemini_escalation_threshold = float(
        env_value("PATENT_GEMINI_THRESHOLD", state.get("escalation_threshold", config.gemini_escalation_threshold))
    )
    config.adaptive_gemini_escalation = env_bool(
        "PATENT_ADAPTIVE_ESCALATION", bool(state.get("adaptive_escalation", config.adaptive_gemini_escalation))
    )
    config.gemini_target_escalation_rate = float(
        env_value("PATENT_GEMINI_TARGET_RATE", state.get("target_escalation_rate", config.gemini_target_escalation_rate * 100))
    ) / 100
    config.monthly_gpt_limit = int(env_value("PATENT_MONTHLY_GPT_LIMIT", state.get("monthly_gpt_limit", config.monthly_gpt_limit)))
    config.daily_gpt_limit = int(env_value("PATENT_DAILY_GPT_LIMIT", config.daily_gpt_limit))
    config.monthly_gemini_limit = int(
        env_value("PATENT_MONTHLY_GEMINI_LIMIT", state.get("monthly_limit", config.monthly_gemini_limit))
    )
    config.daily_gemini_limit = int(env_value("PATENT_DAILY_GEMINI_LIMIT", config.daily_gemini_limit))
    config.gpt_model = str(env_value("PATENT_GPT_MODEL", state.get("gpt_model", config.gpt_model)))
    config.gemini_model = str(env_value("PATENT_GEMINI_MODEL", state.get("model", config.gemini_model)))
    config.daily_digest_enabled = env_bool("PATENT_DAILY_DIGEST_ENABLED", config.daily_digest_enabled)
    config.urgent_notifications_enabled = env_bool(
        "PATENT_URGENT_NOTIFICATIONS_ENABLED", config.urgent_notifications_enabled
    )
    config.urgent_max_digest_share = max(
        0.0, min(0.01, float(env_value("PATENT_URGENT_MAX_SHARE", config.urgent_max_digest_share)))
    )
    config.urgent_min_importance = float(
        env_value("PATENT_URGENT_MIN_IMPORTANCE", config.urgent_min_importance)
    )
    config.urgent_min_short_term_market_impact = float(
        env_value("PATENT_URGENT_MIN_SHORT_TERM", config.urgent_min_short_term_market_impact)
    )
    config.urgent_min_materiality = float(
        env_value("PATENT_URGENT_MIN_MATERIALITY", config.urgent_min_materiality)
    )
    config.urgent_min_company_percentile = float(
        env_value("PATENT_URGENT_MIN_COMPANY_PERCENTILE", config.urgent_min_company_percentile)
    )


def acquire_lock(lock_path: Path) -> None:
    acquire_lock_file(lock_path, "patent-monitor-daily", 6 * 3600)


def close_pipeline(pipeline: PatentPipeline | None) -> None:
    if pipeline is None:
        return
    try:
        pipeline.database.close()
    except sqlite3.Error:
        pass


def prepare_company_search_order(
    database: Path, companies: list[Any], today: date,
) -> tuple[list[Any], dict[str, int]]:
    """Prioritize unfinished/old searches and rotate ties by date."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS company_search_state (
                company_id TEXT PRIMARY KEY,
                last_attempt_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT company_id, last_attempt_at, last_success_at, last_error, attempt_count FROM company_search_state"
            )
        }
        connection.commit()
    finally:
        connection.close()

    targets = [company for company in companies if company.target]
    if not targets:
        return [], {"pending": 0, "never_searched": 0}
    rotation = today.toordinal() % len(targets)
    rotated = targets[rotation:] + targets[:rotation]
    tie_order = {company.company_id: index for index, company in enumerate(rotated)}

    def key(company: Any) -> tuple[int, str, int]:
        state = rows.get(company.company_id, ("", "", "", 0))
        last_attempt, _last_success, error, _attempt_count = state
        # Day precision preserves recovery priority after an interrupted day,
        # while allowing the daily rotation to change order after a full run.
        attempt_day = last_attempt[:10] if last_attempt else "0000-00-00"
        return (0 if error else 1, attempt_day, tie_order[company.company_id])

    ordered = sorted(targets, key=key)
    return ordered, {
        "pending": sum(bool(rows.get(company.company_id, ("", "", "", 0))[2]) for company in targets),
        "never_searched": sum(not rows.get(company.company_id, ("", "", "", 0))[0] for company in targets),
        "rotation_offset": rotation,
    }


def record_company_search_result(database: Path, company_id: str, success: bool, error: str) -> None:
    timestamp = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO company_search_state
                (company_id, last_attempt_at, last_success_at, last_error, attempt_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(company_id) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=CASE WHEN excluded.last_error='' THEN excluded.last_success_at ELSE company_search_state.last_success_at END,
                last_error=excluded.last_error,
                attempt_count=company_search_state.attempt_count+1
            """,
            (company_id, timestamp, timestamp if success else "", error[:1500]),
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    state = load_runtime_state()
    timezone = ZoneInfo(str(env_value("PATENT_TIMEZONE", "Asia/Tokyo")))
    today = datetime.now(timezone).date()
    output = resolve_path(env_value("PATENT_OUTPUT_DIR", state.get("output", DEFAULT_OUTPUT)))
    database = resolve_path(
        env_value("PATENT_DB_PATH", state.get("db", output / "patent_monitor.sqlite3"))
    )
    lock_path = output / ".daily_run.lock"
    daily_dir = output / "daily_logs"
    daily_dir.mkdir(parents=True, exist_ok=True)
    log_path = daily_dir / f"{today.isoformat()}.log"
    status_path = daily_dir / "latest_status.json"
    status: dict[str, object] = {
        "started_at": datetime.now(timezone).isoformat(timespec="seconds"),
        "success": False,
        "environment": "github_actions" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
    }
    exit_code = 0
    pipeline: PatentPipeline | None = None
    store: SupabaseStateStore | None = None
    acquired = False
    try:
        acquire_lock(lock_path)
        acquired = True
        supabase_config = SupabaseStateConfig.from_env()
        if (os.getenv("GITHUB_ACTIONS") == "true" or env_bool("PATENT_REQUIRE_SUPABASE", False)) and not supabase_config:
            raise RuntimeError("GitHub Actions運用ではSupabase設定が必須です")
        if supabase_config:
            store = SupabaseStateStore(supabase_config)
            restore_result = store.restore(database)
            status["supabase_restore"] = restore_result
            log(log_path, f"Supabase状態復元: {restore_result}")

        secrets = load_credentials()
        required = ["epo_ops_key", "epo_ops_secret"]
        missing = [key for key in required if not secrets.get(key)]
        if missing:
            raise RuntimeError("未設定の資格情報: " + ", ".join(missing))

        optional_missing = [
            key for key in ("openai_api_key", "gemini_api_key") if not secrets.get(key)
        ]
        if optional_missing:
            status["ai_credentials_warning"] = optional_missing
            log(
                log_path,
                "AI credentials missing; affected reviews will remain pending: "
                + ", ".join(optional_missing),
            )

        config_path = resolve_path(env_value("PATENT_CONFIG_PATH", state.get("config", ROOT / "patent_monitor_config.json")))
        config = PipelineConfig.from_json(config_path)
        apply_runtime_overrides(config, state)
        smtp_host = str(env_value("SMTP_HOST", state.get("smtp_host", "smtp.gmail.com")))
        smtp_port = int(env_value("SMTP_PORT", state.get("smtp_port", 587)))
        smtp_user = str(env_value("SMTP_USER", state.get("smtp_user", "")))
        recipient = str(env_value("EMAIL_RECIPIENT", state.get("email_recipient", smtp_user)))
        email_enabled = config.daily_digest_enabled or config.urgent_notifications_enabled
        smtp_configured = bool(smtp_user and recipient and secrets.get("smtp_password"))
        if email_enabled and not smtp_configured:
            status["email_configuration_warning"] = (
                "SMTP is not configured; important patents will remain in the email outbox."
            )
            log(log_path, str(status["email_configuration_warning"]))
        if config.adaptive_gemini_escalation and database.exists():
            try:
                connection = sqlite3.connect(database)
                try:
                    row = connection.execute(
                        "SELECT setting_value FROM adaptive_settings WHERE setting_key='gemini_escalation_threshold'"
                    ).fetchone()
                finally:
                    connection.close()
                if row:
                    config.gemini_escalation_threshold = float(row[0])
            except (sqlite3.Error, ValueError):
                pass

        company_path = resolve_path(
            env_value("PATENT_COMPANY_MASTER", state.get("company", ROOT / "patent_company_master.csv"))
        )
        companies = read_companies(company_path)
        if not companies:
            raise RuntimeError(f"監視企業マスタが空です: {company_path}")

        if env_bool("PATENT_COMPANY_MASTER_AUDIT_ENABLED", True):
            try:
                from audit_patent_company_master import audit as audit_company_master
                master_audit = audit_company_master(SimpleNamespace(
                    company_master=str(company_path),
                    config=str(config_path),
                    output_dir=str(output / "company_master_audit"),
                    search_name_limit=None,
                    short_name_length=3,
                ))
                status["company_master_audit"] = {
                    "overflow_company_count": master_audit["overflow_company_count"],
                    "duplicate_normalized_name_count": master_audit["duplicate_normalized_name_count"],
                    "short_normalized_name_count": master_audit["short_normalized_name_count"],
                    "output_files": master_audit["output_files"],
                }
                log(log_path, f"企業マスタ監査完了 {status['company_master_audit']}")
            except Exception as exc:
                status["company_master_audit_warning"] = f"{type(exc).__name__}: {exc}"
                log(log_path, "企業マスタ監査警告 " + str(status["company_master_audit_warning"]))

        provider = EPOOPSProvider(
            secrets["epo_ops_key"], secrets["epo_ops_secret"],
            config.ops_requests_per_minute, config.request_timeout_seconds,
        )
        if config.ops_detail_cache_enabled:
            provider.set_detail_cache(config.ops_detail_cache_path or (output / "epo_detail_cache.sqlite3"))
        lookback_days = max(1, int(env_value("PATENT_LOOKBACK_DAYS", "3")))
        start = today - timedelta(days=lookback_days - 1)
        max_per_company = int(env_value("PATENT_MAX_PER_COMPANY", state.get("max_records", 100)))
        budget_started_monotonic = time.monotonic()
        legacy_company_guard = env_value(
            "PATENT_GITHUB_ACTIONS_TIME_GUARD_MINUTES",
            config.company_search_time_guard_minutes,
        )
        company_guard_minutes = float(env_value(
            "PATENT_COMPANY_SEARCH_TIME_GUARD_MINUTES", legacy_company_guard
        ))
        postprocess_guard_minutes = float(env_value(
            "PATENT_POSTPROCESS_TIME_GUARD_MINUTES", config.postprocess_time_guard_minutes
        ))
        finalization_reserve_minutes = max(0.0, float(env_value(
            "PATENT_FINALIZATION_RESERVE_MINUTES", config.finalization_reserve_minutes
        )))
        company_deadline_monotonic = None
        total_deadline_monotonic = None
        network_stage_deadline_monotonic = None
        if company_guard_minutes > 0:
            company_deadline_monotonic = (
                budget_started_monotonic + company_guard_minutes * 60.0
            )
        if company_guard_minutes > 0 and postprocess_guard_minutes > 0:
            total_deadline_monotonic = budget_started_monotonic + (
                company_guard_minutes + postprocess_guard_minutes
            ) * 60.0
            network_stage_deadline_monotonic = max(
                budget_started_monotonic,
                total_deadline_monotonic - finalization_reserve_minutes * 60.0,
            )
        status["time_budget"] = {
            "company_search_minutes": company_guard_minutes,
            "postprocess_minutes": postprocess_guard_minutes,
            "total_minutes": company_guard_minutes + postprocess_guard_minutes,
            "finalization_reserve_minutes": finalization_reserve_minutes,
        }
        log(
            log_path,
            f"実行時間配分: 全体={company_guard_minutes + postprocess_guard_minutes:g}分 "
            f"企業検索最大={company_guard_minutes:g}分 後段={postprocess_guard_minutes:g}分 "
            f"AI判定・メール確保={finalization_reserve_minutes:g}分",
        )
        ordered_companies, order_summary = prepare_company_search_order(database, companies, today)
        status["company_search_order"] = order_summary
        log(
            log_path,
            f"EPO企業別検索開始 {start}～{today} / {len(ordered_companies)}社 / "
            f"前回失敗={order_summary['pending']} 未探索={order_summary['never_searched']} "
            f"日次開始位置={order_summary['rotation_offset']}",
        )
        if store:
            store.backup(database)
        checkpoint_every = max(5, int(env_value("PATENT_SEARCH_CHECKPOINT_EVERY", "20")))
        checkpoint_counter = 0

        def on_company_result(company: Any, success: bool, error: str) -> None:
            nonlocal checkpoint_counter
            record_company_search_result(database, company.company_id, success, error)
            checkpoint_counter += 1
            if store and checkpoint_counter % checkpoint_every == 0:
                store.backup(database)
                log(log_path, f"探索状態チェックポイント保存 {checkpoint_counter}/{len(ordered_companies)}社")

        records = provider.search_companies(
            ordered_companies, start.isoformat(), today.isoformat(), max_per_company=max_per_company,
            progress=lambda message: log(log_path, message),
            company_result=on_company_result,
            max_applicant_names=config.ops_company_search_name_limit,
            deadline_monotonic=company_deadline_monotonic,
            checkpoint_path=output / "company_search_checkpoint.json",
        )
        log(log_path, f"EPO取得完了 {len(records)}件")
        search_stats = provider.last_company_search_stats
        retry_queue = [
            {
                "window_start": start.isoformat(),
                "window_end": today.isoformat(),
                "company_id": row.get("company_id", ""),
                "company_name": row.get("company_name", ""),
                "record_count": row.get("record_count", 0),
                "max_per_company": max_per_company,
                "searched_names": " | ".join(row.get("searched_names", [])),
                "retry_reason": "hit_max_per_company",
            }
            for row in search_stats.get("capped_companies", [])
        ]
        retry_queue.extend(
            {
                "window_start": start.isoformat(),
                "window_end": today.isoformat(),
                "company_id": row.get("company_id", ""),
                "company_name": row.get("company_name", ""),
                "record_count": 0,
                "max_per_company": max_per_company,
                "searched_names": "",
                "retry_reason": "runtime_guard_skipped",
            }
            for row in search_stats.get("skipped_companies", [])
        )
        retry_queue_path = output / "capped_companies_retry_queue.csv"
        write_csv(retry_queue_path, retry_queue, [
            "window_start", "window_end", "company_id", "company_name", "record_count",
            "max_per_company", "searched_names", "retry_reason",
        ])
        status["company_search_stats"] = {
            "failure_count": search_stats.get("failure_count", 0),
            "capped_company_count": len(search_stats.get("capped_companies", [])),
            "skipped_company_count": len(search_stats.get("skipped_companies", [])),
            "stopped_by_deadline": search_stats.get("stopped_by_deadline", False),
            "retry_queue": str(retry_queue_path),
        }
        technology_records: list[Any] = []
        technology_stats: dict[str, Any] = {
            "status": "disabled",
            "unique_records": 0,
        }
        company_search_aborted = company_search_blocks_postprocess(search_stats)
        if config.technology_discovery_enabled:
            if company_search_aborted:
                technology_stats = {
                    "status": "skipped_company_search_incomplete",
                    "unique_records": 0,
                }
                log(log_path, "企業検索が中断されたため、本日の技術別検索は翌日に回します")
            elif (
                network_stage_deadline_monotonic is not None
                and time.monotonic() >= network_stage_deadline_monotonic
            ):
                technology_stats = {"status": "skipped_deadline", "unique_records": 0}
                log(log_path, "後段処理の時間上限のため、本日の技術別検索は翌日に回します")
            else:
                technology_lookback_days = max(
                    1, int(config.technology_discovery_lookback_days)
                )
                technology_start = today - timedelta(days=technology_lookback_days - 1)
                log(
                    log_path,
                    f"企業検索フェーズ完了。重点技術の新着特許を検索します "
                    f"{technology_start}～{today}（重複は通知済み判定で除外）",
                )
                technology_records, technology_stats = provider.search_technologies(
                    config.technology_discovery_queries,
                    technology_start.isoformat(),
                    today.isoformat(),
                    max_records_per_query=config.technology_discovery_max_records_per_query,
                    deadline_monotonic=network_stage_deadline_monotonic,
                    progress=lambda message: log(log_path, message),
                )
                technology_stats["status"] = (
                    "completed_with_errors" if technology_stats.get("error_count") else "completed"
                )
                technology_stats["lookback_days"] = technology_lookback_days
                by_identity = {record.identity: record for record in records}
                added = 0
                for technology_record in technology_records:
                    existing = by_identity.get(technology_record.identity)
                    if existing is None:
                        records.append(technology_record)
                        by_identity[technology_record.identity] = technology_record
                        added += 1
                        continue
                    existing.raw["technology_discovery"] = True
                    categories = existing.raw.setdefault("technology_categories", [])
                    for category in technology_record.raw.get("technology_categories", []):
                        if category not in categories:
                            categories.append(category)
                technology_stats["added_to_pipeline"] = added
                log(
                    log_path,
                    f"重点技術検索完了: 固有候補={technology_stats.get('unique_records', 0)} "
                    f"追加={added}",
                )
        status["technology_discovery"] = technology_stats
        detail_summary = None
        if config.ops_enrich_details:
            log(log_path, "監視企業候補のCPC/IPC・英語要約をEPOから補完します")
            detail_summary = provider.enrich_records(
                records,
                companies,
                max_records=config.ops_detail_max_records,
                match_threshold=config.fuzzy_match_threshold,
                match_margin=config.fuzzy_match_margin,
                enrich_claims=config.ops_enrich_claims,
                claims_top_rate=config.ops_claims_fulltext_top_rate,
                enrich_family_legal=config.ops_enrich_family_legal,
                enrich_forward_citations=config.ops_enrich_forward_citations,
                forward_citation_max_records=config.ops_forward_citation_max_records,
                include_technology_discovery=config.technology_discovery_enabled,
                progress=lambda message: log(log_path, message),
                stop_requested=lambda: (
                    network_stage_deadline_monotonic is not None
                    and time.monotonic() >= network_stage_deadline_monotonic
                ),
            )
            status["epo_detail_enrichment"] = detail_summary
            log(log_path, f"EPO詳細補完完了: {detail_summary}")

        urgent_sent_this_run = 0
        technology_email_candidates: list[dict[str, Any]] = []

        def on_item_scored(run_id: str, item: Any) -> None:
            nonlocal urgent_sent_this_run
            row = scored_item_to_email_row(item)
            if (
                item.patent.raw.get("technology_discovery")
                and row["_decision"] not in {"important", "urgent"}
            ):
                technology_row = dict(row)
                technology_row["_decision"] = "technology"
                technology_email_candidates.append(technology_row)
            if row["_decision"] not in {"important", "urgent"}:
                return
            register_digest_candidate(database, row, run_id)
            if not config.urgent_notifications_enabled:
                return
            if not smtp_configured:
                return
            qualifies, reason = qualifies_as_urgent(row, config)
            if not qualifies:
                log(log_path, f"緊急通知対象外 {row['_patent_id']}: {reason}")
                return
            if was_notified(database, row["_patent_id"], URGENT_CHANNEL):
                return
            available, budget = urgent_budget_available(database, config.urgent_max_digest_share)
            if not available:
                log(
                    log_path,
                    f"緊急通知比率上限により日次掲載へ {row['_patent_id']}: "
                    f"緊急={budget['urgent_count']}/{budget['candidate_count']} "
                    f"許容={budget['allowed_urgent_count']}",
                )
                return
            send_patent_email(
                [row], smtp_host, smtp_port, smtp_user, secrets["smtp_password"],
                recipient, run_id, kind="urgent",
            )
            mark_notified(database, [row], run_id, recipient, channel=URGENT_CHANNEL)
            urgent_sent_this_run += 1
            log(log_path, f"最重要特許を即時メール送信: {row['_patent_id']} {row.get('company_name', '')}")

        pipeline = PatentPipeline(
            config, companies, database, output,
            openai_api_key=secrets.get("openai_api_key", ""),
            gemini_api_key=secrets.get("gemini_api_key", ""),
            progress=lambda message, ratio: log(log_path, f"{ratio * 100:.1f}% {message}"),
            item_scored=on_item_scored,
        )
        result = pipeline.run(records, "epo_ops_daily")
        if detail_summary is not None:
            result["epo_detail_enrichment"] = detail_summary
        technology_limit = max(0, int(config.technology_discovery_email_limit))
        selected_technology_rows = sorted(
            technology_email_candidates,
            key=lambda row: (-float(row.get("final_score", 0) or 0), str(row.get("_patent_id", ""))),
        )[:technology_limit]
        for technology_row in selected_technology_rows:
            register_digest_candidate(database, technology_row, result["run_id"])
        result["technology_discovery"] = {
            **technology_stats,
            "email_candidate_count": len(technology_email_candidates),
            "email_selected_count": len(selected_technology_rows),
            "email_limit": technology_limit,
        }
        status["patent_run"] = result
        pipeline = None  # run() closes its database on successful completion.

        if env_bool("PATENT_RUN_DIAGNOSTICS_ENABLED", True):
            try:
                from analyze_patent_monitor_run import analyze as analyze_run
                diagnostic = analyze_run(SimpleNamespace(
                    run_dir=str(Path(result["summary_json"]).parent),
                    previous_run_dir="",
                    output_root=str(output),
                    company_master=str(company_path),
                    config=str(config_path),
                    output_dir=str(Path(result["summary_json"]).parent),
                    high_technology_threshold=float(env_value("PATENT_DIAGNOSTIC_HIGH_TECH_THRESHOLD", 24.0)),
                    near_miss_threshold=float(env_value("PATENT_DIAGNOSTIC_NEAR_MISS_THRESHOLD", 0.82)),
                    alias_patch_threshold=float(env_value("PATENT_DIAGNOSTIC_ALIAS_PATCH_THRESHOLD", 0.90)),
                    alias_patch_gap=float(env_value("PATENT_DIAGNOSTIC_ALIAS_PATCH_GAP", 0.20)),
                ))
                status["run_diagnostics"] = {
                    "currently_matchable_leak_count": diagnostic["findings"]["currently_matchable_leak_count"],
                    "near_miss_company_candidate_count": diagnostic["findings"]["near_miss_company_candidate_count"],
                    "suggested_alias_patch_count": diagnostic["findings"]["suggested_alias_patch_count"],
                    "high_technology_unmatched_count": diagnostic["findings"]["high_technology_unmatched_count"],
                    "jp_like_unmatched_applicant_count": diagnostic["findings"]["jp_like_unmatched_applicant_count"],
                    "output_files": diagnostic["output_files"],
                }
                log(log_path, f"run診断完了 {status['run_diagnostics']}")
            except Exception as exc:
                status["run_diagnostics_warning"] = f"{type(exc).__name__}: {exc}"
                log(log_path, "run診断警告 " + str(status["run_diagnostics_warning"]))

        digest_rows(result["evaluation_csv"], database, result["run_id"])
        rows = pending_digest_rows(database)
        digest_already_sent = daily_digest_was_sent(database, today.isoformat())
        if config.daily_digest_enabled and smtp_configured and not digest_already_sent:
            send_patent_email(
                rows, smtp_host, smtp_port, smtp_user, secrets["smtp_password"], recipient,
                result["run_id"], kind="digest",
            )
            mark_notified(database, rows, result["run_id"], recipient, channel=DIGEST_CHANNEL)
            mark_daily_digest_sent(database, today.isoformat(), result["run_id"], recipient)
            log(log_path, f"日次ダイジェストメール送信 掲載={len(rows)}件 緊急送信={urgent_sent_this_run}件")
        elif digest_already_sent:
            log(log_path, f"本日の日次ダイジェストは送信済み 掲載候補={len(rows)}件")
        elif config.daily_digest_enabled and not smtp_configured:
            log(log_path, f"SMTP未設定のため重要特許{len(rows)}件をメール送信待ちにしました")
        else:
            log(log_path, f"日次ダイジェスト無効 掲載候補={len(rows)}件 緊急送信={urgent_sent_this_run}件")
        status["notifications"] = {
            "digest_count": len(rows),
            "urgent_count": urgent_sent_this_run,
            "urgent_max_share": config.urgent_max_digest_share,
            "digest_already_sent": digest_already_sent,
            "smtp_configured": smtp_configured,
            "pending_outbox_count": len(rows),
        }

        if env_bool("PATENT_ENABLE_MARKET_FEEDBACK", True):
            try:
                feedback = MarketFeedbackService(database, companies, output).run(
                    progress=lambda message, ratio: log(log_path, f"株価 {ratio * 100:.1f}% {message}")
                )
                status["market_feedback"] = feedback.__dict__
            except Exception as exc:
                status["market_feedback_warning"] = f"{type(exc).__name__}: {exc}"
                log(log_path, "株価答え合わせ警告: " + str(status["market_feedback_warning"]))
        status["success"] = True
        log(log_path, "日次処理本体完了")
    except Exception as exc:
        exit_code = 1
        status["error"] = f"{type(exc).__name__}: {exc}"
        status["traceback"] = traceback.format_exc()
        log(log_path, "日次処理失敗: " + str(status["error"]))
        try:
            secrets = locals().get("secrets", {})
            user = str(env_value("SMTP_USER", state.get("smtp_user", "")))
            recipient = str(env_value("EMAIL_RECIPIENT", state.get("email_recipient", user)))
            if user and recipient and secrets.get("smtp_password"):
                from patent_monitor.notifications import send_status_email
                send_status_email(
                    str(env_value("SMTP_HOST", state.get("smtp_host", "smtp.gmail.com"))),
                    int(env_value("SMTP_PORT", state.get("smtp_port", 587))),
                    user, secrets["smtp_password"], recipient,
                    "【エラー】特許材料性モニターの日次処理に失敗",
                    str(status["error"]) + "\n\nGitHub Actionsまたは日次ログを確認してください。",
                )
        except Exception as mail_exc:
            log(log_path, f"エラー通知メールも失敗: {type(mail_exc).__name__}: {mail_exc}")
    finally:
        close_pipeline(pipeline)
        if store and database.exists():
            try:
                backup_result = store.backup(database)
                status["supabase_backup"] = backup_result
                log(log_path, f"Supabase状態保存: {backup_result}")
            except Exception as backup_exc:
                exit_code = 1
                status["success"] = False
                status["supabase_backup_error"] = f"{type(backup_exc).__name__}: {backup_exc}"
                log(log_path, "Supabase状態保存失敗: " + str(status["supabase_backup_error"]))
        status["completed_at"] = datetime.now(timezone).isoformat(timespec="seconds")
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        if acquired:
            lock_path.unlink(missing_ok=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
