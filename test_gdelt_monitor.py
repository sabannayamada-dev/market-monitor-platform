from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gdelt_monitor.analysis import (
    CompanyAlias, load_candidates_since, load_company_aliases, match_and_score, save_articles,
)
from gdelt_monitor.adaptive import (
    detect_stability, load_state, mark_stability_alert_sent, on_429,
    on_run_without_429, stability_alert_parameters,
)
from gdelt_monitor.ai import review_emergency_alerts, run_emergency_ai_self_test
from gdelt_monitor.collector import NonJsonResponse, QueryTask, RateLimited, build_tasks, fetch_once
from gdelt_monitor.database import NewsDatabase
from gdelt_monitor.emergency import classify_title, ready_alerts, run_emergency_scout
from gdelt_monitor.global_news import rank_toc_records, score_world_headline, select_daily_world_news
from gdelt_monitor.notifications import build_digest, build_emergency_alert, health_label
from gdelt_monitor.ngram_collector import DocumentMatcher, NgramCollection, NgramFile
from gdelt_monitor.service import run_daily_service
from gdelt_news_daily import main as gdelt_cli_main


ROOT = Path(__file__).resolve().parent


class GdeltMonitorTests(unittest.TestCase):
    def _record_stability_history(
        self,
        database: NewsDatabase,
        now: datetime,
        x_values: list[float],
        y_values: list[float],
        z_values: list[float],
    ) -> None:
        count = len(x_values)
        for index, (x, y, z) in enumerate(zip(x_values, y_values, z_values)):
            event_at = now - timedelta(hours=72 * (count - index - 1) / (count - 1))
            database.record_adaptive_event(
                event_at.isoformat(), "http_429" if index == 0 else "run_completed",
                None, x, None, y, None, z, event_at.isoformat(),
                "429観測" if index == 0 else "正常収集",
            )

    def test_stability_is_detected_independently_for_x_y_z(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "state.sqlite3")
            now = datetime(2026, 8, 18, tzinfo=timezone.utc)
            self._record_stability_history(
                database, now,
                [60] * 13,
                [6 + index * 0.25 for index in range(13)],
                [20 + index for index in range(13)],
            )
            result = detect_stability(database, {"window_hours": 72, "minimum_samples": 12}, now)
            self.assertEqual(["x"], result["stable_parameters"])
            self.assertTrue(result["parameters"]["x"]["stable"])
            self.assertFalse(result["parameters"]["y"]["stable"])
            self.assertFalse(result["parameters"]["z"]["stable"])

    def test_stability_alert_is_sent_once_per_parameter_and_band(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "state.sqlite3")
            now = datetime(2026, 8, 18, tzinfo=timezone.utc)
            self._record_stability_history(database, now, [60] * 13, [6] * 13, [20] * 13)
            settings = {"window_hours": 72, "minimum_samples": 12}
            result = detect_stability(database, settings, now)
            self.assertEqual(["x", "y", "z"], stability_alert_parameters(database, result, settings))
            mark_stability_alert_sent(database, result, ["x", "y", "z"], now)
            self.assertEqual([], stability_alert_parameters(database, result, settings))
            result["parameters"]["x"]["baseline"] = 69
            self.assertEqual(["x"], stability_alert_parameters(database, result, settings))

    def _adaptive_settings(self) -> dict[str, float | int]:
        return {
            "initial_interval_minutes": 60,
            "min_interval_minutes": 30,
            "max_interval_minutes": 180,
            "interval_increase_on_429_minutes": 4,
            "interval_decrease_after_stable_minutes": 2,
            "stable_period_hours": 24,
            "stable_successful_runs_required": 6,
            "fixed_cooldown_hours": 1,
            "query_spacing_seconds": 20,
            "min_query_spacing_seconds": 10,
            "max_query_spacing_seconds": 120,
            "query_spacing_increase_on_intra_run_429_seconds": 5,
            "query_spacing_decrease_after_stable_seconds": 1,
        }

    def test_adaptive_429_and_successful_recovery_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            state = load_state(
                NewsDatabase(Path(temporary) / "state.sqlite3"), self._adaptive_settings(), now
            )
            state, _ = on_429(state, self._adaptive_settings(), now, None)
            self.assertEqual(64, state.interval_minutes)
            self.assertEqual(1, state.cooldown_hours)
            self.assertTrue(state.recovery_probe_pending)
            state, _, _ = on_run_without_429(
                state, self._adaptive_settings(), now + timedelta(hours=6), True
            )
            self.assertEqual(1, state.cooldown_hours)
            self.assertFalse(state.recovery_probe_pending)

    def test_adaptive_failed_recovery_probe_increases_x_but_keeps_y_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            state = load_state(
                NewsDatabase(Path(temporary) / "state.sqlite3"), self._adaptive_settings(), now
            )
            state, _ = on_429(state, self._adaptive_settings(), now, None)
            state, _ = on_429(
                state, self._adaptive_settings(), now + timedelta(hours=1), None
            )
            self.assertEqual(68, state.interval_minutes)
            self.assertEqual(1, state.cooldown_hours)

    def test_existing_long_cooldown_is_clamped_to_fixed_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            database = NewsDatabase(Path(temporary) / "state.sqlite3")
            settings = self._adaptive_settings()
            state = load_state(database, settings, now)
            state.cooldown_hours = 11
            state.cooldown_until = (now + timedelta(hours=11)).isoformat()
            state.next_collection_at = state.cooldown_until
            database.set_state(
                "adaptive_control_v1",
                json.dumps(state.__dict__, ensure_ascii=False),
                now.isoformat(),
            )
            migrated = load_state(database, settings, now)
            self.assertEqual(1, migrated.cooldown_hours)
            self.assertEqual(now + timedelta(hours=1), migrated.cooldown_at(now))

    def test_intra_run_429_increases_z_more_than_stable_day_decreases_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            settings = self._adaptive_settings()
            state = load_state(NewsDatabase(Path(temporary) / "state.sqlite3"), settings, now)
            state, _ = on_429(
                state, settings, now, None, intra_run_spacing_exposed=True
            )
            self.assertEqual(25, state.query_spacing_seconds)
            state.recovery_probe_pending = False
            state.stable_window_started_at = now.isoformat()
            state.stable_successful_runs = 5
            state, _, _ = on_run_without_429(
                state, settings, now + timedelta(hours=24), True
            )
            self.assertEqual(24, state.query_spacing_seconds)

    def test_first_query_429_does_not_change_z(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            settings = self._adaptive_settings()
            state = load_state(NewsDatabase(Path(temporary) / "state.sqlite3"), settings, now)
            state, _ = on_429(state, settings, now, None)
            self.assertEqual(20, state.query_spacing_seconds)

    def test_fixed_schedule_429_keeps_two_hours_and_uses_one_hour_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            settings = {
                **self._adaptive_settings(),
                "fixed_interval_minutes": 120,
                "fixed_cooldown_hours": 1,
            }
            state = load_state(NewsDatabase(Path(temporary) / "state.sqlite3"), settings, now)
            state, _ = on_429(
                state, settings, now, retry_after_seconds=11 * 3600,
                intra_run_spacing_exposed=True,
            )
            self.assertEqual(120, state.interval_minutes)
            self.assertEqual(20, state.query_spacing_seconds)
            self.assertEqual(now + timedelta(hours=1), state.cooldown_at(now))
            state, _, event_type = on_run_without_429(
                state, settings, now + timedelta(hours=1), True
            )
            self.assertEqual("fixed_schedule_completed", event_type)
            self.assertEqual(now + timedelta(hours=3), state.next_at(now))

    def test_429_diagnostics_capture_retry_after_content_type_and_preview(self) -> None:
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        error = urllib.error.HTTPError(
            "https://api.gdeltproject.org/test",
            429,
            "Too Many Requests",
            {"Retry-After": "120", "Content-Type": "text/plain"},
            io.BytesIO(b"temporary per-IP request limit"),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(RateLimited) as raised:
            fetch_once(QueryTask("id", "test", "recall", "test", 1), now, now, 1, 5)
        self.assertEqual(120, raised.exception.retry_after_seconds)
        self.assertIn("Content-Type='text/plain'", str(raised.exception))
        self.assertIn("temporary per-IP request limit", str(raised.exception))

    def test_adaptive_stable_day_decreases_interval_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            state = load_state(
                NewsDatabase(Path(temporary) / "state.sqlite3"), self._adaptive_settings(), now
            )
            state.stable_successful_runs = 5
            state, _, _ = on_run_without_429(
                state, self._adaptive_settings(), now + timedelta(hours=24), True
            )
            self.assertEqual(58, state.interval_minutes)
            self.assertEqual(19, state.query_spacing_seconds)
            self.assertEqual(0, state.stable_successful_runs)

    def test_cooldown_skip_is_a_healthy_cli_exit(self) -> None:
        with patch.object(sys, "argv", ["gdelt_news_daily.py"]), patch(
            "gdelt_news_daily.run_daily_service", return_value={"status": "cooldown_skipped"}
        ):
            self.assertEqual(0, gdelt_cli_main())

    def test_config_builds_global_and_japanese_query_sets(self) -> None:
        config = json.loads((ROOT / "gdelt_monitor_config.json").read_text(encoding="utf-8"))
        tasks = build_tasks(config)
        self.assertEqual(8, len(tasks))
        self.assertEqual("重大悪材料", tasks[0].name)
        japanese_tasks = [task for task in tasks if task.name.startswith("日本語・")]
        self.assertEqual(4, len(japanese_tasks))
        self.assertTrue(all("sourcelang:japanese" in task.query for task in japanese_tasks))
        self.assertTrue(all(task.query.isascii() is False for task in japanese_tasks))
        self.assertTrue(all(len(task.query) < 150 for task in japanese_tasks))
        self.assertEqual(
            {task.profile for task in tasks[:4]},
            {task.profile for task in japanese_tasks},
        )

    def test_company_matching_scoring_and_duplicate_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "companies.csv"
            master.write_text(
                "company_id,company_name,ticker,aliases,subsidiaries,target\n"
                "JP7550,株式会社すき家,7550,Sukiya|すき家,,1\n",
                encoding="utf-8-sig",
            )
            database = NewsDatabase(root / "news.sqlite3")
            config = {
                "digest_score_threshold": 40,
                "positive_keywords": [],
                "negative_keywords": ["food poisoning"],
            }
            task = build_tasks(
                {"broad_queries": [{"name": "bad", "query": "food poisoning", "severity": 55}]}
            )[0]
            article = {
                "url": "https://example.com/news?utm_source=x",
                "title": "Sukiya investigates food poisoning report",
                "seendate": "20260815T010000Z",
                "domain": "example.com",
            }
            inserted, duplicates, article_ids = save_articles(database, "r1", task, [article])
            self.assertEqual((1, 0), (inserted, duplicates))
            inserted, duplicates, _ = save_articles(database, "r2", task, [article])
            self.assertEqual((0, 1), (inserted, duplicates))
            matches, scored, candidates = match_and_score(
                database, article_ids, load_company_aliases(master), config
            )
            self.assertEqual(1, matches)
            self.assertEqual(1, scored)
            self.assertGreaterEqual(candidates[0]["score"], 90)
            with database.connection() as connection:
                connection.execute(
                    """INSERT INTO ai_reviews(article_id,provider,model,status,importance,direction,
                       summary,risk,raw_json,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (article_ids[0], "openai", "test", "completed", 95, "negative",
                     "AI summary", "AI risk", "{}"),
                )
            loaded = load_candidates_since(database, "2000-01-01T00:00:00+00:00", 40)
            self.assertEqual("AI summary", loaded[0]["gpt_summary"])

    def _environment(self, root: Path) -> dict[str, str]:
        config = {
            "schema_version": 1,
            "collector": {"lookback_minutes": 90, "request_sleep_seconds": 0},
            "digest_score_threshold": 40,
            "urgent_score_threshold": 90,
            "broad_queries": [{"name": "risk", "query": "recall", "severity": 55}],
            "positive_keywords": [],
            "negative_keywords": ["recall"],
        }
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        master = root / "companies.csv"
        master.write_text(
            "company_id,company_name,ticker,aliases,subsidiaries,target\n"
            "JP1,Example株式会社,0001,Example,,1\n", encoding="utf-8-sig"
        )
        return {
            "GDELT_CONFIG_PATH": str(config_path),
            "GDELT_STATE_DB": str(root / "news.sqlite3"),
            "GDELT_COMPANY_MASTER": str(master),
            "GDELT_EMAIL_ENABLED": "0",
            "GDELT_AI_ENABLED": "0",
        }

    def test_service_records_success_and_empty_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, self._environment(root), clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff", return_value=[]
            ):
                result = run_daily_service()
            self.assertEqual("completed", result["status"])
            self.assertEqual(1, result["successful_queries"])
            self.assertEqual(1, result["empty_responses"])

    def test_service_does_not_contact_gdelt_before_next_due_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 8, 15, tzinfo=timezone.utc)
            with patch.dict(os.environ, self._environment(root), clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff", return_value=[]
            ) as fetch:
                first = run_daily_service(now=now)
                second = run_daily_service(now=now + timedelta(minutes=1))
            self.assertEqual("completed", first["status"])
            self.assertEqual("schedule_skipped", second["status"])
            self.assertEqual(1, fetch.call_count)
            database = NewsDatabase(root / "news.sqlite3")
            self.assertEqual(1, len(database.recent_runs()))

    def test_service_distinguishes_non_json_from_zero_articles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, self._environment(root), clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff",
                side_effect=NonJsonResponse("html response"),
            ):
                result = run_daily_service()
            self.assertEqual("failed", result["status"])
            self.assertEqual(1, result["non_json_responses"])
            self.assertEqual(0, result["empty_responses"])

    def test_rate_limit_opens_persistent_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            with patch.dict(os.environ, environment, clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff",
                side_effect=RateLimited(429, "limited"),
            ):
                first = run_daily_service()
                second = run_daily_service()
            self.assertEqual("stopped_by_rate_limit", first["status"])
            self.assertEqual("cooldown_skipped", second["status"])
            self.assertEqual(0, second["executed_queries"])
            self.assertEqual(1, second["carried_over"])

    def test_second_query_429_is_treated_as_query_spacing_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            config_path = Path(environment["GDELT_CONFIG_PATH"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["adaptive_control"] = self._adaptive_settings()
            config["broad_queries"].append(
                {"name": "risk2", "query": "fraud", "severity": 55}
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(os.environ, environment, clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff",
                side_effect=[[], RateLimited(429, "limited")],
            ), patch("gdelt_monitor.service.time.sleep"):
                result = run_daily_service(now=datetime(2026, 8, 15, tzinfo=timezone.utc))
            self.assertEqual("stopped_by_rate_limit", result["status"])
            self.assertEqual(25, result["adaptive_query_spacing_seconds"])

    def test_stage_three_runs_ai_but_never_sends_email(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            environment["GDELT_OPERATION_STAGE"] = "3"
            article = {
                "url": "https://example.com/recall",
                "title": "Example recall announced",
                "seendate": "20260815T010000Z",
                "domain": "example.com",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "gdelt_monitor.service.fetch_with_backoff", return_value=[article]
            ), patch(
                "gdelt_monitor.service.review_candidates",
                return_value={"gpt_sent": 1, "gpt_passed": 1, "gemini_sent": 0},
            ) as review, patch("gdelt_monitor.service.send_digest") as send:
                result = run_daily_service(force_email=True)
            review.assert_called_once()
            send.assert_not_called()
            self.assertEqual(1, result["gpt_sent"])

    def test_diagnostic_footer_exposes_health_counts(self) -> None:
        stats = {
            "run_count": 2, "planned_queries": 8, "executed_queries": 8,
            "successful_queries": 6, "empty_responses": 2, "non_json_responses": 1,
            "http_403": 0, "http_429": 1, "timeouts": 0, "other_errors": 0,
            "fetched_articles": 100, "inserted_articles": 20, "duplicate_articles": 80,
            "company_matches": 3, "scored_candidates": 2, "gpt_sent": 1,
            "gpt_passed": 1, "gemini_sent": 0, "final_candidates": 2,
            "urgent_candidates": 0, "carried_over": 1,
        }
        health, alerts = health_label(stats)
        self.assertIn("要確認", health)
        self.assertTrue(alerts)
        _, body = build_digest([], stats, "today", ["http_429: risk limited"])
        self.assertIn("空応答: 2", body)
        self.assertIn("GPT送信: 1件", body)
        self.assertIn("未処理・次回持越し: 1件", body)
        self.assertIn("http_429: risk limited", body)

    def test_digest_includes_ai_summary_when_available(self) -> None:
        candidate = {
            "article_id": "a1", "title": "Example material news", "url": "https://example.com/a1",
            "domain": "example.com", "source_language": "ja",
            "seen_date": "20260815T010000Z", "score": 90.0,
            "direction": "negative", "companies": ["Example株式会社"], "reasons": [],
            "gpt_importance": 92.0, "gpt_summary": "業績への影響を確認する必要があります。",
            "gpt_risk": "タイトル情報のみです。", "gemini_importance": None,
            "gemini_summary": "", "gemini_risk": "",
        }
        _, body = build_digest([candidate], {}, "today")
        self.assertIn("AI要約(GPT・重要度92)", body)
        self.assertIn("AI注意点: タイトル情報のみです。", body)
        self.assertIn("原文言語: 日本語", body)

    def test_world_news_prefilter_favors_major_events_and_rejects_entertainment(self) -> None:
        now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        records = [
            {"title": "Major earthquake triggers nationwide emergency", "url": "https://reuters.com/world/a", "lang": "English"},
            {"title": "Celebrity shares new movie preview video", "url": "https://example.com/show", "lang": "English"},
            {"title": "Central bank announces historic interest rate decision", "url": "https://bbc.com/news/b", "lang": "English"},
        ]
        ranked = rank_toc_records(records, "20260907120000", now, {
            "enabled": True, "minimum_rule_score": 25, "candidates_per_file": 6,
        })
        self.assertEqual(2, len(ranked))
        self.assertEqual("災害", ranked[0]["topic"])
        self.assertGreater(score_world_headline(records[0])[0], score_world_headline(records[1])[0])

    def test_world_news_prefilter_rejects_article_body_and_untrusted_source(self) -> None:
        now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        records = [
            {"title": "Major war changes global economy " + ("detail " * 80), "url": "https://reuters.com/world/a"},
            {"title": "Government signs major international peace deal", "url": "https://untrusted.example/world/b"},
            {"title": "Central bank announces historic interest rate decision", "url": "https://bbc.com/news/c"},
        ]
        ranked = rank_toc_records(records, "20260907120000", now, {
            "enabled": True,
            "minimum_rule_score": 20,
            "candidates_per_file": 6,
            "maximum_title_characters": 240,
            "require_trusted_source": True,
        })
        self.assertEqual(["bbc.com"], [item["domain"] for item in ranked])

    def test_world_news_fallback_deduplicates_same_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
            ranked = rank_toc_records([
                {"title": "Ukraine war briefing: nine killed in Russian strikes on Kyiv", "url": "https://bbc.com/a"},
                {"title": "Russia launches drone strike on Ukraine; civilians killed in Kyiv", "url": "https://reuters.com/b"},
                {"title": "Major earthquake triggers nationwide emergency", "url": "https://apnews.com/c"},
            ], "20260913120000", now, {
                "enabled": True, "minimum_rule_score": 20, "candidates_per_file": 6,
            })
            database.record_global_news_candidates(ranked)
            selected = select_daily_world_news(
                database,
                {"enabled": True, "item_count": 2, "ai_enabled": False},
                "2026-09-13T00:00:00+00:00",
                "2026-09-13",
            )
        self.assertEqual(2, len(selected))
        self.assertTrue(any(item["topic"] == "災害" for item in selected))

    def test_world_news_selection_filters_legacy_untrusted_and_long_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
            ranked = rank_toc_records([
                {"title": "Major war changes global economy " + ("detail " * 80), "url": "https://untrusted.example/a"},
                {"title": "Government signs major international peace deal", "url": "https://untrusted.example/b"},
                {"title": "Major earthquake triggers nationwide emergency", "url": "https://reuters.com/c"},
                {"title": "Central bank announces historic interest rate decision", "url": "https://bbc.com/d"},
            ], "20260914120000", now, {
                "enabled": True, "minimum_rule_score": 20, "candidates_per_file": 6,
            })
            database.record_global_news_candidates(ranked)
            selected = select_daily_world_news(database, {
                "enabled": True, "item_count": 2, "ai_enabled": False,
                "maximum_title_characters": 240, "require_trusted_source": True,
            }, "2026-09-14T00:00:00+00:00", "2026-09-14")
        self.assertEqual({"reuters.com", "bbc.com"}, {item["domain"] for item in selected})

    def test_digest_adds_two_world_news_items_independent_of_stock_candidates(self) -> None:
        world = [
            {
                "candidate_id": f"w{index}", "title": f"World event {index}",
                "japanese_title": f"世界ニュース{index}", "summary": "世界的な出来事です。",
                "reason": "広い地域に影響", "importance": 90 - index,
                "topic": "国際", "domain": "reuters.com", "seen_date": "20260907T010000Z",
                "url": f"https://reuters.com/{index}",
            }
            for index in (1, 2)
        ]
        subject, body = build_digest([], {}, "today", world_news=world)
        self.assertIn("世界2件", subject)
        self.assertIn("【今日の世界の重要ニュース: 2件】", body)
        self.assertIn("世界ニュース1", body)
        self.assertIn("株価との関連性は選定条件に含めていません", body)

    def test_world_news_ai_selects_two_and_reuses_daily_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            ranked = rank_toc_records([
                {"title": "Major earthquake triggers nationwide emergency", "url": "https://reuters.com/a"},
                {"title": "Central bank announces historic interest rate decision", "url": "https://bbc.com/b"},
                {"title": "Government signs major international peace deal", "url": "https://apnews.com/c"},
            ], "20260907120000", now, {"enabled": True, "minimum_rule_score": 20, "candidates_per_file": 6})
            database.record_global_news_candidates(ranked)
            choices = ranked[:2]
            response = {"choices": [{"message": {"content": json.dumps({"items": [
                {"candidate_id": choices[0]["candidate_id"], "japanese_title": "大地震", "summary": "広域災害", "importance": 95, "reason": "多数に影響"},
                {"candidate_id": choices[1]["candidate_id"], "japanese_title": "重要決定", "summary": "国際的影響", "importance": 90, "reason": "広域に影響"},
            ]})}}]}
            settings = {"enabled": True, "item_count": 2, "max_ai_candidates": 30, "ai_enabled": True}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
                "gdelt_monitor.global_news._post_json", return_value=response
            ) as post:
                first = select_daily_world_news(database, settings, "2026-09-07T00:00:00+00:00", "2026-09-07")
                second = select_daily_world_news(database, settings, "2026-09-07T00:00:00+00:00", "2026-09-07")
            self.assertEqual(2, len(first))
            self.assertEqual("ai", first[0]["selection_method"])
            self.assertEqual(first, second)
            post.assert_called_once()

    def test_world_news_ai_missing_display_fields_falls_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            ranked = rank_toc_records([
                {"title": "Major earthquake triggers nationwide emergency", "url": "https://reuters.com/a"},
                {"title": "Government signs major international peace deal", "url": "https://apnews.com/c"},
            ], "20260907120000", now, {"enabled": True, "minimum_rule_score": 20, "candidates_per_file": 6})
            database.record_global_news_candidates(ranked)
            response = {"choices": [{"message": {"content": json.dumps({"items": [
                {"candidate_id": item["candidate_id"], "japanese_title": item["title"], "summary": "", "importance": 0, "reason": "重大"}
                for item in ranked
            ]})}}]}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
                "gdelt_monitor.global_news._post_json", return_value=response
            ):
                selected = select_daily_world_news(
                    database, {"enabled": True}, "2026-09-07T00:00:00+00:00", "2026-09-07"
                )
            self.assertEqual("", selected[0]["japanese_title"])
            self.assertGreater(selected[0]["importance"], 0)


    def test_ngram_matcher_requires_the_full_safe_company_alias(self) -> None:
        task = QueryTask("q1", "positive", "approval", "positive", 40)
        aliases = [
            CompanyAlias("JP1", "HMT", "6090", "Human Metabolome Technologies Inc.", "humanmetabolometechnologies"),
            CompanyAlias("JP2", "SCREEN", "7735", "SCREEN HOLDINGS CO LTD", "screen"),
        ]
        matcher = DocumentMatcher([task], aliases)
        query_hits: set[str] = set()
        company_hits: dict[str, CompanyAlias] = {}
        matcher.scan_line("human rights approval was announced", query_hits, company_hits)
        matcher.scan_line("the screen approval process continued", query_hits, company_hits)
        self.assertEqual({"q1"}, query_hits)
        self.assertEqual({}, company_hits)
        matcher.scan_line("Human Metabolome Technologies received approval", query_hits, company_hits)
        self.assertEqual({"JP1"}, set(company_hits))

    def test_ngram_matcher_accepts_japanese_material_keyword_and_company(self) -> None:
        task = QueryTask(
            "q1", "positive", '(partnership OR "業務提携" OR "買収")', "positive", 40
        )
        alias = CompanyAlias("JP1", "第一三共株式会社", "4568", "第一三共株式会社", "master")
        matcher = DocumentMatcher([task], [alias])
        queries, companies = matcher.scan_title("第一三共が国内企業との業務提携を発表")
        self.assertEqual({"q1"}, queries)
        self.assertEqual({"JP1"}, set(companies))
        short_queries, _ = matcher.scan_title("第一三共が海外企業を買収")
        self.assertEqual({"q1"}, short_queries)

    def test_ngram_matcher_applies_source_language_qualifier_locally(self) -> None:
        tasks = [
            QueryTask("global", "global", "業務提携", "positive", 40),
            QueryTask(
                "japanese", "japanese", "業務提携 sourcelang:japanese", "positive", 40
            ),
        ]
        matcher = DocumentMatcher(tasks, [])
        query_hits, _ = matcher.scan_title("業務提携を発表")
        self.assertEqual({"global", "japanese"}, query_hits)
        self.assertEqual(
            {"global", "japanese"}, matcher.filter_queries_by_language(query_hits, "ja")
        )
        self.assertEqual(
            {"global"}, matcher.filter_queries_by_language(query_hits, "English")
        )

    def test_ngram_discovery_does_not_repeat_head_for_processed_minutes(self) -> None:
        from gdelt_monitor.ngram_collector import discover_files

        start = datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
        with patch("gdelt_monitor.ngram_collector.urllib.request.urlopen") as opened:
            response = opened.return_value.__enter__.return_value
            response.headers = {"Content-Length": "10"}
            files, requests = discover_files(
                start,
                start + timedelta(minutes=2),
                publication_lag_minutes=0,
                skip_stamps={"20260816130100"},
            )
        self.assertEqual(2, requests)
        self.assertEqual(["20260816130000", "20260816130200"], [item.stamp for item in files])
        self.assertEqual(2, opened.call_count)

    def test_ngram_embedded_company_match_and_title_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            task = QueryTask("q1", "positive", "approval", "positive", 40)
            article = {
                "url": "https://first.example/story",
                "title": "Daiichi Sankyo vaccine report",
                "seendate": "20260816T130000Z",
                "_dedupe_by_title": True,
                "_company_matches": [{
                    "company_id": "JP1", "company_name": "第一三共株式会社",
                    "ticker": "4568", "alias": "Daiichi Sankyo",
                }],
            }
            inserted, duplicates, ids = save_articles(database, "run1", task, [article])
            second = {**article, "url": "https://second.example/the-same-story"}
            inserted2, duplicates2, _ = save_articles(database, "run2", task, [second])
            self.assertEqual((1, 0), (inserted, duplicates))
            self.assertEqual((0, 1), (inserted2, duplicates2))
            matches, scored, candidates = match_and_score(
                database, ids, [], {"digest_score_threshold": 40}
            )
            self.assertEqual(1, matches)
            self.assertEqual(1, scored)
            self.assertEqual(["第一三共株式会社"], candidates[0]["companies"])

    def test_market_cap_snapshot_adds_english_company_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "companies.csv"
            master.write_text(
                "company_id,company_name,ticker,aliases,subsidiaries,target\n"
                "JP1,第一三共株式会社,4568,,,1\n", encoding="utf-8-sig"
            )
            snapshot = root / "snapshot.csv"
            snapshot.write_text(
                "入力値,銘柄,名称\n4568,4568.T,Daiichi Sankyo Company Limited\n",
                encoding="utf-8-sig",
            )
            aliases = load_company_aliases(master, snapshot)
            self.assertIn("Daiichi Sankyo Company Limited", [alias.alias for alias in aliases])

    def test_web_ngram_service_records_processed_files_without_doc_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            environment["GDELT_OPERATION_STAGE"] = "2"
            config_path = Path(environment["GDELT_CONFIG_PATH"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["collector"]["source"] = "web_ngrams"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            task = build_tasks(config)[0]
            article = {
                "url": "https://example.com/story", "title": "Example approval",
                "seendate": "20260816T130000Z", "_dedupe_by_title": True,
                "_company_matches": [{
                    "company_id": "JP1", "company_name": "Example Company",
                    "ticker": "0001", "alias": "Example",
                }],
            }
            collection = NgramCollection(
                articles_by_query={task.query_id: [article]},
                discovered_files=[NgramFile("20260816130000", "toc", "ngram")],
                processed_files=[{"stamp": "20260816130000", "matched_documents": 1, "bytes": 123}],
                failed_files=[], head_requests=91, downloaded_bytes=123,
            )
            with patch.dict(os.environ, environment, clear=False), patch(
                "gdelt_monitor.service.collect_web_ngrams", return_value=collection
            ), patch("gdelt_monitor.service.fetch_with_backoff") as doc_api:
                result = run_daily_service(now=datetime(2026, 8, 16, 14, tzinfo=timezone.utc))
            doc_api.assert_not_called()
            self.assertEqual("completed", result["status"])
            self.assertEqual(1, result["ngram_processed_files"])
            database = NewsDatabase(root / "news.sqlite3")
            self.assertEqual(
                {"20260816130000"}, database.processed_ngram_stamps_since("20260816")
            )


    def test_fixed_two_hour_schedule_migrates_existing_interval_without_unpausing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "state.sqlite3")
            now = datetime(2026, 8, 16, tzinfo=timezone.utc)
            paused = "2099-01-01T00:00:00+00:00"
            database.set_state(
                "adaptive_control_v1",
                json.dumps({
                    "interval_minutes": 84.0,
                    "cooldown_hours": 1.0,
                    "query_spacing_seconds": 20.0,
                    "next_collection_at": paused,
                    "cooldown_until": paused,
                    "recovery_probe_pending": True,
                    "stable_window_started_at": now.isoformat(),
                    "stable_successful_runs": 0,
                    "last_429_at": now.isoformat(),
                    "last_change_reason": "paused",
                }),
                now.isoformat(),
            )
            settings = {**self._adaptive_settings(), "fixed_interval_minutes": 120}
            state = load_state(database, settings, now)
            self.assertEqual(120, state.interval_minutes)
            self.assertEqual(paused, state.next_collection_at)

    def test_emergency_classifier_distinguishes_actual_and_hypothetical_closure(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        actual = classify_title(
            {"title": "Iran closes Strait of Hormuz to shipping", "url": "https://reuters.com/a"},
            "20260816140000",
            now,
        )
        hypothetical = classify_title(
            {"title": "Iran may close Strait of Hormuz", "url": "https://example.com/b"},
            "20260816140000",
            now,
        )
        self.assertEqual("closed", actual[0]["event_state"])
        self.assertEqual([], hypothetical)

    def test_emergency_classifier_detects_reopening_and_declaration_of_war(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        reopened = classify_title(
            {"title": "Strait of Hormuz reopens to shipping", "url": "https://example.com/a"},
            "20260816140000",
            now,
        )
        declared = classify_title(
            {"title": "Freedonia formally declares war on Sylvania", "url": "https://example.com/b"},
            "20260816140000",
            now,
        )
        warning = classify_title(
            {"title": "Freedonia warns it may declare war", "url": "https://example.com/c"},
            "20260816140000",
            now,
        )
        self.assertEqual("reopened", reopened[0]["event_state"])
        self.assertEqual("declaration_of_war", declared[0]["event_type"])
        self.assertEqual([], warning)

    def test_emergency_alert_requires_trusted_or_independent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
            untrusted = classify_title(
                {"title": "Strait of Hormuz closed to shipping", "url": "https://one.example/a"},
                "20260816140000",
                now,
            )[0]
            database.record_emergency_candidate(untrusted)
            settings = {
                "trusted_domains": ["reuters.com"],
                "minimum_independent_domains": 2,
                "minimum_independent_titles": 2,
            }
            self.assertEqual([], ready_alerts(database, settings, now))
            independent = classify_title(
                {"title": "Shipping halted through Strait of Hormuz", "url": "https://two.example/b"},
                "20260816140100",
                now + timedelta(minutes=1),
            )[0]
            database.record_emergency_candidate(independent)
            alerts = ready_alerts(database, settings, now + timedelta(minutes=1))
            self.assertEqual(1, len(alerts))
            self.assertEqual("independent_sources", alerts[0]["confirmation"])
            database.record_emergency_alert(alerts[0], (now + timedelta(minutes=1)).isoformat())
            self.assertEqual([], ready_alerts(database, settings, now + timedelta(minutes=2)))

    def test_single_trusted_source_can_trigger_emergency_alert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
            candidate = classify_title(
                {"title": "Strait of Hormuz closed to shipping", "url": "https://www.reuters.com/a"},
                "20260816140000",
                now,
            )[0]
            database.record_emergency_candidate(candidate)
            alerts = ready_alerts(database, {"trusted_domains": ["reuters.com"]}, now)
            self.assertEqual("trusted_source", alerts[0]["confirmation"])
            subject, body = build_emergency_alert(alerts[0])
            self.assertIn("GDELT", subject)
            self.assertIn("reuters.com", body)

    def test_reopening_discussion_is_not_treated_as_completed_reopening(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        candidates = classify_title(
            {
                "title": "Iran Says Reopening Strait Of Hormuz Is A Separate Issue",
                "url": "https://example.com/discussion",
            },
            "20260816140000",
            now,
        )
        self.assertEqual([], candidates)

    def test_extended_emergency_categories_detect_confirmed_headlines(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        examples = {
            "nuclear_weapon_event": "Country conducts nuclear test, government confirms",
            "nuclear_facility_attack": "Nuclear power plant struck and severely damaged",
            "head_of_state": "President assassinated, officials confirm death",
            "coup": "Military coup seizes power and government is overthrown",
            "ceasefire": "Regional ceasefire signed and takes effect",
            "major_transport_hub": "Major port fully closed after all operations suspended",
            "financial_sanctions": "Government imposes sweeping financial sanctions and cuts banks off SWIFT",
            "systemic_financial_event": "Government imposes capital controls with immediate effect",
            "major_disaster": "Massive earthquake causes widespread outage and infrastructure failure",
            "major_exchange": "NYSE halts all trading in market-wide halt",
            "critical_facility": "Major LNG terminal shut down indefinitely",
        }
        for expected, title in examples.items():
            with self.subTest(title=title):
                candidates = classify_title(
                    {"title": title, "url": "https://reuters.com/test"},
                    "20260816140000",
                    now,
                )
                self.assertIn(expected, {item["event_type"] for item in candidates})

    def test_extended_emergency_rules_reject_warning_or_proposal(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        for title in (
            "Government may impose capital controls",
            "Proposal to exclude banks from SWIFT",
            "Military reportedly plans coup to seize power",
            "Talks on ceasefire taking effect",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    [],
                    classify_title(
                        {"title": title, "url": "https://example.com/test"},
                        "20260816140000",
                        now,
                    ),
                )

    def test_head_of_state_rule_rejects_former_leaders_and_other_targets(self) -> None:
        now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
        rejected = (
            "Former president dies aged 92",
            "President confirms minister arrested in corruption investigation",
            "President's aide detained after protest",
        )
        for title in rejected:
            with self.subTest(title=title):
                candidates = classify_title(
                    {"title": title, "url": "https://reuters.com/test"},
                    "20260816140000",
                    now,
                )
                self.assertNotIn("head_of_state", {item["event_type"] for item in candidates})
        confirmed = classify_title(
            {"title": "President assassinated, officials confirm death", "url": "https://reuters.com/test"},
            "20260816140000",
            now,
        )
        self.assertIn("head_of_state", {item["event_type"] for item in confirmed})

    def test_emergency_gpt_review_is_fail_closed_cached_and_budgeted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
            alert = {
                "alert_id": "a1",
                "event_key": "major_exchange:global",
                "event_type": "major_exchange",
                "event_state": "trading_halted",
                "entity": "major exchange halt",
                "confirmation": "trusted_source",
                "evidence": [{
                    "title": "NYSE halts all trading",
                    "title_key": "t1",
                    "domain": "reuters.com",
                    "url": "https://reuters.com/a",
                    "seen_date": "20260816T140000Z",
                }],
            }
            response = {
                "choices": [{"message": {"content": json.dumps({
                    "event_confirmed": True,
                    "same_event": True,
                    "market_impact": 96,
                    "urgency": 94,
                    "is_speculation": False,
                    "event_category": "major_exchange",
                    "affected_assets": ["equities"],
                    "japanese_summary": "NYSEが全取引を停止",
                    "reason": "発生済みの全面停止",
                })}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 100},
            }
            settings = {"ai_review": {"enabled": True, "monthly_budget_usd": 7}}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
                "gdelt_monitor.ai._post_json", return_value=response
            ) as post:
                approved, stats, errors = review_emergency_alerts(
                    database, [alert], settings, now
                )
                cached, cached_stats, cached_errors = review_emergency_alerts(
                    database, [alert], settings, now + timedelta(minutes=1)
                )
            self.assertEqual(1, len(approved))
            self.assertEqual(1, stats["sent"])
            self.assertEqual([], errors)
            self.assertEqual(1, len(cached))
            self.assertEqual(1, cached_stats["cached"])
            self.assertEqual([], cached_errors)
            self.assertEqual(1, post.call_count)
            sent_prompt = post.call_args.args[1]["messages"][0]["content"]
            self.assertIn("0-100 scale", sent_prompt)
            self.assertIn("85=minimum", sent_prompt)

            with patch.dict(os.environ, {}, clear=True):
                held, _, held_errors = review_emergency_alerts(
                    database,
                    [{**alert, "event_key": "coup:global", "evidence": [{**alert["evidence"][0], "title_key": "t2"}]}],
                    settings,
                    now,
                )
            self.assertEqual([], held)
            self.assertIn("OPENAI_API_KEY", held_errors[0])

    def test_emergency_ai_self_test_requires_negative_and_positive_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)

            def response(review: dict[str, object]) -> dict[str, object]:
                return {
                    "choices": [{"message": {"content": json.dumps(review)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }

            negative = response({
                "event_confirmed": False,
                "same_event": True,
                "market_impact": 0,
                "urgency": 0,
                "is_speculation": True,
                "event_category": "other",
                "affected_assets": [],
                "japanese_summary": "接続試験",
                "reason": "ニュースではない",
            })
            positive = response({
                "event_confirmed": True,
                "same_event": True,
                "market_impact": 95,
                "urgency": 96,
                "is_speculation": False,
                "event_category": "major_exchange",
                "affected_assets": ["equities"],
                "japanese_summary": "NYSEが全取引を停止",
                "reason": "発生済みの全面停止",
            })
            settings = {"ai_review": {"enabled": True, "self_test_on_startup": True}}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
                "gdelt_monitor.ai._post_json", side_effect=[negative, positive]
            ) as post:
                self.assertEqual("completed", run_emergency_ai_self_test(database, settings, now))
                self.assertEqual("completed", run_emergency_ai_self_test(database, settings, now))
            self.assertEqual(2, post.call_count)
            state = json.loads(database.get_state("emergency_ai_self_test_v3") or "{}")
            self.assertTrue(state["negative_safe"])
            self.assertTrue(state["positive_safe"])

    def test_unhealthy_emergency_ai_self_test_holds_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = NewsDatabase(Path(temporary) / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
            database.set_state(
                "emergency_ai_self_test_v3",
                json.dumps({"status": "unexpected_result"}),
                now.isoformat(),
            )
            settings = {"ai_review": {"enabled": True}}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                approved, _, errors = review_emergency_alerts(
                    database,
                    [{
                        "event_key": "major_exchange:global",
                        "event_type": "major_exchange",
                        "event_state": "trading_halted",
                        "evidence": [],
                    }],
                    settings,
                    now,
                )
            self.assertEqual([], approved)
            self.assertIn("not healthy", errors[0])

    def test_document_matcher_checks_company_and_keyword_across_one_document(self) -> None:
        task = QueryTask("q1", "positive", "approval", "positive", 40)
        alias = CompanyAlias(
            "JP1", "Daiichi Sankyo", "4568", "Daiichi Sankyo Company Limited", "daiichisankyo"
        )
        matcher = DocumentMatcher([task], [alias])
        queries, companies = matcher.scan_document(
            ["Daiichi Sankyo announced trial results", "the medicine received approval"]
        )
        self.assertEqual({"q1"}, queries)
        self.assertEqual({"JP1"}, set(companies))

    def test_document_matcher_can_require_company_and_keyword_in_title(self) -> None:
        task = QueryTask("q1", "positive", "approval", "positive", 40)
        alias = CompanyAlias("JP1", "Sony", "6758", "Sony Group Corporation", "sony")
        matcher = DocumentMatcher([task], [alias])
        queries, companies = matcher.scan_title("Sony wins regulatory approval")
        self.assertEqual({"q1"}, queries)
        self.assertEqual({"JP1"}, set(companies))

        queries, companies = matcher.scan_title("Regulatory approval was granted")
        self.assertEqual({"q1"}, queries)
        self.assertEqual({}, companies)

    def test_ambiguous_company_aliases_are_not_used(self) -> None:
        task = QueryTask("q1", "risk", "military strike", "risk", 55)
        aliases = [
            CompanyAlias("JP1", "Canon", "7751", "Canon Inc.", "canon"),
            CompanyAlias("JP2", "Subaru", "7270", "SUBARU CORPORATION", "subaru"),
            CompanyAlias("JP3", "Neural", "4056", "Neural Group Inc.", "neural"),
            CompanyAlias("JP4", "FIG", "4392", "Future Innovation Group, Inc.", "fig"),
        ]
        matcher = DocumentMatcher([task], aliases)
        queries, companies = matcher.scan_document(
            ["the canon describes a military strike", "a neural group studied the Subaru character", "future innovation group"]
        )
        self.assertEqual({"q1"}, queries)
        self.assertEqual({}, companies)

    def test_emergency_scout_runs_every_fifteen_minutes_and_reuses_toc_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = NewsDatabase(root / "news.sqlite3")
            now = datetime(2026, 8, 16, 14, tzinfo=timezone.utc)
            file = NgramFile("20260816135500", "toc", "ngram")
            toc = {
                1: {
                    "ID": 1,
                    "title": "Strait of Hormuz closed to shipping",
                    "url": "https://www.reuters.com/a",
                    "lang": "en",
                }
            }
            settings = {"enabled": True, "interval_minutes": 15, "trusted_domains": ["reuters.com"]}
            with patch(
                "gdelt_monitor.emergency.discover_files", return_value=([file], 26)
            ) as discover, patch(
                "gdelt_monitor.emergency.load_toc", return_value=(toc, 200)
            ) as load:
                first = run_emergency_scout(database, settings, {}, now, root / "toc_cache")
                second = run_emergency_scout(
                    database, settings, {}, now + timedelta(minutes=1), root / "toc_cache"
                )
            self.assertEqual("completed", first.status)
            self.assertEqual(1, first.scanned_files)
            self.assertEqual(1, len(first.ready_alerts))
            self.assertEqual("schedule_skipped", second.status)
            self.assertEqual(1, discover.call_count)
            self.assertEqual(1, load.call_count)
            with database.connection() as connection:
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM emergency_scout_runs").fetchone()[0]
                )

    def test_japanese_wrapped_short_ascii_company_names_do_not_match_common_text(self) -> None:
        task = QueryTask("q1", "risk", "investigation", "risk", 55)
        aliases = [
            CompanyAlias("JP1", "SCREEN", "7735", "株式会社ＳＣＲＥＥＮホールディングス", "screen"),
            CompanyAlias("JP2", "TOWA", "6315", "ＴＯＷＡ株式会社", "towa"),
            CompanyAlias("JP3", "NANO", "4571", "ＮＡＮＯホールディングス株式会社", "nano"),
        ]
        matcher = DocumentMatcher([task], aliases)
        queries, companies = matcher.scan_document(
            ["an investigation was shown on the screen", "a nanotechnology report followed"]
        )
        self.assertEqual({"q1"}, queries)
        self.assertEqual({}, companies)


if __name__ == "__main__":
    unittest.main()
