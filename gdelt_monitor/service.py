from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .ai import (
    review_candidates, review_emergency_alerts, review_emergency_alerts_with_jev,
    run_emergency_ai_self_test,
)
from .adaptive import (
    detect_stability, load_state, lookback_minutes, mark_stability_alert_sent,
    on_403, on_429, on_run_without_429, save_state, stability_alert_parameters,
    snapshot, waiting_status,
)
from .analysis import load_candidates_since, load_company_aliases, match_and_score, save_articles
from .collector import (
    CollectorError, NonJsonResponse, RateLimited, RequestTimeout, build_tasks, fetch_with_backoff,
)
from .config import RuntimePaths, load_config, operation_stage
from .database import NewsDatabase
from .emergency import EmergencyScoutResult, run_emergency_scout
from .global_news import select_daily_world_news
from .notifications import (
    build_digest, notification_sent, send_digest, send_emergency_alert, send_stability_alert,
)
from .ngram_collector import NgramCollection, collect_web_ngrams


JST = ZoneInfo("Asia/Tokyo")


def _merge_emergency_approvals(
    gpt_approved: list[dict[str, Any]], jev_approved: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    approved_by_id: dict[str, dict[str, Any]] = {}
    for alert in gpt_approved:
        merged = dict(alert)
        merged["approval_sources"] = ["openai"]
        approved_by_id[str(alert.get("alert_id") or alert["event_key"])] = merged
    for alert in jev_approved:
        identity = str(alert.get("alert_id") or alert["event_key"])
        if identity in approved_by_id:
            approved_by_id[identity]["jev_review"] = alert.get("jev_review", {})
            approved_by_id[identity]["approval_sources"].append("jev")
        else:
            merged = dict(alert)
            merged["approval_sources"] = ["jev"]
            approved_by_id[identity] = merged
    return list(approved_by_id.values())


def _emergency_snapshot(
    result: EmergencyScoutResult,
    sent: int,
    send_errors: list[str],
    ai_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    ai_stats = ai_stats or {}
    return {
        "emergency_status": result.status,
        "emergency_head_requests": result.head_requests,
        "emergency_discovered_files": result.discovered_files,
        "emergency_scanned_files": result.scanned_files,
        "emergency_downloaded_bytes": result.downloaded_bytes,
        "emergency_candidates_found": result.candidates_found,
        "emergency_ready_alerts": len(result.ready_alerts),
        "emergency_sent_alerts": sent,
        "emergency_ai_sent": ai_stats.get("sent", 0),
        "emergency_ai_approved": ai_stats.get("approved", 0),
        "emergency_ai_cached": ai_stats.get("cached", 0),
        "emergency_ai_budget_skipped": ai_stats.get("budget_skipped", 0),
        "emergency_ai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "emergency_jev_sent": ai_stats.get("jev_sent", 0),
        "emergency_jev_approved": ai_stats.get("jev_approved", 0),
        "emergency_jev_cached": ai_stats.get("jev_cached", 0),
        "emergency_jev_budget_skipped": ai_stats.get("jev_budget_skipped", 0),
        "emergency_jev_configured": bool(os.getenv("TYPESAFE_API_KEY", "").strip()),
        "emergency_parallel_approved": ai_stats.get("parallel_approved", 0),
        "emergency_next_scan_at": result.next_scan_at,
        "emergency_errors": [*result.errors, *send_errors],
    }


def _summary(run_id: str, start: datetime, end: datetime, planned: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "planned_queries": planned,
        "errors": [],
    }
    for field in (
        "executed_queries successful_queries empty_responses non_json_responses http_403 http_429 "
        "timeouts other_errors fetched_articles inserted_articles duplicate_articles company_matches "
        "scored_candidates gpt_sent gpt_passed gemini_sent final_candidates urgent_candidates carried_over"
    ).split():
        result[field] = 0
    return result


def run_daily_service(
    max_queries: int | None = None,
    force_email: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    stage = operation_stage()
    database = NewsDatabase(paths.database)
    database.set_state(
        "emergency_ai_key_configured",
        "true" if os.getenv("OPENAI_API_KEY", "").strip() else "false",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    database.set_state(
        "emergency_jev_key_configured",
        "true" if os.getenv("TYPESAFE_API_KEY", "").strip() else "false",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    collector_config = config.get("collector", {})
    adaptive_config = config.get("adaptive_control", {})
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = load_state(database, adaptive_config, end)
    tasks = build_tasks(config)
    emergency_ai_self_test = (
        run_emergency_ai_self_test(database, config.get("emergency_monitor", {}), end)
        if stage >= 3
        else "stage_skipped"
    )
    if max_queries is not None and max_queries >= 0:
        tasks = tasks[:max_queries]
    emergency_result = run_emergency_scout(
        database,
        config.get("emergency_monitor", {}),
        collector_config,
        end,
        paths.toc_cache,
        config.get("global_news_digest", {}),
    )
    emergency_sent = 0
    emergency_send_errors: list[str] = []
    emergency_ai_stats: dict[str, int] = {}
    if stage >= 3:
        emergency_settings = config.get("emergency_monitor", {})
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="emergency-review") as pool:
            gpt_future = pool.submit(
                review_emergency_alerts,
                database,
                emergency_result.ready_alerts,
                emergency_settings,
                end,
            )
            jev_future = pool.submit(
                review_emergency_alerts_with_jev,
                database,
                emergency_result.ready_alerts,
                emergency_settings,
                end,
            )
            try:
                gpt_approved, gpt_stats, gpt_errors = gpt_future.result()
            except Exception as exc:
                gpt_approved = []
                gpt_stats = {"sent": 0, "approved": 0, "cached": 0, "budget_skipped": 0}
                gpt_errors = [f"OpenAI review worker: {type(exc).__name__}: {exc}"]
            try:
                jev_approved, jev_stats, jev_errors = jev_future.result()
            except Exception as exc:
                jev_approved = []
                jev_stats = {"sent": 0, "approved": 0, "cached": 0, "budget_skipped": 0}
                jev_errors = [f"Jev review worker: {type(exc).__name__}: {exc}"]

        approved_alerts = _merge_emergency_approvals(gpt_approved, jev_approved)
        emergency_result.ready_alerts = approved_alerts
        emergency_ai_stats = {
            **gpt_stats,
            "gpt_approved": gpt_stats.get("approved", 0),
            "jev_sent": jev_stats.get("sent", 0),
            "jev_approved": jev_stats.get("approved", 0),
            "jev_cached": jev_stats.get("cached", 0),
            "jev_budget_skipped": jev_stats.get("budget_skipped", 0),
            "parallel_approved": len(approved_alerts),
        }
        emergency_result.errors.extend([*gpt_errors, *jev_errors])
    if stage >= 4:
        for alert in emergency_result.ready_alerts:
            try:
                send_emergency_alert(alert)
                database.record_emergency_alert(
                    alert, datetime.now(timezone.utc).isoformat(timespec="seconds")
                )
                emergency_sent += 1
            except Exception as exc:
                emergency_send_errors.append(f"{type(exc).__name__}: {exc}")
    emergency_details = _emergency_snapshot(
        emergency_result, emergency_sent, emergency_send_errors, emergency_ai_stats
    )
    emergency_details["emergency_ai_self_test"] = emergency_ai_self_test
    if force_email and max_queries == 0:
        _maybe_email(database, config, state, True)
        return {"status": "email_only", **snapshot(state), **emergency_details}
    wait_status = waiting_status(state, end)
    if wait_status:
        _maybe_email(database, config, state, force_email)
        return {
            "status": wait_status,
            "planned_queries": len(tasks),
            "executed_queries": 0,
            "carried_over": len(tasks),
            **snapshot(state),
            **emergency_details,
        }
    start = end - timedelta(minutes=lookback_minutes(state, collector_config, adaptive_config))
    summary = _summary(uuid.uuid4().hex, start, end, len(tasks))
    summary.update(emergency_details)
    for message in emergency_details["emergency_errors"]:
        summary["other_errors"] += 1
        summary["errors"].append(
            {"query": "emergency_scout", "type": "emergency_error", "message": message}
        )
    database.start_run(summary)
    all_article_ids: list[str] = []
    stopped_by_limit = False
    try:
        collector_source = str(collector_config.get("source", "doc_api")).strip().casefold()
        ngram_collection: NgramCollection | None = None
        ngram_error: CollectorError | None = None
        collection_aliases = None
        if collector_source == "web_ngrams":
            collection_aliases = load_company_aliases(paths.company_master, paths.market_cap_snapshot)
            processed_stamps = database.processed_ngram_stamps_since(
                start.strftime("%Y%m%d%H%M00")
            )
            try:
                ngram_collection = collect_web_ngrams(
                    tasks,
                    collection_aliases,
                    start,
                    end,
                    collector_config,
                    processed_stamps,
                    paths.toc_cache,
                )
                summary.update(
                    {
                        "ngram_head_requests": ngram_collection.head_requests,
                        "ngram_discovered_files": len(ngram_collection.discovered_files),
                        "ngram_processed_files": len(ngram_collection.processed_files),
                        "ngram_failed_files": len(ngram_collection.failed_files),
                        "ngram_downloaded_bytes": ngram_collection.downloaded_bytes,
                    }
                )
                for failure in ngram_collection.failed_files:
                    summary["other_errors"] += 1
                    summary["errors"].append(
                        {
                            "query": f"web_ngrams:{failure['stamp']}",
                            "type": "ngram_error",
                            "message": failure["message"],
                        }
                    )
            except CollectorError as exc:
                ngram_error = exc
        for index, task in enumerate(tasks):
            started = time.monotonic()
            summary["executed_queries"] += 1
            query_status = "completed"
            error_type = error_message = ""
            fetched = inserted = 0
            try:
                if collector_source == "web_ngrams":
                    if ngram_error is not None:
                        raise ngram_error
                    assert ngram_collection is not None
                    articles = ngram_collection.articles_by_query.get(task.query_id, [])
                else:
                    articles = fetch_with_backoff(task, start, end, collector_config)
                fetched = len(articles)
                summary["successful_queries"] += 1
                summary["fetched_articles"] += fetched
                if not articles:
                    summary["empty_responses"] += 1
                inserted, duplicates, ids = save_articles(database, summary["run_id"], task, articles)
                summary["inserted_articles"] += inserted
                summary["duplicate_articles"] += duplicates
                all_article_ids.extend(ids)
            except RateLimited as exc:
                query_status, error_type, error_message = "rate_limited", exc.error_type, str(exc)
                summary["http_403" if exc.status_code == 403 else "http_429"] += 1
                if exc.status_code == 429:
                    state, previous = on_429(
                        state, adaptive_config, end, exc.retry_after_seconds,
                        intra_run_spacing_exposed=summary["successful_queries"] > 0,
                    )
                    event_type = "recovery_probe_429" if previous.recovery_probe_pending else "http_429"
                else:
                    state, previous = on_403(state, end, exc.retry_after_seconds)
                    event_type = "http_403"
                save_state(database, state, end, event_type, previous)
                stopped_by_limit = True
            except NonJsonResponse as exc:
                query_status, error_type, error_message = "failed", exc.error_type, str(exc)
                summary["non_json_responses"] += 1
            except RequestTimeout as exc:
                query_status, error_type, error_message = "failed", exc.error_type, str(exc)
                summary["timeouts"] += 1
            except CollectorError as exc:
                query_status, error_type, error_message = "failed", exc.error_type, str(exc)
                summary["other_errors"] += 1
            if error_message:
                summary["errors"].append({"query": task.name, "type": error_type, "message": error_message})
            with database.connection() as connection:
                connection.execute(
                    """INSERT INTO query_runs(
                       run_id,query_id,query_name,status,fetched_count,inserted_count,duration_seconds,
                       error_type,error_message) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        summary["run_id"], task.query_id, task.name, query_status, fetched, inserted,
                        round(time.monotonic() - started, 3), error_type, error_message[:2000],
                    ),
                )
            if stopped_by_limit:
                summary["carried_over"] = len(tasks) - index - 1
                break
            if index < len(tasks) - 1 and collector_source != "web_ngrams":
                time.sleep(max(0.0, state.query_spacing_seconds))

        if ngram_collection is not None:
            processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in ngram_collection.processed_files:
                database.record_ngram_file(
                    item["stamp"], processed_at, item["matched_documents"], item["bytes"]
                )

        candidates: list[dict[str, Any]] = []
        if stage >= 2:
            aliases = collection_aliases or load_company_aliases(
                paths.company_master, paths.market_cap_snapshot
            )
            matches, scored, candidates = match_and_score(database, all_article_ids, aliases, config)
            summary["company_matches"] = matches
            summary["scored_candidates"] = scored
            summary["final_candidates"] = len(candidates)
            urgent_threshold = float(config.get("urgent_score_threshold", 90))
            summary["urgent_candidates"] = sum(item["score"] >= urgent_threshold for item in candidates)
        if stage >= 3:
            try:
                summary.update(review_candidates(database, candidates, config))
            except Exception as exc:
                summary["other_errors"] += 1
                summary["carried_over"] += sum(
                    item["score"] >= float(config.get("ai", {}).get("gpt_score_threshold", 60))
                    for item in candidates
                )
                summary["errors"].append(
                    {"query": "ai_review", "type": "ai_error", "message": f"{type(exc).__name__}: {exc}"}
                )
        error_total = sum(summary[key] for key in ("non_json_responses", "http_403", "http_429", "timeouts", "other_errors"))
        if not stopped_by_limit:
            fully_successful = bool(tasks) and summary["successful_queries"] == len(tasks)
            state, previous, event_type = on_run_without_429(
                state, adaptive_config, end, fully_successful
            )
            save_state(database, state, end, event_type, previous)
        summary.update(snapshot(state))
        summary["status"] = (
            "stopped_by_rate_limit" if stopped_by_limit else
            "failed" if summary["successful_queries"] == 0 and error_total else
            "completed_with_errors" if error_total else "completed"
        )
        summary["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        database.finish_run(summary)
        stability_settings = config.get("stability_detection", {})
        stability = (
            detect_stability(database, stability_settings, end)
            if stability_settings.get("enabled", True)
            else {"status": "固定2時間運用", "reason": "適応学習は使用しません"}
        )
        try:
            _maybe_stability_email(database, config, state, stability, end)
        except Exception as exc:
            summary["other_errors"] += 1
            summary["errors"].append(
                {"query": "stability_alert", "type": "email_error", "message": f"{type(exc).__name__}: {exc}"}
            )
            summary["status"] = "completed_with_errors"
            database.finish_run(summary)
        _maybe_email(database, config, state, force_email)
        return summary
    except Exception:
        summary["status"] = "runner_failed"
        summary["other_errors"] += 1
        summary["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        database.finish_run(summary)
        raise


def _maybe_email(
    database: NewsDatabase,
    config: dict[str, Any],
    state: Any,
    force_email: bool,
) -> None:
    if operation_stage() < 4:
        return
    now = datetime.now(JST)
    digest_hour = int(config.get("digest_hour_jst", 18))
    key = f"gdelt-digest:{now.date().isoformat()}"
    if (not force_email and now.hour < digest_hour) or notification_sent(database, key):
        return
    day_start_jst = datetime(now.year, now.month, now.day, tzinfo=JST)
    since = day_start_jst.astimezone(timezone.utc).isoformat(timespec="seconds")
    stats = database.aggregate_runs(since)
    daily_candidates = load_candidates_since(
        database, since, float(config.get("digest_score_threshold", 40))
    )
    world_news = select_daily_world_news(
        database, config.get("global_news_digest", {}), since, now.date().isoformat()
    )
    errors = database.recent_errors_since(since)
    adaptive_details = snapshot(state)
    stability_settings = config.get("stability_detection", {})
    stability = (
        detect_stability(database, stability_settings, datetime.now(timezone.utc))
        if stability_settings.get("enabled", True)
        else {"status": "固定2時間運用", "reason": "適応学習は使用しません"}
    )
    adaptive_details.update(
        {
            "adaptive_stability_status": stability.get("status"),
            "adaptive_stability_reason": stability.get("reason"),
            "adaptive_stability_samples": stability.get("sample_count"),
            "adaptive_stability_window_hours": stability.get("window_hours"),
        }
    )
    subject, body = build_digest(
        daily_candidates, stats, f"{day_start_jst:%Y-%m-%d %H:%M}～{now:%H:%M} JST",
        errors, adaptive_details, world_news,
    )
    send_digest(database, key, subject, body, [item["article_id"] for item in daily_candidates])


def _maybe_stability_email(
    database: NewsDatabase,
    config: dict[str, Any],
    state: Any,
    result: dict[str, Any],
    now: datetime,
) -> None:
    if operation_stage() < 4:
        return
    settings = config.get("stability_detection", {})
    if not settings.get("enabled", True):
        return
    parameters = stability_alert_parameters(database, result, settings)
    if not parameters:
        return
    send_stability_alert(database, result, parameters, snapshot(state), now)
    mark_stability_alert_sent(database, result, parameters, now)
