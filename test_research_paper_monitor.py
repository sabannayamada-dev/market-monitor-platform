from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research_paper_monitor.collectors import RateLimited, _get, collect_arxiv, parse_arxiv_feed, parse_jstage_feed, parse_openalex_work, reconstruct_abstract
from research_paper_monitor.database import PaperDatabase, normalize_arxiv_id, normalize_doi
from research_paper_monitor.models import PaperRecord
from research_paper_monitor.notifications import build_digest
from research_paper_monitor.scoring import score_paper
from research_paper_monitor.service import run_daily_service
from research_paper_monitor.rate_limit import cooldown_until, load_state, on_rate_limited, on_success


ROOT = Path(__file__).resolve().parent


def sample_record(source: str = "openalex", source_id: str = "W1", doi: str = "10.1000/test") -> PaperRecord:
    return PaperRecord(
        source=source,
        source_id=source_id,
        title="Low-loss vertical diamond MOSFET for power electronics",
        abstract="We demonstrate microwave plasma CVD, boron doping, device fabrication, high voltage reliability and low on-resistance.",
        authors=("A. Researcher", "B. Engineer"),
        published_at="2026-09-05",
        updated_at="2026-09-05T01:00:00Z",
        doi=doi,
        journal="Journal of Device Research",
        categories=("Materials Science", "Applied Physics"),
        landing_url="https://example.test/paper",
        pdf_url="https://example.test/paper.pdf",
        raw={"id": source_id},
    )


class CollectorTests(unittest.TestCase):
    def test_http_406_is_a_short_rate_limit_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://export.arxiv.org/api/query?very-long-query=secret",
            406,
            "Not Acceptable",
            {},
            None,
        )
        with patch("research_paper_monitor.collectors.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RateLimited) as raised:
                _get("https://export.arxiv.org/api/query?very-long-query=secret", 10, "test-agent")
        self.assertEqual(406, raised.exception.status_code)
        self.assertEqual("HTTP 406", str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))

    def test_reconstruct_openalex_abstract(self) -> None:
        self.assertEqual("A compact antenna", reconstruct_abstract({"antenna": [2], "A": [0], "compact": [1]}))

    def test_parse_openalex_work(self) -> None:
        record = parse_openalex_work({
            "id": "https://openalex.org/W123", "title": "Metasurface antenna",
            "doi": "https://doi.org/10.1/demo", "publication_date": "2026-09-01",
            "abstract_inverted_index": {"New": [0], "device": [1]},
            "authorships": [{"author": {"display_name": "Ada Example"}}],
            "primary_location": {"landing_page_url": "https://example.test", "source": {"display_name": "Example Journal"}},
            "topics": [{"display_name": "Metasurfaces", "subfield": {"display_name": "Optics"}}],
            "ids": {"arxiv": "https://arxiv.org/abs/2609.00001"},
        })
        self.assertEqual("W123", record.source_id)
        self.assertEqual("New device", record.abstract)
        self.assertEqual("2609.00001", record.arxiv_id)

    def test_parse_arxiv_feed(self) -> None:
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry><id>https://arxiv.org/abs/2609.00001v2</id><updated>2026-09-05T00:00:00Z</updated>
          <published>2026-09-04T00:00:00Z</published><title> A new antenna </title><summary> Useful work. </summary>
          <author><name>Ada Example</name></author><category term="eess.SP"/>
          <link href="https://arxiv.org/pdf/2609.00001v2" type="application/pdf"/><arxiv:doi>10.1/demo</arxiv:doi></entry>
        </feed>'''
        records = parse_arxiv_feed(xml)
        self.assertEqual(1, len(records))
        self.assertEqual("2609.00001", records[0].arxiv_id)
        self.assertEqual("A new antenna", records[0].title)
        self.assertEqual(("eess.SP",), records[0].categories)

    def test_parse_jstage_feed_prefers_japanese_metadata(self) -> None:
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
          <result><status>0</status></result>
          <entry>
            <article_title><ja>ダイヤモンドMOSFETの高耐圧化</ja><en>High-voltage diamond MOSFET</en></article_title>
            <article_link><ja>https://www.jstage.jst.go.jp/article/example/1/1/1/_article/-char/ja</ja></article_link>
            <author><ja><name>研究 太郎</name><name>技術 花子</name></ja><en><name>Taro Kenkyu</name><name>Hanako Gijutsu</name></en></author>
            <material_title><ja>応用物理</ja><en>Applied Physics</en></material_title>
            <prism:doi>10.1234/example.1</prism:doi><joi>JST.JSTAGE/example.1</joi>
            <updated>2026-09-05T00:00:00Z</updated>
          </entry>
        </feed>'''.encode("utf-8")
        records = parse_jstage_feed(xml)
        self.assertEqual(1, len(records))
        self.assertEqual("ダイヤモンドMOSFETの高耐圧化", records[0].title)
        self.assertEqual(("研究 太郎", "技術 花子"), records[0].authors)
        self.assertEqual("10.1234/example.1", records[0].doi)
        self.assertEqual("jstage", records[0].source)

    def test_jstage_rate_limit_status_uses_common_cooldown_path(self) -> None:
        with self.assertRaises(RateLimited) as caught:
            parse_jstage_feed(b"<feed><result><status>ERR_003</status><message>limit</message></result></feed>")
        self.assertEqual(429, caught.exception.status_code)


