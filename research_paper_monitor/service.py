from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ai_review import review_candidates
from .collectors import RateLimited, collect_arxiv, collect_jstage, collect_openalex
from .config import RuntimePaths, load_config, operation_stage
from .database import PaperDatabase
from .notifications import build_digest, flush_outbox, queue_digest
from .rate_limit import cooldown_until, on_rate_limited, on_success
from .scoring import score_paper


Collector = Callable[..., list[Any]]


def run_daily_service(
    *,
    force_email: bool = False,
    baseline: bool = False,
    now: datetime | None = None,
    collectors: dict[str, Collector] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    stage = operation_stage()
    database = PaperDatabase(paths.database)
    run_id = str(uuid.uuid4())
    is_first_run = not bool(database.get_state("baseline_completed"))
    normal_lookback = max(1, int(config.get("collection", {}).get("lookback_days", 7)))
    baseline_lookback = max(normal_lookback, int(config.get("collection", {}).get("initial_baseline_days", 30)))
    lookback_days = baseline_lookback if baseline or is_first_run else normal_lookback
    window_start = current - timedelta(days=lookback_days)
    database.start_run(run_id, current.isoformat(timespec="seconds"), window_start.isoformat(timespec="seconds"), current.isoformat(timespec="seconds"))
    stats: dict[str, int] = {
        "sources_attempted": 0, "sources_succeeded": 0, "fetched": 0,
        "inserted": 0, "updated": 0, "duplicates": 0, "scored": 0,
        "candidates": 0, "ai_sent": 0, "ai_completed": 0, "ai_failed": 0,
        "ai_budget_skipped": 0, "emailed": 0,
        "http_403": 0, "http_406": 0, "http_429": 0, "cooldown_skipped": 0,
    }
    errors: list[str] = []
    new_ids: list[str] = []
    profiles = list(config["profiles"])
    source_config = config.get("sources", {})
    injected = collectors or {}
    source_calls: list[tuple[str, Callable[[], list[Any]]]] = []
    if source_config.get("openalex", {}).get("enabled", True):
        function = injected.get("openalex")
        source_calls.append(("openalex", (lambda fn=function: fn(config, profiles, window_start.date().isoformat())) if function else (lambda: collect_openalex(config, profiles, window_start.date().isoformat()))))
    if source_config.get("arxiv", {}).get("enabled", True):
        function = injected.get("arxiv")
        source_calls.append(("arxiv", (lambda fn=function: fn(config, profiles, window_start, current)) if function else (lambda: collect_arxiv(config, profiles, window_start, current))))
    if source_config.get("jstage", {}).get("enabled", False):
        function = injected.get("jstage")
        source_calls.append(("jstage", (lambda fn=function: fn(config, profiles, window_start, current)) if function else (lambda: collect_jstage(config, profiles, window_start, current))))
    try:
        def store_records(records: list[Any]) -> None:
            stats["fetched"] += len(records)
            for record in records:
                paper_id, item_status = database.upsert_paper(record, current.isoformat(timespec="seconds"))
                if item_status == "inserted":
                    stats["inserted"] += 1
                    new_ids.append(paper_id)
                elif item_status == "merged":
                    stats["updated"] += 1
                else:
                    stats["duplicates"] += 1

        for source, call in source_calls:
            stats["sources_attempted"] += 1
            source_cooldown = cooldown_until(database, source, current)
            if source_cooldown:
                stats["cooldown_skipped"] += 1
                message = f"{source}: rate-limit cooldown until {source_cooldown.isoformat(timespec='seconds')}"
                errors.append(message)
                database.record_source_run(run_id, source, "cooldown_skipped", error=RuntimeError(message))
                continue
            try:
                records = call()
                database.record_source_run(run_id, source, "completed", len(records))
                stats["sources_succeeded"] += 1
                store_records(records)
                on_success(database, source, current)
            except RateLimited as exc:
                store_records(exc.partial_records)
                field = f"http_{exc.status_code}" if exc.status_code in {403, 406, 429} else "http_429"
                stats[field] += 1
                state = on_rate_limited(
                    database, source, current, exc.retry_after_seconds,
                    source_config.get(source, {}).get("rate_limit", {}),
                )
                message = f"{source}: {exc}; cooldown until {state['cooldown_until']}"
                errors.append(message)
                database.record_source_run(run_id, source, "rate_limited", len(exc.partial_records), exc)
            except Exception as exc:
                database.record_source_run(run_id, source, "failed", error=exc)
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
        if stage >= 2:
            score_ids = list(dict.fromkeys([*new_ids, *database.unscored_paper_ids()]))
            for paper_id in score_ids:
                paper = database.paper(paper_id)
                score = score_paper(paper, profiles)
                database.save_score(score, current.isoformat(timespec="seconds"))
                stats["scored"] += 1
            threshold = float(config.get("scoring", {}).get("digest_threshold", 50))
            limit = int(config.get("digest", {}).get("max_items", 15))
            candidates = database.candidate_rows(list(dict.fromkeys(new_ids)), threshold, limit)
            stats["candidates"] = len(candidates)
        else:
            candidates = []

        should_baseline = baseline or (is_first_run and bool(config.get("collection", {}).get("auto_baseline_on_first_run", True)))
        if should_baseline:
            minimum_sources = max(
                1,
                int(config.get("collection", {}).get("minimum_successful_sources_for_baseline", stats["sources_attempted"])),
            )
            baseline_safe = stats["sources_attempted"] > 0 and stats["sources_succeeded"] >= minimum_sources
            if baseline_safe:
                database.set_state("baseline_completed", current.isoformat(timespec="seconds"), current.isoformat(timespec="seconds"))
                status = "baseline_completed"
            else:
                status = "failed" if not stats["sources_succeeded"] and not stats["fetched"] else "completed_with_errors"
        else:
            if stage >= 3 and candidates:
                ai_stats, ai_errors = review_candidates(database, candidates, config, current)
                stats.update({key: stats.get(key, 0) + value for key, value in ai_stats.items()})
                errors.extend(ai_errors)
                threshold = float(config.get("scoring", {}).get("digest_threshold", 50))
                candidates = database.candidate_rows(list(dict.fromkeys(new_ids)), threshold, int(config.get("digest", {}).get("max_items", 15)))
            if stage >= 4 or force_email:
                window = f"{window_start.astimezone(ZoneInfo('Asia/Tokyo')).date()} ～ {current.astimezone(ZoneInfo('Asia/Tokyo')).date()}"
                subject, body = build_digest(candidates, stats, window, errors)
                key = "research-paper-digest:" + current.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
                queue_digest(database, key, subject, body, candidates, current.isoformat(timespec="seconds"))
                sent, mail_errors = flush_outbox(database)
                stats["emailed"] += sent
                errors.extend(mail_errors)
            status = "completed" if not errors else "completed_with_errors" if stats["sources_succeeded"] else "failed"
        database.finish_run(run_id, status, stats, errors)
        return {"run_id": run_id, "status": status, **stats, "errors": errors}
    except Exception as exc:
        errors.append(f"service: {type(exc).__name__}: {exc}")
        database.finish_run(run_id, "failed", stats, errors)
        return {"run_id": run_id, "status": "failed", **stats, "errors": errors}
