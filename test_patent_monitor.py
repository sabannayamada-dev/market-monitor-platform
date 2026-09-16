from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import gzip
import io
import json
import sqlite3
import sys
import unittest
import urllib.error
from datetime import date
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from patent_monitor.pipeline import (
    Company,
    CompanyMatcher,
    EPOOPSProvider,
    GeminiReviewer,
    MatchResult,
    OpenAIReviewer,
    PatentPipeline,
    PatentDatabase,
    PipelineConfig,
    PatentRecord,
    ScoredPatent,
    consolidate_families,
    materiality_score,
    mock_patents,
    normalize_name,
    read_companies,
    stable_percentile,
    technology_score,
)
from patent_monitor.secure_store import delete_credentials, load_credentials, save_credentials
from patent_monitor.notifications import (
    DIGEST_CHANNEL,
    URGENT_CHANNEL,
    mark_notified,
    pending_digest_rows,
    qualifies_as_urgent,
    register_digest_candidate,
    send_patent_email,
    urgent_budget_available,
)
from patent_monitor.market_feedback import (
    MarketFeedbackService,
    normalize_publication_date,
)
from patent_monitor_daily import company_search_blocks_postprocess
from collect_patent_backfill_forever import (
    _adaptive_rate_after_stable_window,
    _adaptive_rate_after_throttle,
    _adaptive_rate_initial,
    _append_retry_rows,
    _archive_search_quality,
    _backward_chunks,
    _clear_window_staging,
    _persist_normalized_records,
    _record_company_skip,
    _record_window,
    _save_raw_epo_payload,
    _stage_company_records,
    _session_days,
    _window_area_kind,
    _window_was_interrupted,
    init_collector_db,
    load_collector_state,
    save_collector_state,
    save_minimal_patents,
)
from patent_monitor_tool import should_log_collector_progress


