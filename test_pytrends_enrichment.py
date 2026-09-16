from __future__ import annotations

from datetime import datetime

import pandas as pd

from pytrends_enrichment import build_query, summarize_series, timeframe_from_months


def test_build_query_removes_legal_form() -> None:
    assert build_query("株式会社 任天堂", "株価") == "任天堂 株価"


def test_timeframe_has_explicit_dates() -> None:
    assert timeframe_from_months(12, datetime(2026, 7, 15)) == "2025-07-08 2026-07-15"


def test_summarize_series_ignores_partial_latest_period() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-05-03", periods=9, freq="W"),
            "value": [10, 20, 30, 40, 50, 60, 70, 80, 100],
            "isPartial": [False] * 8 + [True],
        }
    )
    result = summarize_series("任天堂 株価", frame)
    assert result.status == "ok"
    assert result.points == 8
    assert result.latest_value == 80.0
    assert result.recent_4_period_avg == 65.0
    assert result.previous_4_period_avg == 25.0
    assert result.recent_change_pct == 160.0
    assert result.max_value == 80.0
    assert result.latest_vs_peak_ratio == 1.0
    assert result.is_partial_latest is True


def test_empty_series_is_reported() -> None:
    result = summarize_series("テスト 株価", pd.DataFrame())
    assert result.status == "no_data"
    assert result.error