class DatabaseAndScoringTests(unittest.TestCase):
    def test_identifier_normalization(self) -> None:
        self.assertEqual("10.1000/test", normalize_doi("https://doi.org/10.1000/TEST"))
        self.assertEqual("2609.00001", normalize_arxiv_id("https://arxiv.org/abs/2609.00001v3"))

    def test_cross_source_doi_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = PaperDatabase(Path(temporary) / "papers.sqlite3")
            first_id, first_status = database.upsert_paper(sample_record(), "2026-09-05T00:00:00+00:00")
            second_id, second_status = database.upsert_paper(sample_record("arxiv", "2609.1"), "2026-09-05T01:00:00+00:00")
            self.assertEqual("inserted", first_status)
            self.assertEqual("merged", second_status)
            self.assertEqual(first_id, second_id)

    def test_diamond_power_profile_scores_high(self) -> None:
        config = json.loads((ROOT / "research_paper_config.json").read_text(encoding="utf-8"))
        paper = {**sample_record().__dict__, "paper_id": "p1", "authors": list(sample_record().authors), "categories": list(sample_record().categories)}
        score = score_paper(paper, config["profiles"])
        self.assertEqual("diamond_power_electronics", score.profile_id)
        self.assertGreaterEqual(score.score, 70)

    def test_pdf_derived_radio_and_quantum_profiles_score_high(self) -> None:
        config = json.loads((ROOT / "research_paper_config.json").read_text(encoding="utf-8"))
        radio = {
            **sample_record().__dict__, "paper_id": "radio", "title": "Near-field measurement for electromagnetic interference source localization",
            "abstract": "A correlation measurement system visualizes an electromagnetic field around a small satellite instrument.",
            "authors": [], "categories": ["Electrical Engineering", "Instrumentation"],
        }
        quantum = {
            **sample_record().__dict__, "paper_id": "quantum", "title": "Diamond NV center quantum sensor for intracellular sensing",
            "abstract": "A room temperature nanodiamond sensor enables high sensitivity biomedical sensing and quantum information experiments.",
            "authors": [], "categories": ["Quantum Physics", "Applied Physics"],
        }
        self.assertEqual("radio_space_engineering", score_paper(radio, config["profiles"]).profile_id)
        self.assertGreaterEqual(score_paper(radio, config["profiles"]).score, 70)
        self.assertEqual("diamond_quantum_interfaces", score_paper(quantum, config["profiles"]).profile_id)
        self.assertGreaterEqual(score_paper(quantum, config["profiles"]).score, 70)

    def test_japanese_jstage_titles_qualify(self) -> None:
        config = json.loads((ROOT / "research_paper_config.json").read_text(encoding="utf-8"))
        paper = {
            **sample_record(source="jstage", source_id="J1").__dict__, "paper_id": "j1",
            "title": "高耐圧縦型ダイヤモンドMOSFETの作製",
            "abstract": "",
            "authors": [], "categories": ["J-STAGE"],
        }
        score = score_paper(paper, config["profiles"])
        self.assertEqual("diamond_power_electronics", score.profile_id)
        self.assertGreaterEqual(score.score, 50)

    def test_digest_contains_jstage_attribution(self) -> None:
        _subject, body = build_digest([], {"sources_attempted": 3, "sources_succeeded": 3}, "2026-09-01 ～ 2026-09-05", [])
        self.assertIn("情報提供元: J-STAGE", body)
        self.assertIn("https://www.jstage.jst.go.jp/", body)

    def test_previous_generic_topics_no_longer_qualify(self) -> None:
        config = json.loads((ROOT / "research_paper_config.json").read_text(encoding="utf-8"))
        old_topic = {
            **sample_record().__dict__, "paper_id": "old", "title": "Metasurface antenna beamforming for wireless networks",
            "abstract": "A generic millimeter wave communication study.", "authors": [],
            "categories": ["Telecommunications", "Optics"],
        }
        self.assertLess(score_paper(old_topic, config["profiles"]).score, 50)

    def test_generic_power_and_dft_papers_require_diamond_anchor(self) -> None:
        config = json.loads((ROOT / "research_paper_config.json").read_text(encoding="utf-8"))
        generic = {
            **sample_record().__dict__, "paper_id": "generic",
            "title": "Efficient power electronics from density functional theory",
            "abstract": "A high sensitivity simulation with carrier mobility and reliability.",
            "authors": [], "categories": ["Materials Science", "Applied Physics"],
        }
        score = score_paper(generic, config["profiles"])
        self.assertEqual(0, score.score)

    def test_rate_limit_cooldown_persists_and_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = PaperDatabase(Path(temporary) / "papers.sqlite3")
            now = datetime(2026, 9, 5, tzinfo=timezone.utc)
            settings = {"initial_cooldown_hours": 6, "max_cooldown_hours": 48}
            first = on_rate_limited(database, "arxiv", now, None, settings)
            self.assertEqual(1, first["consecutive_429"])
            self.assertIsNotNone(cooldown_until(database, "arxiv", now))
            second = on_rate_limited(database, "arxiv", now, 7 * 3600, settings)
            self.assertEqual(2, second["consecutive_429"])
            self.assertEqual("2026-09-05T12:00:00+00:00", second["cooldown_until"])
            on_success(database, "arxiv", now)
            self.assertEqual(0, load_state(database, "arxiv")["consecutive_429"])

    def test_rate_limit_supports_twenty_minute_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = PaperDatabase(Path(temporary) / "papers.sqlite3")
            now = datetime(2026, 9, 5, tzinfo=timezone.utc)
            settings = {"initial_cooldown_minutes": 20, "max_cooldown_minutes": 120}
            first = on_rate_limited(database, "arxiv", now, None, settings)
            self.assertEqual("2026-09-05T00:20:00+00:00", first["cooldown_until"])
            second = on_rate_limited(database, "arxiv", now, None, settings)
            self.assertEqual("2026-09-05T00:40:00+00:00", second["cooldown_until"])

    def test_rate_limit_can_remain_fixed_at_twenty_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = PaperDatabase(Path(temporary) / "papers.sqlite3")
            now = datetime(2026, 9, 5, tzinfo=timezone.utc)
            settings = {"initial_cooldown_minutes": 20, "max_cooldown_minutes": 20}
            on_rate_limited(database, "arxiv", now, None, settings)
            second = on_rate_limited(database, "arxiv", now, None, settings)
            self.assertEqual("2026-09-05T00:20:00+00:00", second["cooldown_until"])