class PatentMonitorTests(unittest.TestCase):
    def test_production_time_budget_finishes_well_before_systemd_timeout(self):
        config = PipelineConfig.from_json(Path(__file__).resolve().parent / "patent_monitor_config.json")
        self.assertEqual(90.0, config.company_search_time_guard_minutes)
        self.assertLessEqual(
            config.company_search_time_guard_minutes + config.postprocess_time_guard_minutes,
            180.0,
        )

    def test_company_time_box_does_not_block_reserved_postprocess_phase(self):
        self.assertFalse(company_search_blocks_postprocess({"stopped_by_deadline": True}))
        self.assertTrue(company_search_blocks_postprocess({"stopped_by_request": True}))
        self.assertTrue(company_search_blocks_postprocess({"stopped_by_throttle": True}))

    def test_market_feedback_normalizes_compact_publication_date_and_rejects_empty_prices(self):
        self.assertEqual(normalize_publication_date("20260714"), "2026-07-14")
        self.assertEqual(normalize_publication_date("2026-07-14"), "2026-07-14")
        self.assertEqual(normalize_publication_date("invalid"), "")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "history.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE patents (
                    patent_id TEXT PRIMARY KEY, company_id TEXT, publication_date TEXT
                );
                CREATE TABLE evaluations (patent_id TEXT, created_at TEXT);
                INSERT INTO patents VALUES ('JP1', 'C1', '20260714');
                INSERT INTO evaluations VALUES ('JP1', '2026-08-16T00:00:00');
                """
            )
            connection.commit()
            connection.close()
            download = Mock(return_value=SimpleNamespace(empty=True))
            fake_yfinance = SimpleNamespace(download=download)
            company = SimpleNamespace(company_id="C1", ticker="7203.T")
            with patch.dict(sys.modules, {"yfinance": fake_yfinance}):
                with self.assertRaisesRegex(RuntimeError, "株価取得失敗"):
                    MarketFeedbackService(database, [company], root / "out").run()
            self.assertEqual(download.call_args.kwargs["start"], "2026-07-14")

    def test_epo_expected_missing_detail_is_warning_not_error(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Complete record</invention-title>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        abstract = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><abstract lang="en">Abstract.</abstract>
          </exchange-document></exchange-documents></world-patent-data>'''
        record = PatentRecord(
            source="epo_ops", publication_number="EP123A1", applicants=["FUJITSU LTD"]
        )
        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_published_data", side_effect=[biblio, abstract]),
            patch.object(
                provider,
                "_family_data",
                side_effect=RuntimeError("HTTP 404 Not Found / SERVER.EntityNotFound"),
            ),
        ):
            summary = provider.enrich_records(
                [record],
                [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])],
                enrich_claims=False,
                enrich_forward_citations=False,
            )
        self.assertEqual(summary["error_count"], 0)
        self.assertEqual(summary["warning_count"], 1)
        self.assertEqual(summary["warning_kind_counts"], {"family_legal.not_found": 1})
        self.assertEqual(record.raw["epo_detail_warnings"], ["family_legal.not_found"])

    def test_notification_outbox_retains_rows_until_digest_delivery(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite3"
            row = {
                "publication_number": "JP2026000001A",
                "company_name": "テスト株式会社",
                "gemini_decision": "important",
                "gemini_summary": "重要候補",
            }
            self.assertTrue(register_digest_candidate(database, row, "run-1"))
            pending = pending_digest_rows(database)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["company_name"], "テスト株式会社")
            mark_notified(database, pending, "run-1", "test@example.com", DIGEST_CHANNEL)
            self.assertEqual(pending_digest_rows(database), [])

    def test_ai_review_cache_requires_matching_model_prompt_and_input(self):
        with TemporaryDirectory() as directory:
            database = PatentDatabase(Path(directory) / "history.sqlite3")
            result = {"decision": "important", "importance_score": 91}
            database.store_ai_review("openai", "JP1", "gpt-test", "hash-1", result)
            self.assertEqual(
                database.cached_ai_review("openai", "JP1", "gpt-test", "hash-1"), result
            )
            self.assertIsNone(
                database.cached_ai_review("openai", "JP1", "gpt-other", "hash-1")
            )
            self.assertIsNone(
                database.cached_ai_review("openai", "JP1", "gpt-test", "hash-2")
            )
            database.close()

    def test_urgent_notification_budget_never_exceeds_one_percent(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite3"
            for index in range(99):
                register_digest_candidate(
                    database,
                    {
                        "publication_number": f"JP{index:06d}",
                        "gemini_decision": "important",
                    },
                    "run-test",
                )
            available, summary = urgent_budget_available(database, 0.01)
            self.assertFalse(available)
            self.assertEqual(summary["allowed_urgent_count"], 0)
            candidate = {
                "publication_number": "JP999999",
                "gemini_decision": "urgent",
                "_patent_id": "JP999999",
                "_decision": "urgent",
            }
            register_digest_candidate(database, candidate, "run-test")
            available, summary = urgent_budget_available(database, 0.01)
            self.assertTrue(available)
            self.assertEqual(summary["allowed_urgent_count"], 1)
            mark_notified(database, [candidate], "run-test", "test@example.com", URGENT_CHANNEL)
            self.assertFalse(urgent_budget_available(database, 0.01)[0])

    def test_urgent_notification_requires_strict_gemini_scores(self):
        config = PipelineConfig()
        row = {
            "gemini_decision": "urgent",
            "gemini_importance": 99,
            "gemini_short_term_market_impact": 97,
            "gemini_materiality": 96,
            "company_percentile": 99.5,
        }
        self.assertTrue(qualifies_as_urgent(row, config)[0])
        row["gemini_materiality"] = 94
        self.assertFalse(qualifies_as_urgent(row, config)[0])

    def test_openai_quota_error_has_actionable_message(self):
        exc = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            429,
            "Too Many Requests",
            Message(),
            io.BytesIO(json.dumps({"error": {"code": "insufficient_quota"}}).encode("utf-8")),
        )
        message = str(OpenAIReviewer._http_error(exc))
        self.assertIn("APIの利用枠", message)
        self.assertIn("API課金", message)

    def test_gpt_escalation_uses_scores_and_decision(self):
        config = PipelineConfig(gemini_escalation_threshold=85)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(config, [], root / "history.sqlite3", root / "out")
            self.assertEqual(
                pipeline._should_escalate_to_gemini({"decision": "watch", "importance_score": 84}),
                (False, "GPT一次審査で再審査不要"),
            )
            escalated, reason = pipeline._should_escalate_to_gemini(
                {"decision": "watch", "materiality_score": 91}
            )
            self.assertTrue(escalated)
            self.assertIn("材料性", reason)
            self.assertTrue(pipeline._should_escalate_to_gemini({"decision": "urgent"})[0])
            pipeline.database.close()

    def test_openai_strict_schema_disallows_extra_properties(self):
        schema = OpenAIReviewer._strict_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), set(schema["required"]))

    def test_high_gpt_score_is_reexamined_by_gemini(self):
        result_template = {
            "importance_score": 92,
            "materiality_score": 91,
            "short_term_market_impact_score": 88,
            "long_term_business_value_score": 91,
            "novelty_score": 86,
            "decision": "important",
            "technology_summary": "test",
            "business_impact": "test",
            "market_impact_reason": "test",
            "expected_time_horizon": "test",
            "key_evidence": ["test"],
            "risks_and_uncertainty": ["test"],
            "email_summary": "test",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            patent = next(item for item in mock_patents() if item.publication_number == "JP7301490B2")
            company = Company("JP5957", "日東精工株式会社")
            config = PipelineConfig(
                gemini_threshold=0,
                company_percentile_threshold=100,
                random_reject_audit_rate=0,
                gemini_escalation_threshold=85,
            )
            with (
                patch.object(OpenAIReviewer, "review", return_value=(result_template, 100, 50)),
                patch.object(GeminiReviewer, "review", return_value=(result_template, 100, 50)),
            ):
                result = PatentPipeline(
                    config, [company], root / "history.sqlite3", root / "out",
                    openai_api_key="openai-test", gemini_api_key="gemini-test",
                ).run([patent], "two_stage_test")
            self.assertEqual(result["gpt_count"], 1)
            self.assertEqual(result["gemini_count"], 1)
            with Path(result["evaluation_csv"]).open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["route"], "gemini_reviewed")
            self.assertEqual(row["gpt_decision"], "important")
            self.assertEqual(row["gemini_decision"], "important")

    def test_epo_http_error_keeps_stage_body_and_quota_headers(self):
        headers = Message()
        headers["Retry-After"] = "60"
        exc = urllib.error.HTTPError(
            "https://ops.epo.org/test", 403, "Forbidden", headers, io.BytesIO(b"quota exceeded")
        )
        message = str(EPOOPSProvider._http_error("test-stage", exc))
        self.assertIn("test-stage", message)
        self.assertIn("quota exceeded", message)
        self.assertIn("Retry-After=60", message)

    def test_epo_search_404_no_results_is_empty_page_not_failure(self):
        body = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <fault xmlns="http://ops.epo.org">
          <code>SERVER.EntityNotFound</code>
          <message>No results found</message>
        </fault>'''
        exc = urllib.error.HTTPError(
            "https://ops.epo.org/search", 404, "Not Found", Message(), io.BytesIO(body)
        )
        provider = EPOOPSProvider("key", "secret")
        company_results = []
        with (
            patch.object(provider, "_token", return_value="token"),
            patch("urllib.request.urlopen", side_effect=exc),
        ):
            records = provider.search_companies(
                [Company("JPTEST", "NO RESULT CORP")],
                "2026-07-14",
                "2026-07-15",
                company_result=lambda company, success, error: company_results.append((success, error)),
            )
        self.assertEqual(records, [])
        self.assertEqual(company_results, [(True, "")])

    def test_epo_company_search_refreshes_expired_access_token(self):
        expired = urllib.error.HTTPError(
            "https://ops.epo.org/search",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(b"<error><message>invalid_access_token</message><description>Access token has expired</description></error>"),
        )
        xml = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Refreshed token title</invention-title>
            <parties><applicants><applicant><applicant-name><name>TEST CORP</name></applicant-name></applicant></applicants></parties>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", side_effect=["expired-token", "fresh-token"]) as token,
            patch("urllib.request.urlopen", side_effect=[expired, io.BytesIO(xml)]),
        ):
            records = provider.search_companies(
                [Company("JPTEST", "TEST CORP")],
                "2026-07-14",
                "2026-07-15",
            )
        self.assertEqual(token.call_count, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Refreshed token title")

    def test_epo_company_search_waits_lowers_rate_and_retries_after_throttle(self):
        xml = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Recovered after throttle</invention-title>
            <parties><applicants><applicant><applicant-name><name>TEST CORP</name></applicant-name></applicant></applicants></parties>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        provider = EPOOPSProvider("key", "secret", requests_per_minute=10)
        provider.throttle_pause_seconds = 0
        messages = []
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(
                provider,
                "_search_page",
                side_effect=[RuntimeError("HTTP 429 Retry-After=60 Client.RobotDetected"), xml],
            ) as search_page,
        ):
            records = provider.search_companies(
                [Company("JPTEST", "TEST CORP")],
                "2026-07-14",
                "2026-07-15",
                progress=messages.append,
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(search_page.call_count, 2)
        self.assertEqual(provider.requests_per_minute, 8)
        self.assertEqual(provider.last_company_search_stats["throttle_pause_count"], 1)
        self.assertFalse(provider.last_company_search_stats["stopped_by_throttle"])
        self.assertTrue(any("req/min" in message for message in messages))

    def test_unknown_market_cap_uses_neutral_factor(self):
        record = PatentRecord(source="test", publication_number="JP1", applicants=["TEST"])
        score, reasons = materiality_score(record, Company("1", "TEST", market_cap_jpy=0), 50, 50)
        self.assertEqual(score, 50)
        self.assertTrue(any("中立" in reason for reason in reasons))

    def test_gemini_model_falls_back_to_available_flash_lite(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-3.5-flash",
                    "baseModelId": "gemini-3.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.1-flash-lite",
                    "baseModelId": "gemini-3.1-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
        reviewer = GeminiReviewer("test-key", "retired-model")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode("utf-8"))):
            self.assertEqual(reviewer.resolve_model(), "gemini-3.1-flash-lite")

    def test_retired_gemini_20_is_migrated_to_31_flash_lite(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.0-flash-lite",
                    "baseModelId": "gemini-2.0-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.1-flash-lite",
                    "baseModelId": "gemini-3.1-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
        reviewer = GeminiReviewer("test-key", "gemini-2.0-flash-lite")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode("utf-8"))):
            self.assertEqual(reviewer.resolve_model(), "gemini-3.1-flash-lite")

    def test_unavailable_25_flash_lite_is_migrated_to_31(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-flash-lite",
                    "baseModelId": "gemini-2.5-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.1-flash-lite",
                    "baseModelId": "gemini-3.1-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
        reviewer = GeminiReviewer("test-key", "gemini-2.5-flash-lite")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode("utf-8"))):
            self.assertEqual(reviewer.resolve_model(), "gemini-3.1-flash-lite")

    def test_historical_validation_prompt_hides_known_stock_reaction(self):
        patent = next(item for item in mock_patents() if item.publication_number == "JP7301490B2")
        item = ScoredPatent(
            patent=patent,
            match=MatchResult(company_id="JP5957", company_name="日東精工株式会社"),
            technology_score=0.0,
            metadata_score=20.0,
            novelty_score=50.0,
            materiality_score=25.683,
            final_score=23.834,
        )
        prompt = GeminiReviewer.build_prompt(item, Company("JP5957", "日東精工株式会社"))
        self.assertIn("JP7301490B2", prompt)
        self.assertNotIn("11.6", prompt)
        self.assertNotIn("known_market_reaction", prompt)
        self.assertNotIn("23.834", prompt)
        self.assertNotIn("事前評価", prompt)
        self.assertIn("short_term_market_impact_score", prompt)
        self.assertIn("long_term_business_value_score", prompt)

    def test_medical_material_patent_matches_broad_technology_scope(self):
        patent = next(item for item in mock_patents() if item.publication_number == "JP7301490B2")
        config = PipelineConfig.from_json(Path(__file__).with_name("patent_monitor_config.json"))
        score, reasons = technology_score(
            patent,
            Company("JP5957", "日東精工株式会社", technology_tags=["医療機器", "生分解性マグネシウム"]),
            config,
        )
        self.assertGreaterEqual(score, config.broad_technology_gemini_threshold)
        self.assertTrue(any(reason.startswith("broad_") for reason in reasons))

    @unittest.skipUnless(__import__("os").name == "nt", "DPAPI is Windows-only")
    def test_dpapi_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.dpapi"
            original = {"epo_ops_key": "test-key", "epo_ops_secret": "test-secret"}
            save_credentials(original, path)
            self.assertNotIn(b"test-secret", path.read_bytes())
            self.assertEqual(load_credentials(path), original)
            self.assertTrue(delete_credentials(path))

    def test_english_company_normalization_does_not_remove_internal_co(self):
        self.assertEqual(normalize_name("COSMO ENERGY HOLDINGS CO., LTD."), "COSMOENERGY")

    def test_epo_country_marker_is_not_part_of_company_name(self):
        self.assertEqual(normalize_name("FUJITSU LTD [JP]"), "FUJITSU")

    def test_company_search_names_prioritize_epo_useful_names_without_losing_match_aliases(self):
        company = Company(
            "JPTEST",
            "テストホールディングス株式会社",
            aliases=["TEST HOLDINGS CORP", "TEST CORPORATION", "テスト株式会社"],
            subsidiaries=["TEST IP MAN CO LTD", "TEST DEVICE CO LTD", "株式会社テストデバイス"],
        )
        names = company.search_names(4)
        self.assertIn("TEST IP MAN CO LTD", names)
        self.assertIn("TEST HOLDINGS CORP", names)
        self.assertLessEqual(len(names), 4)
        self.assertIn("株式会社テストデバイス", company.all_names)

    def test_epo_applicant_abbreviations_are_normalized(self):
        self.assertEqual(
            normalize_name("PANASONIC IP MAN CO LTD [JP]"),
            "PANASONICINTELLECTUALPROPERTYMANAGEMENT",
        )
        self.assertEqual(normalize_name("TORAY IND INC [JP]"), "TORAYINDUSTRIES")
        self.assertEqual(normalize_name("MURATA MFG CO LTD [JP]"), "MURATAMANUFACTURING")
        self.assertEqual(normalize_name("\ub3c4\ub808\uc774 \uce74\ubd80\uc2dc\ud0a4\uac00\uc774\uc0e4"), "TORAYINDUSTRIES")

    def test_sparse_company_percentile_uses_neutral_or_global_baseline(self):
        value, basis, samples = stable_percentile(80, [20], [], 5, 30)
        self.assertEqual((value, basis, samples), (50.0, "neutral_insufficient_history", 1))
        global_history = list(range(40))
        value, basis, samples = stable_percentile(30, [20], global_history, 5, 30)
        self.assertEqual(basis, "global_history")
        self.assertEqual(samples, 40)
        self.assertGreater(value, 50)

    def test_epo_parser_prefers_english_and_extracts_ipc_cpc(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <world-patent-data xmlns="http://www.epo.org/exchange">
          <exchange-documents><exchange-document country="EP" doc-number="1234567" kind="A1">
            <bibliographic-data>
              <invention-title lang="de">DEUTSCHER TITEL</invention-title>
              <invention-title lang="en">English quantum title</invention-title>
              <parties><applicants><applicant><applicant-name><name>TEST CORP [JP]</name></applicant-name></applicant></applicants></parties>
              <classifications-ipcr><classification-ipcr><text>G06N 10/00</text></classification-ipcr></classifications-ipcr>
              <patent-classifications><patent-classification>
                <classification-scheme scheme="CPCI"/><section>H</section><class>01</class><subclass>L</subclass>
                <main-group>21</main-group><subgroup>00</subgroup>
              </patent-classification></patent-classifications>
              <publication-reference><document-id><date>20260715</date></document-id></publication-reference>
            </bibliographic-data>
            <abstract lang="en"><p>English abstract text.</p></abstract>
          </exchange-document></exchange-documents>
        </world-patent-data>'''
        record = EPOOPSProvider._parse(xml)[0]
        self.assertEqual(record.title, "English quantum title")
        self.assertEqual(record.abstract, "English abstract text.")
        self.assertIn("G06N10/00", record.ipc_codes)
        self.assertIn("H01L21/00", record.cpc_codes)

    def test_epo_parser_extracts_dates_priorities_claims_and_citations(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <world-patent-data xmlns="http://www.epo.org/exchange">
          <exchange-documents><exchange-document country="EP" doc-number="1234567" kind="A1" family-id="9988">
            <bibliographic-data>
              <publication-reference><document-id document-id-type="docdb">
                <country>EP</country><doc-number>1234567</doc-number><kind>A1</kind><date>20260715</date>
              </document-id></publication-reference>
              <application-reference><document-id document-id-type="docdb">
                <country>EP</country><doc-number>25123456</doc-number><kind>A</kind><date>20250120</date>
              </document-id></application-reference>
              <priority-claims>
                <priority-claim><document-id document-id-type="docdb">
                  <country>JP</country><doc-number>2024012345</doc-number><kind>A</kind><date>20240118</date>
                </document-id></priority-claim>
                <priority-claim><document-id document-id-type="docdb">
                  <country>US</country><doc-number>2024123456</doc-number><kind>A</kind><date>20240201</date>
                </document-id></priority-claim>
              </priority-claims>
              <references-cited><citation><patcit><document-id document-id-type="docdb">
                <country>US</country><doc-number>7654321</doc-number><kind>B2</kind>
              </document-id></patcit></citation>
              <citation><nplcit><text>Journal article DOI 10.1000/test</text></nplcit></citation></references-cited>
            </bibliographic-data>
            <claims lang="en"><claim num="1"><claim-text>First.</claim-text></claim>
              <claim num="2"><claim-text>Second.</claim-text></claim></claims>
          </exchange-document></exchange-documents>
        </world-patent-data>'''
        metadata = {
            "retrieved_at": "2026-07-18T12:00:00+09:00",
            "source_endpoint": "published-data/publication/epodoc/biblio",
            "query_window_start": "2026-07-11",
            "query_window_end": "2026-07-17",
            "payload_hash": "abc123",
            "parser_version": "test-parser",
        }
        record = EPOOPSProvider._parse(xml, metadata)[0]
        self.assertEqual(record.application_number, "EP25123456A")
        self.assertEqual(record.filing_date, "2025-01-20")
        self.assertEqual(record.publication_date, "2026-07-15")
        self.assertEqual(record.priority_date, "2024-01-18")
        self.assertEqual(record.priority_numbers, ["JP2024012345A", "US2024123456A"])
        self.assertEqual(record.claim_count, 2)
        self.assertEqual(record.citations, ["US7654321B2"])
        self.assertEqual(record.non_patent_citations, ["Journal article DOI 10.1000/test"])
        self.assertEqual(record.family_id, "9988")
        self.assertEqual(record.payload_hash, "abc123")

    def test_epo_family_parser_extracts_members_legal_and_ownership_events(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
          <ops:patent-family legal="true" total-result-count="2">
            <ops:family-member><publication-reference><document-id document-id-type="docdb">
              <country>EP</country><doc-number>1234567</doc-number><kind>A1</kind>
            </document-id></publication-reference>
              <ops:legal code="GRANT" desc="PATENT GRANTED" dateMigr="20260102" infl="+" />
            </ops:family-member>
            <ops:family-member><publication-reference><document-id document-id-type="docdb">
              <country>US</country><doc-number>9999999</doc-number><kind>B2</kind>
            </document-id></publication-reference>
              <ops:legal code="ASSIGN" desc="TRANSFER OF OWNER" dateMigr="20260304" />
            </ops:family-member>
          </ops:patent-family>
        </ops:world-patent-data>'''
        family = EPOOPSProvider._parse_family_metadata(xml)
        self.assertEqual(family["family_members"], ["EP1234567A1", "US9999999B2"])
        self.assertEqual(family["country_codes"], ["EP", "US"])
        self.assertEqual(family["family_size"], 2)
        self.assertEqual(family["legal_status"], "granted")
        self.assertEqual(len(family["legal_events"]), 2)
        self.assertEqual(len(family["ownership_events"]), 1)

    def test_raw_normalized_and_quality_data_survive_staging_cleanup(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "collector.sqlite3"
            init_collector_db(db)
            content = b"<world-patent-data><test>raw payload</test></world-patent-data>"
            payload_hash = __import__("hashlib").sha256(content).hexdigest()
            _save_raw_epo_payload(
                db,
                {
                    "payload_hash": payload_hash,
                    "retrieved_at": "2026-07-18T12:00:00+09:00",
                    "source_endpoint": "published-data/search/biblio",
                    "query_window_start": "2026-07-11",
                    "query_window_end": "2026-07-17",
                    "company_id": "C1",
                    "company_name": "TEST",
                    "parser_version": "test-parser",
                },
                content,
            )
            record = PatentRecord(
                source="epo_ops",
                publication_number="EP123A1",
                application_number="EP456A",
                family_id="F1",
                publication_date="2026-07-15",
                filing_date="2025-01-01",
                priority_date="2024-01-01",
                payload_hash=payload_hash,
                citation_count=3,
                family_size=2,
                claim_count=10,
                legal_status="granted",
            )
            start = date(2026, 7, 11)
            end = date(2026, 7, 17)
            _persist_normalized_records(db, [record], start, end)
            _stage_company_records(db, start, end, "C1", "TEST", [record], ["TEST CORP"])
            _archive_search_quality(
                db,
                start,
                end,
                {
                    "companies": [{
                        "company_id": "C1", "company_name": "TEST", "success": True,
                        "record_count": 1, "capped": True, "searched_names": ["TEST CORP"], "error": "",
                    }],
                    "skipped_companies": [],
                    "throttle_pause_count": 1,
                },
                "2026-07-18T11:00:00+09:00",
            )
            _record_window(
                db,
                start,
                end,
                "completed",
                "2026-07-18T11:00:00+09:00",
                "2026-07-18T12:00:00+09:00",
                input_records=1,
                kept_records=1,
                search_stats={
                    "failure_count": 2,
                    "throttle_pause_count": 1,
                    "capped_companies": [{"company_id": "C1"}],
                },
                detail_stats={"enriched_count": 1},
            )
            _clear_window_staging(db, start, end)
            connection = sqlite3.connect(db)
            try:
                compressed = connection.execute(
                    "SELECT content_gzip FROM epo_raw_payloads WHERE payload_hash=?", (payload_hash,)
                ).fetchone()[0]
                observation_count = connection.execute(
                    "SELECT COUNT(*) FROM epo_raw_observations"
                ).fetchone()[0]
                normalized = connection.execute(
                    "SELECT application_number, claim_count, legal_status FROM normalized_patent_records"
                ).fetchone()
                quality = connection.execute(
                    "SELECT capped, throttle_count, searched_names_json FROM collector_company_quality_history"
                ).fetchone()
                progress_count = connection.execute(
                    "SELECT COUNT(*) FROM collector_company_progress"
                ).fetchone()[0]
                window_quality = connection.execute(
                    """SELECT epo_failure_count, epo_throttle_count, capped_company_count,
                              search_stats_json, detail_stats_json FROM collector_windows"""
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(gzip.decompress(compressed), content)
            self.assertEqual(observation_count, 1)
            self.assertEqual(normalized, ("EP456A", 10, "granted"))
            self.assertEqual(quality[:2], (1, 1))
            self.assertEqual(json.loads(quality[2]), ["TEST CORP"])
            self.assertEqual(progress_count, 0)
            self.assertEqual(window_quality[:3], (2, 1, 1))
            self.assertEqual(json.loads(window_quality[3])["failure_count"], 2)
            self.assertEqual(json.loads(window_quality[4])["enriched_count"], 1)

    def test_stalled_company_skip_is_durable_and_not_overwritten(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "collector.sqlite3"
            init_collector_db(db)
            start = date(2026, 7, 11)
            end = date(2026, 7, 17)
            _record_company_skip(
                db,
                start,
                end,
                "JPTEST",
                "TEST CORP",
                "skipped_stalled",
                "skipped_stalled: company search exceeded per-company deadline",
                start,
                end,
                ["TEST CORP"],
                "2026-07-18T11:00:00+09:00",
                1800,
                {"company_id": "JPTEST", "company_name": "TEST CORP"},
            )
            _archive_search_quality(
                db,
                start,
                end,
                {
                    "companies": [],
                    "skipped_companies": [{
                        "company_id": "JPTEST",
                        "company_name": "TEST CORP",
                        "status": "skipped_stalled",
                        "reason": "skipped_stalled: company search exceeded per-company deadline",
                        "record_count": 0,
                        "searched_names": ["TEST CORP"],
                    }],
                    "throttle_pause_count": 0,
                },
                "2026-07-18T11:00:00+09:00",
            )
            connection = sqlite3.connect(db)
            try:
                progress = connection.execute(
                    "SELECT status, record_count FROM collector_company_progress WHERE company_id='JPTEST'"
                ).fetchone()
                quality = connection.execute(
                    "SELECT status, error, searched_names_json FROM collector_company_quality_history WHERE company_id='JPTEST'"
                ).fetchone()
                skip = connection.execute(
                    "SELECT status, reason, elapsed_seconds FROM collector_company_skip_history WHERE company_id='JPTEST'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(progress, ("skipped_stalled", 0))
            self.assertEqual(quality[0], "skipped_stalled")
            self.assertIn("deadline", quality[1])
            self.assertEqual(json.loads(quality[2]), ["TEST CORP"])
            self.assertEqual(skip, ("skipped_stalled", "skipped_stalled: company search exceeded per-company deadline", 1800))

    def test_epo_search_zero_max_records_means_unlimited(self):
        def page_xml(start: int, count: int) -> bytes:
            documents = "".join(
                f'''<exchange-document country="EP" doc-number="{start + index}" kind="A1">
                  <bibliographic-data><invention-title lang="en">Title {start + index}</invention-title></bibliographic-data>
                </exchange-document>'''
                for index in range(count)
            )
            return (
                '<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>'
                + documents
                + "</exchange-documents></world-patent-data>"
            ).encode("utf-8")

        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_search_page", side_effect=[page_xml(1, 100), page_xml(101, 20)]) as search_page,
        ):
            records = provider.search("test-query", max_records=0)
        self.assertEqual(len(records), 120)
        self.assertEqual(
            [call.args[2:] for call in search_page.call_args_list],
            [(1, 100), (101, 200)],
        )

    def test_epo_company_search_zero_max_per_company_means_unlimited(self):
        def page_xml(start: int, count: int) -> bytes:
            documents = "".join(
                f'''<exchange-document country="EP" doc-number="{start + index}" kind="A1">
                  <bibliographic-data>
                    <invention-title lang="en">Company Title {start + index}</invention-title>
                    <parties><applicants><applicant><applicant-name><name>TEST CORP</name></applicant-name></applicant></applicants></parties>
                  </bibliographic-data>
                </exchange-document>'''
                for index in range(count)
            )
            return (
                '<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>'
                + documents
                + "</exchange-documents></world-patent-data>"
            ).encode("utf-8")

        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_search_page", side_effect=[page_xml(1, 100), page_xml(101, 20)]) as search_page,
        ):
            records = provider.search_companies(
                [Company("JPTEST", "TEST CORP")],
                "2026-07-14",
                "2026-07-15",
                max_per_company=0,
            )
        self.assertEqual(len(records), 120)
        self.assertEqual(
            [call.args[2:] for call in search_page.call_args_list],
            [(1, 100), (101, 200)],
        )

    def test_epo_company_search_records_capped_companies_and_checkpoint(self):
        def page_xml(start: int, count: int) -> bytes:
            documents = "".join(
                f'''<exchange-document country="EP" doc-number="{start + index}" kind="A1">
                  <bibliographic-data><invention-title lang="en">Title {start + index}</invention-title></bibliographic-data>
                </exchange-document>'''
                for index in range(count)
            )
            return (
                '<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>'
                + documents
                + "</exchange-documents></world-patent-data>"
            ).encode("utf-8")

        provider = EPOOPSProvider("key", "secret")
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with (
                patch.object(provider, "_token", return_value="token"),
                patch.object(provider, "_search_page", return_value=page_xml(1, 50)),
            ):
                records = provider.search_companies(
                    [Company("JPTEST", "TEST CORP")],
                    "2026-07-14",
                    "2026-07-15",
                    max_per_company=50,
                    checkpoint_path=checkpoint,
                )
            self.assertEqual(len(records), 50)
            self.assertEqual(len(provider.last_company_search_stats["capped_companies"]), 1)
            self.assertTrue(checkpoint.exists())

    def test_epo_company_search_includes_subsidiaries_with_name_limit(self):
        xml = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Subsidiary title</invention-title>
            <parties><applicants><applicant><applicant-name><name>SUB ONE LTD</name></applicant-name></applicant></applicants></parties>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        provider = EPOOPSProvider("key", "secret")
        company = Company(
            "JPTEST",
            "TEST HOLDINGS",
            aliases=["TEST ALIAS"],
            subsidiaries=["SUB ONE LTD", "SUB TWO LTD"],
        )
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_search_page", return_value=xml) as search_page,
        ):
            records = provider.search_companies(
                [company],
                "2026-07-14",
                "2026-07-15",
                max_per_company=100,
                max_applicant_names=3,
            )
        self.assertEqual(len(records), 1)
        query = search_page.call_args.args[1]
        self.assertIn('pa="TEST HOLDINGS"', query)
        self.assertIn('pa="TEST ALIAS"', query)
        self.assertIn('pa="SUB ONE LTD"', query)
        self.assertNotIn('SUB TWO LTD', query)

    def test_epo_company_search_stops_before_requests_when_requested(self):
        provider = EPOOPSProvider("key", "secret")
        companies = [Company("C1", "ONE CORP"), Company("C2", "TWO CORP")]
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_search_page") as search_page,
        ):
            records = provider.search_companies(
                companies,
                "2026-07-14",
                "2026-07-15",
                stop_requested=lambda: True,
            )
        self.assertEqual(records, [])
        self.assertEqual(search_page.call_count, 0)
        self.assertTrue(provider.last_company_search_stats["stopped_by_request"])
        self.assertEqual(len(provider.last_company_search_stats["skipped_companies"]), 2)

    def test_epo_company_search_retries_on_robot_detection(self):
        provider = EPOOPSProvider("key", "secret")
        provider.throttle_pause_seconds = 0
        companies = [Company("C1", "ONE CORP"), Company("C2", "TWO CORP")]
        error = RuntimeError(
            "EPO OPS 特許検索: HTTP 403 Forbidden / Retry-After=900000, "
            "X-Throttling-Control=busy (search=black:0) / response=<code>CLIENT.RobotDetected</code>"
        )
        xml = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Recovered from robot throttle</invention-title>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_search_page", side_effect=[error, xml, EPOOPSProvider._empty_search_response()]) as search_page,
        ):
            records = provider.search_companies(
                companies,
                "2026-07-14",
                "2026-07-15",
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(search_page.call_count, 3)
        stats = provider.last_company_search_stats
        self.assertFalse(stats["stopped_by_throttle"])
        self.assertEqual(stats["throttle_pause_count"], 1)
        self.assertEqual(stats["failure_count"], 0)
        self.assertEqual(len(stats["skipped_companies"]), 0)

    def test_epo_enrichment_fetches_details_only_for_matched_company(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Quantum controller</invention-title>
            <classifications-ipcr><classification-ipcr><text>G06N 10/00</text></classification-ipcr></classifications-ipcr>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        abstract = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1">
            <abstract lang="en"><p>Detailed English abstract.</p></abstract>
          </exchange-document></exchange-documents></world-patent-data>'''
        matched = PatentRecord(
            source="epo_ops", publication_number="EP123A1", title="ALTER TITEL",
            applicants=["FUJITSU LTD [JP]"],
        )
        unmatched = PatentRecord(
            source="epo_ops", publication_number="EP999A1", title="Other",
            applicants=["UNRELATED CORP [US]"],
        )
        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_published_data", side_effect=[biblio, abstract]) as detail,
        ):
            summary = provider.enrich_records(
                [matched, unmatched],
                [Company("JP6702", "富士通株式会社", aliases=["FUJITSU LTD"])],
                max_records=10,
                enrich_claims=False,
                enrich_family_legal=False,
                enrich_forward_citations=False,
            )
        self.assertEqual(detail.call_count, 2)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(summary["classification_filled_count"], 1)
        self.assertEqual(summary["abstract_filled_count"], 1)
        self.assertEqual(matched.title, "Quantum controller")
        self.assertIn("G06N10/00", matched.ipc_codes)
        self.assertEqual(matched.abstract, "Detailed English abstract.")
        self.assertFalse(unmatched.detail_enriched)

    def test_epo_enrichment_stops_before_token_when_requested(self):
        provider = EPOOPSProvider("key", "secret")
        with patch.object(provider, "_token") as token:
            summary = provider.enrich_records(
                [PatentRecord(source="epo_ops", publication_number="EP123A1", applicants=["FUJITSU LTD [JP]"])],
                [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])],
                stop_requested=lambda: True,
            )
        self.assertEqual(token.call_count, 0)
        self.assertTrue(summary["stopped_by_request"])
        self.assertEqual(summary["deferred_count"], 1)
        self.assertEqual(summary["enriched_count"], 0)

    def test_epo_technology_search_uses_same_day_query_and_deduplicates_categories(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <publication-reference><document-id><country>EP</country><doc-number>123</doc-number><kind>A1</kind><date>20260902</date></document-id></publication-reference>
            <parties><applicants><applicant><applicant-name><name>FUTURE ENERGY LAB</name></applicant-name></applicant></applicants></parties>
            <invention-title lang="en">Quantum battery controller</invention-title>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        provider = EPOOPSProvider("key", "secret")
        queries = [
            {"name": "量子技術", "query": 'ta="quantum computing"'},
            {"name": "次世代電池", "query": 'ta="solid state battery"'},
        ]
        with patch.object(provider, "_token", return_value="token"), patch.object(
            provider, "_search_page", return_value=biblio
        ) as search:
            records, stats = provider.search_technologies(
                queries, "2026-09-02", "2026-09-02", max_records_per_query=10
            )
        self.assertEqual(2, search.call_count)
        self.assertIn('pd within "2026-09-02 2026-09-02"', search.call_args_list[0].args[1])
        self.assertEqual(1, len(records))
        self.assertTrue(records[0].raw["technology_discovery"])
        self.assertEqual(["量子技術", "次世代電池"], records[0].raw["technology_categories"])
        self.assertEqual(2, stats["successful_query_count"])

    def test_epo_enrichment_uses_detail_cache(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Cached title</invention-title>
            <classifications-ipcr><classification-ipcr><text>G06N 10/00</text></classification-ipcr></classifications-ipcr>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        abstract = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1">
            <abstract lang="en"><p>Cached abstract.</p></abstract>
          </exchange-document></exchange-documents></world-patent-data>'''
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "details.sqlite3"
            provider = EPOOPSProvider("key", "secret")
            provider.set_detail_cache(cache)
            with (
                patch.object(provider, "_token", return_value="token"),
                patch.object(provider, "_published_data", side_effect=[biblio, abstract]) as detail,
            ):
                summary = provider.enrich_records(
                    [PatentRecord(source="epo_ops", publication_number="EP123A1", applicants=["FUJITSU LTD [JP]"])],
                    [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])],
                    enrich_claims=False,
                    enrich_family_legal=False,
                    enrich_forward_citations=False,
                )
            self.assertEqual(detail.call_count, 2)
            self.assertEqual(summary["cache_write_count"], 2)
            connection = sqlite3.connect(cache)
            stored, compression, response_size = connection.execute(
                "SELECT content,compression,response_size FROM epo_detail_cache "
                "WHERE publication_number='EP123A1' AND constituent='biblio'"
            ).fetchone()
            connection.close()
            self.assertEqual(compression, "gzip")
            self.assertEqual(response_size, len(biblio))
            self.assertEqual(gzip.decompress(stored), biblio)

            provider2 = EPOOPSProvider("key", "secret")
            provider2.set_detail_cache(cache)
            with (
                patch.object(provider2, "_token", return_value="token"),
                patch.object(provider2, "_published_data") as detail2,
            ):
                summary2 = provider2.enrich_records(
                    [PatentRecord(source="epo_ops", publication_number="EP123A1", applicants=["FUJITSU LTD [JP]"])],
                    [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])],
                    enrich_claims=False,
                    enrich_family_legal=False,
                    enrich_forward_citations=False,
                )
            self.assertEqual(detail2.call_count, 0)
            self.assertGreaterEqual(summary2["cache_hit_count"], 1)

    def test_epo_detail_cache_migrates_legacy_uncompressed_rows(self):
        content = b"<legacy-cache>payload</legacy-cache>"
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "details.sqlite3"
            connection = sqlite3.connect(cache)
            connection.execute(
                """
                CREATE TABLE epo_detail_cache (
                    publication_number TEXT NOT NULL,
                    constituent TEXT NOT NULL,
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (publication_number, constituent)
                )
                """
            )
            connection.execute(
                "INSERT INTO epo_detail_cache VALUES(?,?,?,?)",
                ("EP123A1", "biblio", content, "2026-07-18T00:00:00"),
            )
            connection.commit()
            connection.close()

            provider = EPOOPSProvider("key", "secret")
            provider.set_detail_cache(cache)
            self.assertEqual(provider._cached_published_data("EP123A1", "biblio"), content)

            connection = sqlite3.connect(cache)
            stored, compression, response_size = connection.execute(
                "SELECT content,compression,response_size FROM epo_detail_cache"
            ).fetchone()
            connection.close()
            self.assertEqual(compression, "gzip")
            self.assertEqual(response_size, len(content))
            self.assertEqual(gzip.decompress(stored), content)

    def test_epo_enrichment_populates_claims_family_legal_and_forward_citations(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Complete record</invention-title>
            <application-reference><document-id document-id-type="docdb"><country>EP</country>
              <doc-number>25123</doc-number><kind>A</kind><date>20250101</date>
            </document-id></application-reference>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        abstract = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><abstract lang="en">Abstract.</abstract>
          </exchange-document></exchange-documents></world-patent-data>'''
        claims = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><claims lang="en">
            <claim num="1"><claim-text>Claim one.</claim-text></claim>
            <claim num="2"><claim-text>Claim two.</claim-text></claim>
          </claims></exchange-document></exchange-documents></world-patent-data>'''
        family = b'''<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
          <ops:patent-family><ops:family-member><publication-reference><document-id document-id-type="docdb">
            <country>EP</country><doc-number>123</doc-number><kind>A1</kind>
          </document-id></publication-reference><ops:legal code="GRANT" desc="PATENT GRANTED" /></ops:family-member>
          <ops:family-member><publication-reference><document-id document-id-type="docdb">
            <country>JP</country><doc-number>456</doc-number><kind>A</kind>
          </document-id></publication-reference></ops:family-member></ops:patent-family>
        </ops:world-patent-data>'''
        cited = b'''<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
          <ops:biblio-search total-result-count="7"><ops:search-result><exchange-documents>
            <exchange-document country="US" doc-number="999" kind="B2" />
          </exchange-documents></ops:search-result></ops:biblio-search></ops:world-patent-data>'''
        record = PatentRecord(
            source="epo_ops", publication_number="EP123A1", applicants=["FUJITSU LTD [JP]"]
        )
        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_published_data", side_effect=[biblio, abstract, claims]),
            patch.object(provider, "_family_data", return_value=family),
            patch.object(provider, "_search_page", return_value=cited),
        ):
            summary = provider.enrich_records(
                [record], [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])]
            )
        self.assertEqual(record.application_number, "EP25123A")
        self.assertEqual(record.claim_count, 2)
        self.assertEqual(record.family_size, 2)
        self.assertEqual(record.legal_status, "granted")
        self.assertEqual(record.cited_by, ["US999B2"])
        self.assertEqual(record.citation_count, 7)
        self.assertEqual(summary["forward_citation_count"], 7)

    def test_epo_claims_enrichment_can_be_limited_to_top_rate(self):
        biblio = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><bibliographic-data>
            <invention-title lang="en">Complete record</invention-title>
          </bibliographic-data></exchange-document></exchange-documents></world-patent-data>'''
        abstract = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><abstract lang="en">Abstract.</abstract>
          </exchange-document></exchange-documents></world-patent-data>'''
        claims = b'''<world-patent-data xmlns="http://www.epo.org/exchange"><exchange-documents>
          <exchange-document country="EP" doc-number="123" kind="A1"><claims lang="en">
            <claim num="1"><claim-text>Claim one.</claim-text></claim>
          </claims></exchange-document></exchange-documents></world-patent-data>'''
        records = [
            PatentRecord(source="epo_ops", publication_number=f"EP{index}A1", applicants=["FUJITSU LTD"])
            for index in range(10)
        ]
        provider = EPOOPSProvider("key", "secret")
        with (
            patch.object(provider, "_token", return_value="token"),
            patch.object(provider, "_published_data", side_effect=[biblio, abstract] * 10 + [claims]),
        ):
            summary = provider.enrich_records(
                records,
                [Company("JP6702", "FUJITSU", aliases=["FUJITSU LTD"])],
                max_records=10,
                enrich_claims=True,
                claims_top_rate=0.1,
                enrich_family_legal=False,
                enrich_forward_citations=False,
            )
        self.assertEqual(summary["claims_candidate_count"], 1)
        self.assertEqual(summary["claims_filled_count"], 1)
        self.assertEqual(sum(bool(record.claims) for record in records), 1)

    def test_backward_chunks_splits_inclusive_date_ranges(self):
        chunks = _backward_chunks(date(2026, 7, 1), date(2026, 7, 18), 7)
        self.assertEqual(
            chunks,
            [
                (date(2026, 7, 1), date(2026, 7, 4)),
                (date(2026, 7, 5), date(2026, 7, 11)),
                (date(2026, 7, 12), date(2026, 7, 18)),
            ],
        )

    def test_family_consolidation(self):
        families = consolidate_families(mock_patents())
        self.assertEqual(len(families), 11)
        merged = next(item for item in families if item[0].family_key == "MOCKFAMSEMICONDUCTOR")
        self.assertEqual(len(merged[1]), 2)
        self.assertIn("JP", merged[0].country_codes)
        self.assertIn("US", merged[0].country_codes)

    def test_ambiguous_fuzzy_match_is_not_auto_adopted(self):
        matcher = CompanyMatcher(
            [
                Company("1", "ABCテクノロジーA株式会社"),
                Company("2", "ABCテクノロジーB株式会社"),
            ],
            threshold=0.75,
            margin=0.10,
        )
        result = matcher.match(["ABCテクノロジー株式会社"])
        self.assertTrue(result.review_required)
        self.assertEqual(result.company_id, "")

    def test_duplicate_aliases_for_same_company_are_not_ambiguous(self):
        matcher = CompanyMatcher(
            [Company("1", "ソニーグループ株式会社", aliases=["SONY GROUP CORPORATION", "SONY CORPORATION"])],
            threshold=0.90,
            margin=0.05,
        )
        result = matcher.match(["SONY GROUP CORPORATION"])
        self.assertFalse(result.review_required)
        self.assertEqual(result.company_id, "1")

    def test_company_master_matches_recent_epo_applicant_aliases(self):
        companies = read_companies(Path(__file__).with_name("patent_company_master.csv"))
        matcher = CompanyMatcher(companies, threshold=0.90, margin=0.05)
        expected = {
            "PANASONIC IP MAN CO LTD [JP]": "JP6752",
            "\u30d1\u30ca\u30bd\u30cb\u30c3\u30af\uff29\uff30\u30de\u30cd\u30b8\u30e1\u30f3\u30c8\u682a\u5f0f\u4f1a\u793e": "JP6752",
            "PANASONIC IND CO LTD [JP]": "JP6752",
            "TORAY IND INC [JP]": "JP3402",
            "\ub3c4\ub808\uc774 \uce74\ubd80\uc2dc\ud0a4\uac00\uc774\uc0e4": "JP3402",
            "MURATA MFG CO LTD [JP]": "JP6981",
            "IHI AEROSPACE CO LTD [JP]": "JP7013",
            "NIKON ESSILOR CO LTD [JP]": "JP7731",
        }
        for applicant, company_id in expected.items():
            with self.subTest(applicant=applicant):
                result = matcher.match([applicant])
                self.assertEqual(result.company_id, company_id)

    def test_pipeline_runs_without_api_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            companies = [
                Company(
                    "C1", "北陸電子株式会社", aliases=["HOKURIKU ELECTRONICS CO LTD", "北陸電子工業株式会社"],
                    market_cap_jpy=280_000_000_000, technology_tags=["半導体", "量子"],
                ),
                Company("C2", "東都サービス株式会社", market_cap_jpy=65_000_000_000),
            ]
            config = PipelineConfig(
                target_cpc_prefixes=["H10D", "G06N10"], target_keywords=["半導体", "量子"],
                excluded_keywords=["予約システム"], random_reject_audit_rate=0,
            )
            result = PatentPipeline(config, companies, root / "history.sqlite3", root / "out").run(mock_patents(), "mock")
            self.assertEqual(result["input_count"], 12)
            self.assertEqual(result["family_count"], 11)
            self.assertEqual(result["error_count"], 0)
            self.assertTrue(Path(result["evaluation_csv"]).exists())

    def test_reprocessing_same_patent_does_not_reduce_novelty(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            patent = next(item for item in mock_patents() if item.publication_number == "JP7301490B2")
            company = Company("JP5957", "日東精工株式会社")
            config = PipelineConfig(
                gemini_threshold=101,
                company_percentile_threshold=101,
                random_reject_audit_rate=0,
            )
            database_path = root / "history.sqlite3"
            first = PatentPipeline(config, [company], database_path, root / "out").run([patent], "repeat_test")
            second = PatentPipeline(config, [company], database_path, root / "out").run([patent], "repeat_test")

            def novelty(result):
                with Path(result["evaluation_csv"]).open(encoding="utf-8-sig", newline="") as handle:
                    return float(next(csv.DictReader(handle))["novelty_score"])

            self.assertEqual(novelty(first), 50.0)
            self.assertEqual(novelty(second), 50.0)

    def test_collector_state_and_database_are_resume_ready(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "collector_state.json"
            db_path = root / "collector.sqlite3"
            state = load_collector_state(state_path)
            self.assertEqual(state["mode"], "idle")
            state["next_end_date"] = "2026-07-16"
            state["total_windows_completed"] = 3
            save_collector_state(state_path, state)
            reloaded = load_collector_state(state_path)
            self.assertEqual(reloaded["next_end_date"], "2026-07-16")
            self.assertEqual(reloaded["total_windows_completed"], 3)

            init_collector_db(db_path)
            connection = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
            self.assertIn("collected_patents", tables)
            self.assertIn("collector_windows", tables)

    def test_collector_saves_minimal_patents_idempotently(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "collector.sqlite3"
            csv_path = root / "patent_evaluations.csv"
            init_collector_db(db_path)
            columns = [
                "publication_number", "publication_date", "title", "applicants",
                "company_id", "company_name", "cpc_codes", "ipc_codes",
                "technology_score", "final_score", "route", "source_url",
                "candidate_reasons_json",
            ]
            rows = [
                {
                    "publication_number": "JP100A",
                    "publication_date": "2026-07-16",
                    "title": "Quantum device",
                    "applicants": "TEST CORP",
                    "company_id": "JPTEST",
                    "company_name": "テスト株式会社",
                    "cpc_codes": "G06N 10/00",
                    "ipc_codes": "",
                    "technology_score": "80",
                    "final_score": "70",
                    "route": "gpt_pending",
                    "source_url": "https://example.test",
                    "candidate_reasons_json": "{}",
                },
                {
                    "publication_number": "JP200A",
                    "publication_date": "2026-07-16",
                    "title": "Low score",
                    "applicants": "UNKNOWN",
                    "company_id": "",
                    "company_name": "",
                    "cpc_codes": "",
                    "ipc_codes": "",
                    "technology_score": "0",
                    "final_score": "5",
                    "route": "rejected",
                    "source_url": "",
                    "candidate_reasons_json": "",
                },
            ]
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)

            self.assertEqual(save_minimal_patents(db_path, csv_path, "run1"), 1)
            self.assertEqual(save_minimal_patents(db_path, csv_path, "run2"), 1)
            connection = sqlite3.connect(db_path)
            try:
                saved = connection.execute(
                    "SELECT publication_number, run_id FROM collected_patents"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(saved, [("JP100A", "run2")])

    def test_collector_treats_stopped_unscored_window_as_interrupted(self):
        self.assertTrue(
            _window_was_interrupted(
                {"family_count": 10, "scored_count": 0},
                lambda: True,
            )
        )
        self.assertTrue(
            _window_was_interrupted(
                {"family_count": 10, "scored_count": 9},
                lambda: True,
            )
        )
        self.assertFalse(
            _window_was_interrupted(
                {"family_count": 10, "scored_count": 10},
                lambda: True,
            )
        )
        self.assertFalse(
            _window_was_interrupted(
                {"family_count": 10, "scored_count": 0},
                lambda: False,
            )
        )

    def test_collector_retry_queue_deduplicates_retried_windows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capped_companies_retry_queue.csv"
            stats = {
                "max_per_company": 50,
                "capped_companies": [
                    {
                        "company_id": "JP7203",
                        "company_name": "トヨタ自動車株式会社",
                        "record_count": 50,
                        "searched_names": ["TOYOTA MOTOR CORPORATION"],
                    }
                ],
                "skipped_companies": [],
            }
            _append_retry_rows(path, date(2026, 7, 11), date(2026, 7, 17), stats)
            _append_retry_rows(path, date(2026, 7, 11), date(2026, 7, 17), stats)
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company_id"], "JP7203")

    def test_collector_window_area_kind_labels_resume_context(self):
        self.assertEqual(_window_area_kind(""), "新規領域")
        self.assertEqual(_window_area_kind("interrupted"), "欠損補完")
        self.assertEqual(_window_area_kind("error"), "欠損補完")
        self.assertEqual(_window_area_kind("running"), "欠損補完")
        self.assertEqual(_window_area_kind("completed"), "完了済み再処理")
        self.assertEqual(_session_days(3, 7), 21)

    def test_collector_adaptive_rate_bounds(self):
        self.assertEqual(_adaptive_rate_initial({}, 9), 9)
        self.assertEqual(_adaptive_rate_initial({"collector_ops_requests_per_minute": 12}, 9), 9)
        self.assertEqual(_adaptive_rate_initial({"collector_ops_requests_per_minute": 0}, 9), 9)
        self.assertEqual(_adaptive_rate_after_throttle(9), 8)
        self.assertEqual(_adaptive_rate_after_throttle(1), 1)
        self.assertEqual(_adaptive_rate_after_stable_window(8, 9), 9)
        self.assertEqual(_adaptive_rate_after_stable_window(9, 9), 9)

    def test_collector_gui_log_filter_keeps_progress_light(self):
        self.assertTrue(should_log_collector_progress("放置収集: 2026-07-11〜2026-07-17 / 新規領域"))
        self.assertTrue(should_log_collector_progress("EPOレート調整: スロットル検知のため次回 8 req/min に下げます"))
        self.assertTrue(should_log_collector_progress("EPO企業別検索 25/451社: X 新規候補=0 累計=1 失敗=0"))
        self.assertTrue(should_log_collector_progress("EPO企業別検索 26/451社: X 新規候補=0 累計=1 失敗=1"))
        self.assertFalse(should_log_collector_progress("EPO企業別検索 26/451社: X 新規候補=0 累計=1 失敗=0"))
        self.assertTrue(should_log_collector_progress("EPO詳細補完 25/200件: EP1 分類=2 要約=あり"))
        self.assertFalse(should_log_collector_progress("EPO詳細補完 26/200件: EP1 分類=2 要約=あり"))


    def test_ranked_ai_routes_top_twenty_percent_and_one_percent_audit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(ranked_gpt_top_rate=0.20, ranked_gpt_audit_rate=0.01),
                [], root / "history.sqlite3", root / "out",
            )
            items = [
                ScoredPatent(
                    patent=PatentRecord(source="test", publication_number=f"JP{index:04d}"),
                    match=MatchResult(company_id="C1", company_name="Test"),
                    final_score=float(index),
                )
                for index in range(100)
            ]
            pipeline._assign_ranked_ai_routes(items, "2026-01-01/2026-12-31")
            self.assertEqual(sum(item.route == "gpt_ranked" for item in items), 20)
            self.assertEqual(sum(item.route == "gpt_audit" for item in items), 1)
            first_audit = [item.patent.identity for item in items if item.route == "gpt_audit"]
            pipeline._assign_ranked_ai_routes(items, "2026-01-01/2026-12-31")
            self.assertEqual(
                [item.patent.identity for item in items if item.route == "gpt_audit"], first_audit
            )
            pipeline.database.close()

    def test_ranked_ai_sends_top_fifteen_percent_of_gpt_results_to_gemini(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(ranked_gemini_top_rate=0.15),
                [], root / "history.sqlite3", root / "out",
            )
            items = [
                ScoredPatent(
                    patent=PatentRecord(source="test", publication_number=f"JP{index:04d}"),
                    match=MatchResult(company_id="C1", company_name="Test"),
                    final_score=float(index),
                    gpt_result={"importance_score": index},
                )
                for index in range(20)
            ]
            selected = pipeline._select_gemini_candidates(items)
            self.assertEqual(len(selected), 3)
            self.assertEqual([item.gpt_result["importance_score"] for item in selected], [19, 18, 17])
            pipeline.database.close()

    def test_daily_gpt_quota_prioritizes_highest_ranked_candidate(self):
        items = [
            ScoredPatent(
                patent=PatentRecord(source="test", publication_number="LOW"),
                match=MatchResult(company_id="C1", company_name="Test"),
                final_score=40,
                route="gpt_audit",
            ),
            ScoredPatent(
                patent=PatentRecord(source="test", publication_number="TOP"),
                match=MatchResult(company_id="C1", company_name="Test"),
                final_score=99,
                route="gpt_ranked",
            ),
            ScoredPatent(
                patent=PatentRecord(source="test", publication_number="MID"),
                match=MatchResult(company_id="C1", company_name="Test"),
                final_score=80,
                route="gpt_ranked",
            ),
        ]
        ordered = PatentPipeline._ranked_gpt_candidates(items)
        self.assertEqual([item.patent.publication_number for item in ordered], ["TOP", "MID", "LOW"])

    def test_technology_discovery_has_separate_gpt_ranking_after_company_candidates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(
                    ranked_gpt_top_rate=1,
                    ranked_gpt_audit_rate=0,
                    technology_discovery_gpt_top_rate=1,
                ),
                [], root / "history.sqlite3", root / "out",
            )
            company_item = ScoredPatent(
                patent=PatentRecord(source="test", publication_number="COMPANY"),
                match=MatchResult(company_id="C1", company_name="Monitored"),
                final_score=50,
            )
            technology_patent = PatentRecord(source="test", publication_number="TECH")
            technology_patent.raw["technology_discovery"] = True
            technology_item = ScoredPatent(
                patent=technology_patent,
                match=MatchResult(),
                final_score=99,
            )
            pipeline._assign_ranked_ai_routes([company_item, technology_item], "daily")
            self.assertEqual("gpt_ranked", company_item.route)
            self.assertEqual("gpt_technology", technology_item.route)
            ordered = pipeline._ranked_gpt_candidates([technology_item, company_item])
            self.assertEqual(["COMPANY", "TECH"], [item.patent.publication_number for item in ordered])
            pipeline.database.close()

    def test_technology_discovery_row_remains_pending_for_daily_digest(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "notifications.sqlite3"
            register_digest_candidate(
                database,
                {
                    "_patent_id": "TECH-1",
                    "_decision": "technology",
                    "title": "Quantum sensor",
                    "technology_categories": "量子技術",
                },
                "run-1",
            )
            rows = pending_digest_rows(database)
            self.assertEqual(1, len(rows))
            self.assertEqual("technology", rows[0]["_decision"])

    def test_daily_email_separates_company_and_technology_sections(self):
        rows = [
            {
                "_patent_id": "COMPANY-1",
                "_decision": "important",
                "company_name": "監視企業株式会社",
                "title": "Company invention",
                "publication_number": "JP1",
                "source_url": "https://example.com/1",
            },
            {
                "_patent_id": "TECH-1",
                "_decision": "technology",
                "applicants": "GLOBAL LAB",
                "technology_categories": "量子技術",
                "title": "Quantum invention",
                "publication_number": "EP2",
                "final_score": 88,
                "source_url": "https://example.com/2",
            },
        ]
        with patch(
            "patent_monitor.notifications.translate_to_japanese",
            return_value={},
        ), patch("patent_monitor.notifications.SMTPMailer.send_message") as sent:
            send_patent_email(
                rows, "smtp.example.com", 587, "sender@example.com", "password",
                "recipient@example.com", "run-1", kind="digest",
            )
        message = sent.call_args.args[0]
        body = message.get_content()
        self.assertIn("【1. 企業監視で検出した重要特許】", body)
        self.assertIn("【2. 重点技術から検出した新着特許】", body)
        self.assertLess(body.index("監視企業株式会社"), body.index("GLOBAL LAB"))
        self.assertIn("技術分野: 量子技術", body)

    def test_adaptive_gemini_threshold_does_not_move_without_new_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(
                    adaptive_gemini_escalation=True,
                    adaptive_escalation_min_samples=1,
                    gemini_escalation_threshold=85,
                ),
                [], root / "history.sqlite3", root / "out",
            )
            pipeline.adaptive_scores = [55, 60, 65]
            pipeline._finalize_adaptive_escalation()
            self.assertEqual(85, pipeline.config.gemini_escalation_threshold)
            pipeline._update_adaptive_escalation({"importance_score": 95})
            pipeline._finalize_adaptive_escalation()
            self.assertNotEqual(85, pipeline.config.gemini_escalation_threshold)
            pipeline.database.close()

    def test_ai_budget_guard_stops_calls_and_records_event(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(annual_ai_budget_jpy=2000, ai_budget_call_reserve_jpy=5),
                [], root / "history.sqlite3", root / "out",
            )
            year = date.today().strftime("%Y")
            pipeline.database.connection.execute(
                """INSERT INTO ai_cost_ledger (
                    created_at, usage_year, service, model, run_id,
                    input_tokens, output_tokens, cost_usd, cost_jpy
                ) VALUES (datetime('now'), ?, 'openai', 'test', 'old-run', 0, 0, 0, 1996)""",
                (year,),
            )
            pipeline.database.connection.commit()
            self.assertFalse(pipeline._budget_allows_call("new-run", "openai"))
            self.assertTrue(pipeline.ai_budget_stopped)
            event_count = pipeline.database.connection.execute(
                "SELECT COUNT(*) FROM ai_budget_events WHERE usage_year = ?", (year,)
            ).fetchone()[0]
            self.assertEqual(event_count, 1)
            pipeline.database.close()

    def test_ranked_ai_budget_stop_keeps_algorithm_result(self):
        review = {
            "importance_score": 90,
            "materiality_score": 90,
            "short_term_market_impact_score": 90,
            "long_term_business_value_score": 90,
            "novelty_score": 90,
            "decision": "important",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            company = Company("C1", "TEST COMPANY")
            patent = PatentRecord(
                source="test", publication_number="JP1", title="test patent",
                applicants=["TEST COMPANY"],
            )
            config = PipelineConfig(
                ranked_ai_enabled=True,
                ranked_gpt_top_rate=1,
                ranked_gpt_audit_rate=0,
                annual_ai_budget_jpy=0,
            )
            with patch.object(OpenAIReviewer, "review", return_value=(review, 100, 50)) as mocked:
                result = PatentPipeline(
                    config, [company], root / "history.sqlite3", root / "out",
                    openai_api_key="openai-test",
                ).run([patent], "budget-stop-test")
            mocked.assert_not_called()
            self.assertTrue(result["ai_budget_stopped"])
            self.assertEqual(result["scored_count"], 1)
            with Path(result["evaluation_csv"]).open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["route"], "ai_budget_stopped")

    def test_ai_budget_reserve_expands_when_model_prices_increase(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PatentPipeline(
                PipelineConfig(
                    ai_budget_call_reserve_jpy=5,
                    gemini_input_usd_per_million=100,
                    gemini_output_usd_per_million=100,
                    ai_usd_jpy_rate=160,
                ),
                [], root / "history.sqlite3", root / "out",
            )
            self.assertGreater(pipeline._estimated_ai_call_ceiling_jpy("gemini"), 5)
            pipeline.database.close()


if __name__ == "__main__":
    unittest.main()
