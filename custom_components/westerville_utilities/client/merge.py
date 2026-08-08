"""Merge multi-granularity Westerville readings into one time-ordered series.

Home Assistant's Energy dashboard wants a single continuous statistic per
meter, not one series per granularity. We combine monthly (long history),
daily (~9 months retention observed), and hourly (~2 weeks retention
observed) readings for a meter, letting finer-granularity readings override
coarser ones for any period they overlap -- mirroring how Home Assistant's
own Opower integration merges monthly/daily/hourly cost reads (see
`_update_with_finer_cost_reads` in
`homeassistant/components/opower/coordinator.py`).

Trade-off: if finer data only *partially* covers a coarser period (e.g. a
meter's AMI registration started mid-month), the coarser reading is dropped
entirely rather than split, leaving a small gap for the uncovered portion.
That's preferable to the alternative of double-counting usage in the Energy
dashboard by keeping both.
"""

from __future__ import annotations

from .models import GRANULARITY_FINENESS, Reading

# Water is reported in CF (daily) and CCF (monthly) by the same meter --
# confirmed against real synced data (CCF = 100 CF). Electric is reported as
# both "KWH" and "kWh" depending on the page. Normalize before merging so
# the series is unit-consistent.
_CCF_TO_CF = 100.0


def _normalized(reading: Reading) -> Reading:
    unit = reading.unit.strip().upper()
    if unit == "CCF":
        return Reading(
            meter_id=reading.meter_id,
            granularity=reading.granularity,
            period_start=reading.period_start,
            period_end=reading.period_end,
            value=reading.value * _CCF_TO_CF,
            unit="CF",
        )
    if unit == "KWH":
        return Reading(
            meter_id=reading.meter_id,
            granularity=reading.granularity,
            period_start=reading.period_start,
            period_end=reading.period_end,
            value=reading.value,
            unit="kWh",
        )
    return reading


def merge_readings(readings: list[Reading]) -> list[Reading]:
    """Merge readings across granularities into one non-overlapping series.

    For any two readings whose periods overlap, the finer-granularity one
    wins. Input order doesn't matter; output is sorted by period_start.
    """
    normalized = [_normalized(r) for r in readings]
    # Finer granularity first, so it "claims" its period before coarser
    # readings covering the same time are considered.
    normalized.sort(key=lambda r: (GRANULARITY_FINENESS[r.granularity], r.period_start))

    accepted: list[Reading] = []
    covered: list[tuple] = []  # (start, end) intervals already claimed by finer data

    def _overlaps(start, end) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in covered)

    for reading in normalized:
        if _overlaps(reading.period_start, reading.period_end):
            continue
        accepted.append(reading)
        covered.append((reading.period_start, reading.period_end))

    accepted.sort(key=lambda r: r.period_start)
    return accepted