class ServiceTests(unittest.TestCase):
    def test_arxiv_waits_twenty_minutes_and_retries_once(self) -> None:
        config = {
            "sources": {"arxiv": {
                "enabled": True,
                "base_url": "https://export.arxiv.org/api/query",
                "max_results_per_profile": 10,
                "request_spacing_seconds": 10,
                "retry_after_cooldown_once": True,
                "retry_wait_minutes": 20,
            }}
        }
        profiles = [{"arxiv_categories": ["quant-ph"], "arxiv_queries": ["diamond sensor"]}]
        limited = RateLimited(406, "HTTP 406")
        with patch("research_paper_monitor.collectors._get", side_effect=[limited, b'<feed xmlns="http://www.w3.org/2005/Atom"/>']) as get, patch(
            "research_paper_monitor.collectors.time.sleep"
        ) as sleep:
            records = collect_arxiv(
                config, profiles,
                datetime(2026, 9, 8, tzinfo=timezone.utc),
                datetime(2026, 9, 15, tzinfo=timezone.utc),
            )
        self.assertEqual([], records)
        self.assertEqual(2, get.call_count)
        sleep.assert_called_once_with(1200.0)

    def test_stage_two_backfills_stage_one_unscored_papers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "papers.sqlite3"
            environment = {
                "PAPER_CONFIG_PATH": str(ROOT / "research_paper_config.json"),
                "PAPER_STATE_DB": str(database_path),
            }

            def openalex(*_args):
                return [sample_record()]

            def arxiv(*_args):
                return []

            with patch.dict(os.environ, {**environment, "PAPER_OPERATION_STAGE": "1"}, clear=False):
                first = run_daily_service(
                    now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    collectors={"openalex": openalex, "arxiv": arxiv, "jstage": lambda *_args: []},
                )
            with patch.dict(os.environ, {**environment, "PAPER_OPERATION_STAGE": "2"}, clear=False):
                second = run_daily_service(
                    now=datetime(2026, 9, 6, tzinfo=timezone.utc),
                    collectors={"openalex": openalex, "arxiv": arxiv, "jstage": lambda *_args: []},
                )
            self.assertEqual("baseline_completed", first["status"])
            self.assertEqual(0, first["scored"])
            self.assertEqual(1, second["scored"])
            self.assertEqual(0, second["candidates"])

    def test_first_run_is_baseline_and_second_run_only_handles_new_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "papers.sqlite3"
            environment = {
                "PAPER_CONFIG_PATH": str(ROOT / "research_paper_config.json"),
                "PAPER_STATE_DB": str(database_path),
                "PAPER_OPERATION_STAGE": "2",
            }
            calls = {"openalex": 0}

            def openalex(*_args):
                calls["openalex"] += 1
                if calls["openalex"] == 1:
                    return [sample_record()]
                return [sample_record(), sample_record(source_id="W2", doi="10.1000/new")]

            def arxiv(*_args):
                return []

            with patch.dict(os.environ, environment, clear=False):
                first = run_daily_service(
                    now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    collectors={"openalex": openalex, "arxiv": arxiv, "jstage": lambda *_args: []},
                )
                second = run_daily_service(
                    now=datetime(2026, 9, 6, tzinfo=timezone.utc),
                    collectors={"openalex": openalex, "arxiv": arxiv, "jstage": lambda *_args: []},
                )
            self.assertEqual("baseline_completed", first["status"])
            self.assertEqual(1, first["inserted"])
            self.assertEqual("completed", second["status"])
            self.assertEqual(1, second["inserted"])
            self.assertEqual(1, second["candidates"])

    def test_rate_limited_source_does_not_stop_other_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "PAPER_CONFIG_PATH": str(ROOT / "research_paper_config.json"),
                "PAPER_STATE_DB": str(Path(temporary) / "papers.sqlite3"),
                "PAPER_OPERATION_STAGE": "1",
            }

            def openalex(*_args):
                return [sample_record()]

            def arxiv(*_args):
                error = RateLimited(429, "HTTP 429", 3600)
                raise error

            with patch.dict(os.environ, environment, clear=False):
                result = run_daily_service(
                    now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    collectors={"openalex": openalex, "arxiv": arxiv, "jstage": lambda *_args: []},
                )
            self.assertEqual("baseline_completed", result["status"])
            self.assertEqual(1, result["inserted"])
            self.assertEqual(1, result["http_429"])
            self.assertEqual(2, result["sources_succeeded"])

    def test_http_406_is_counted_and_other_sources_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "PAPER_CONFIG_PATH": str(ROOT / "research_paper_config.json"),
                "PAPER_STATE_DB": str(Path(temporary) / "papers.sqlite3"),
                "PAPER_OPERATION_STAGE": "1",
            }

            def arxiv(*_args):
                raise RateLimited(406, "HTTP 406")

            with patch.dict(os.environ, environment, clear=False):
                result = run_daily_service(
                    now=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    collectors={"openalex": lambda *_args: [sample_record()], "arxiv": arxiv, "jstage": lambda *_args: []},
                )
            self.assertEqual("baseline_completed", result["status"])
            self.assertEqual(1, result["http_406"])
            self.assertEqual(0, result["http_429"])
            self.assertEqual(2, result["sources_succeeded"])
            self.assertTrue(any(error.startswith("arxiv: HTTP 406;") for error in result["errors"]))
            self.assertFalse(any("https://" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
