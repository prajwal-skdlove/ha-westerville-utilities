"""Tests for the month-anchor helper used when importing statistics.

Pure logic -- needs Home Assistant's `dt_util`/`StatisticData` importable
but no `hass` fixture or recorder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.recorder.models import StatisticData
from homeassistant.util import dt as dt_util

from custom_components.westerville_utilities.coordinator import _with_month_anchors


@pytest.fixture(autouse=True)
def eastern_time_zone():
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("America/New_York"))
    yield
    dt_util.set_default_time_zone(original)


def _row(start: datetime, state: float, total: float) -> StatisticData:
    return StatisticData(start=start, state=state, sum=total)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def test_fills_empty_months_with_local_midnight_anchors_carrying_the_previous_sum() -> None:
    stats = [_row(_utc(2026, 9, 22), 130.0, 2407.0), _row(_utc(2026, 12, 22), 90.0, 2497.0)]

    result = _with_month_anchors(stats, None, 0.0)

    assert [(r["start"], r["state"], r["sum"]) for r in result] == [
        (_utc(2026, 9, 22), 130.0, 2407.0),
        # Oct 1 and Nov 1 are 00:00 EDT (04:00 UTC); Dec 1 is 00:00 EST (05:00 UTC).
        (_utc(2026, 10, 1, 4), 0.0, 2407.0),
        (_utc(2026, 11, 1, 4), 0.0, 2407.0),
        (_utc(2026, 12, 1, 5), 0.0, 2407.0),
        (_utc(2026, 12, 22), 90.0, 2497.0),
    ]


def test_incremental_import_anchors_the_gap_after_the_last_imported_row() -> None:
    prior_start = _utc(2026, 9, 22)
    stats = [_row(_utc(2026, 10, 22), 100.0, 2507.0)]

    result = _with_month_anchors(stats, prior_start.timestamp(), 2407.0)

    assert [(r["start"], r["sum"]) for r in result] == [
        (_utc(2026, 10, 1, 4), 2407.0),
        (_utc(2026, 10, 22), 2507.0),
    ]


def test_never_adds_an_anchor_after_the_newest_row() -> None:
    # An anchor later than the newest real row would become the "last
    # imported" row and make the next refresh skip real readings before it.
    stats = [_row(_utc(2026, 10, 22), 100.0, 2507.0)]

    assert _with_month_anchors(stats, None, 0.0) == stats
    assert _with_month_anchors(stats, _utc(2026, 10, 3).timestamp(), 2407.0) == stats


def test_real_row_at_the_anchor_instant_wins() -> None:
    stats = [
        _row(_utc(2026, 9, 22), 130.0, 2407.0),
        _row(_utc(2026, 10, 1, 4), 5.0, 2412.0),
        _row(_utc(2026, 10, 22), 100.0, 2512.0),
    ]

    assert _with_month_anchors(stats, None, 0.0) == stats


def test_empty_input_is_returned_unchanged() -> None:
    assert _with_month_anchors([], None, 12.0) == []
