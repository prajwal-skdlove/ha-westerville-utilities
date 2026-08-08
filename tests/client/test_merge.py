"""Tests for merge_readings: the multi-granularity reading merge + unit
normalization that feeds statistics import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from client.merge import merge_readings
from client.models import Granularity, Reading

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _reading(granularity: Granularity, start: datetime, end: datetime, value: float, unit: str) -> Reading:
    return Reading(meter_id="m1", granularity=granularity, period_start=start, period_end=end, value=value, unit=unit)


def test_finer_granularity_overrides_coarser_for_overlapping_period():
    monthly = _reading(Granularity.MONTHLY, BASE, BASE + timedelta(days=31), 10.0, "CCF")
    dailies = [
        _reading(Granularity.DAILY, BASE + timedelta(days=25 + i), BASE + timedelta(days=26 + i), 20.0, "CF")
        for i in range(5)
    ]
    hourlies = [
        _reading(
            Granularity.HOURLY,
            BASE + timedelta(days=30, hours=h),
            BASE + timedelta(days=30, hours=h + 1),
            1.0,
            "CF",
        )
        for h in range(24)
    ]

    merged = merge_readings([monthly, *dailies, *hourlies])

    # Monthly is fully excluded: something finer overlaps every day of it.
    assert not any(r.granularity == Granularity.MONTHLY for r in merged)
    # The 24 hours of day 30 win over the daily reading covering that day.
    day_30 = [r for r in merged if r.period_start.date() == (BASE + timedelta(days=30)).date()]
    assert len(day_30) == 24
    assert all(r.granularity == Granularity.HOURLY for r in day_30)
    # Days 25-29 remain daily; only day 30 was claimed by the hourly data.
    daily_entries = [r for r in merged if r.granularity == Granularity.DAILY]
    assert len(daily_entries) == 5


def test_output_is_sorted_by_period_start_regardless_of_input_order():
    readings = [
        _reading(Granularity.DAILY, BASE + timedelta(days=2), BASE + timedelta(days=3), 1.0, "kWh"),
        _reading(Granularity.DAILY, BASE, BASE + timedelta(days=1), 1.0, "kWh"),
        _reading(Granularity.DAILY, BASE + timedelta(days=1), BASE + timedelta(days=2), 1.0, "kWh"),
    ]
    merged = merge_readings(readings)
    assert [r.period_start for r in merged] == sorted(r.period_start for r in merged)


def test_ccf_normalized_to_cf_at_100x():
    monthly = _reading(Granularity.MONTHLY, BASE, BASE + timedelta(days=31), 10.0, "CCF")
    merged = merge_readings([monthly])
    assert len(merged) == 1
    assert merged[0].unit == "CF"
    assert merged[0].value == 1000.0


def test_kwh_casing_normalized():
    for raw_unit in ("KWH", "kWh", "kwh"):
        reading = _reading(Granularity.MONTHLY, BASE, BASE + timedelta(days=31), 500.0, raw_unit)
        merged = merge_readings([reading])
        assert merged[0].unit == "kWh", f"unit {raw_unit!r} did not normalize"


def test_non_overlapping_readings_are_all_kept():
    readings = [
        _reading(Granularity.MONTHLY, BASE, BASE + timedelta(days=31), 100.0, "kWh"),
        _reading(Granularity.MONTHLY, BASE + timedelta(days=31), BASE + timedelta(days=62), 110.0, "kWh"),
    ]
    merged = merge_readings(readings)
    assert len(merged) == 2
    assert [r.value for r in merged] == [100.0, 110.0]


def test_empty_input_returns_empty_output():
    assert merge_readings([]) == []
