"""Reading fetch for the Westerville portal: monthly billed usage from the
`consumptionInquiry` table, and daily/hourly advanced-meter (AMI) usage from
`smartMeterConsumV3`.

Ported from `utility-reader`'s `providers/westerville/usage.py`, adapted for
Home Assistant's coordinator model: instead of a local SQLite `sync_state`
table tracking resume points, the coordinator passes `since`/`backfill`
based on Home Assistant's own long-term statistics (see coordinator.py).

Both endpoints need to be told *which* meter within the account/inquiryType
to return data for -- an account can have more than one meter per utility
(confirmed live: a main "Electric" meter plus a separate "EV CHARGE"
submeter). Monthly billed usage is selected via a `compareAccounts` query
param; AMI usage via `selectedMeterId`. Critically, `selectedMeterId` for a
meter with no AMI registration isn't rejected -- the portal silently falls
back to whatever meter was last active in the session and returns *that*
meter's data. Every AMI fetch here verifies the response's subtitle actually
reports the meter we asked for before trusting it.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from .accounts import inquiry_type_of
from .models import Granularity, Meter, Reading
from .shared import (
    CONSUMPTION_REPORT_BY_INQUIRY_TYPE,
    MAX_DAILY_SPAN_DAYS,
    get,
    is_error_page,
    parse_ami_fragment,
    subtitle_meter_id,
)

_LOGGER = logging.getLogger(__name__)

# Some meters (water, in particular) report AMI data a few days behind
# "now", so the most recent window(s) can legitimately be empty even though
# older data exists. Tolerate a run of empty windows before concluding we've
# reached the true start of the meter's history and stopping the backward
# walk.
MAX_CONSECUTIVE_EMPTY_WINDOWS = 5

# On incremental (non-backfill) polls, only look this far back for
# daily/hourly AMI data -- enough to catch the portal's usual reporting lag
# and any corrections, without re-walking the meter's whole history.
INCREMENTAL_DAILY_LOOKBACK_DAYS = 10
INCREMENTAL_HOURLY_LOOKBACK_DAYS = 5

# Hard caps on how far a *backfill* walk can go, independent of
# MAX_CONSECUTIVE_EMPTY_WINDOWS. The empty-window heuristic alone isn't
# enough here: Home Assistant runs the first backfill synchronously inside
# config entry setup, which has its own ~10 minute timeout. If the
# heuristic doesn't trigger quickly (sparse/gappy data further back than
# expected), a purely reactive "walk until empty" turned into hundreds of
# sequential requests across multiple meters in practice and got the whole
# setup cancelled mid-request. These caps bound the worst case regardless
# of what the heuristic does, mirroring how Home Assistant's own Opower
# integration hard-caps backfill depth (3 years daily, 2 months hourly)
# rather than relying on a portal signal to know when to stop.
BACKFILL_DAILY_LOOKBACK_DAYS = 400  # >9 months of observed daily retention, with margin
BACKFILL_HOURLY_LOOKBACK_DAYS = 30  # >2 weeks of observed hourly retention, with margin


def _consumption_column_indices(table) -> tuple[int | None, int | None, int | None, str]:
    """Map header names to column indices.

    The `consumptionTable` layout differs by inquiryType (confirmed live):
    hydro has a "Type" column and a single "Usage in kWh"/"Amount$" pair;
    water has no "Type" column and splits "Water Amount $"/"Sewer Amount $"
    instead, which shifts every later column over by one. Reading by header
    name instead of a fixed index is what makes this work for both.
    """
    thead = table.find("thead")
    headers = [c.get_text(strip=True) for c in thead.find_all(["th", "td"])] if thead else []

    date_idx = days_idx = usage_idx = None
    unit = "unknown"
    for i, text in enumerate(headers):
        lower = text.lower()
        if lower == "date":
            date_idx = i
        elif lower == "days":
            days_idx = i
        elif lower.startswith("usage in "):
            usage_idx = i
            unit = text[len("Usage in ") :].strip() or "unknown"

    return date_idx, days_idx, usage_idx, unit


async def fetch_monthly(client: httpx.AsyncClient, meter: Meter) -> list[Reading]:
    """Fetch the meter's full monthly billed-usage history (one request)."""
    inquiry_type = inquiry_type_of(meter)
    report = CONSUMPTION_REPORT_BY_INQUIRY_TYPE.get(inquiry_type, "")
    compare_accounts = f"{inquiry_type}_{meter.account_id}_{meter.meter_id}"
    query = (
        f"/app/capricorn?para=consumptionInquiry&inquiryType={inquiry_type}"
        f"&tab=probe&compareAccounts={compare_accounts}"
    )
    if report:
        query += f"&report={report}"
    resp = await get(client, query)

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="consumptionTable")
    if table is None:
        _LOGGER.debug("No consumption table for meter %s; nothing to sync", meter.meter_id)
        return []

    date_idx, days_idx, usage_idx, unit = _consumption_column_indices(table)
    if date_idx is None or usage_idx is None:
        _LOGGER.warning(
            "Meter %s: consumption table header missing Date/Usage columns; skipping", meter.meter_id
        )
        return []

    rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
    readings: list[Reading] = []
    skipped_other_meter = 0
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) <= max(date_idx, usage_idx, days_idx or 0):
            _LOGGER.debug("Skipping unparseable billed-usage row (too few cells): %s", cells)
            continue
        if cells[0] != meter.meter_id:
            # Defense in depth: compareAccounts should already scope rows to
            # this meter, but don't silently attribute another meter's row
            # to this one if the portal ever ignores the param.
            skipped_other_meter += 1
            continue
        try:
            period_end = datetime.strptime(cells[date_idx], "%b %d, %Y").replace(tzinfo=UTC)
            days = int(cells[days_idx].replace(",", "") or 0) if days_idx is not None else 0
            value = float(cells[usage_idx].replace(",", "").replace("$", ""))
        except ValueError:
            _LOGGER.debug("Skipping unparseable billed-usage row: %s", cells)
            continue
        period_start = period_end - timedelta(days=days) if days else period_end
        readings.append(
            Reading(
                meter_id=meter.meter_id,
                granularity=Granularity.MONTHLY,
                period_start=period_start,
                period_end=period_end,
                value=value,
                unit=unit,
            )
        )

    if skipped_other_meter:
        _LOGGER.warning(
            "Meter %s: %d billed-usage row(s) belonged to a different meter; skipped",
            meter.meter_id, skipped_other_meter,
        )
    _LOGGER.debug("Meter %s: parsed %d monthly billed-usage reading(s)", meter.meter_id, len(readings))
    return readings


def _verified_ami_fragment(
    resp_text: str, meter: Meter
) -> tuple[list[str], list[float | None], str] | None:
    """parse_ami_fragment(), but rejects responses whose subtitle reports a
    different meter (the session-fallback failure mode described above)."""
    if subtitle_meter_id(resp_text) != meter.meter_id:
        _LOGGER.warning(
            "Meter %s: AMI response reported a different meter (session fallback); discarding", meter.meter_id
        )
        return None
    return parse_ami_fragment(resp_text)


async def _fetch_daily_window(
    client: httpx.AsyncClient,
    meter: Meter,
    inquiry_type: str,
    window_start: date,
    window_end: date,
) -> list[Reading] | None:
    query = (
        f"/app/capricorn?para=smartMeterConsumV3&inquiryType={inquiry_type}&type=daily"
        f"&dailyFromDate={window_start.isoformat()}&dailyToDate={window_end.isoformat()}"
        f"&tab=SMCONSUM&selectedMeterId={meter.meter_id}"
    )
    resp = await get(client, query)
    if is_error_page(resp.text):
        _LOGGER.warning(
            "Meter %s: daily AMI request rejected for %s -> %s (span too wide?)",
            meter.meter_id, window_start, window_end,
        )
        return None
    parsed = _verified_ami_fragment(resp.text, meter)
    if parsed is None:
        return None

    labels, values, unit = parsed
    readings: list[Reading] = []
    for label, value in zip(labels, values):
        if value is None:
            continue
        try:
            day_start = datetime.strptime(label, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            _LOGGER.debug("Skipping unparseable daily AMI label: %r", label)
            continue
        readings.append(
            Reading(
                meter_id=meter.meter_id,
                granularity=Granularity.DAILY,
                period_start=day_start,
                period_end=day_start + timedelta(days=1),
                value=value,
                unit=unit,
            )
        )
    return readings


async def fetch_daily(
    client: httpx.AsyncClient,
    meter: Meter,
    *,
    since: datetime | None,
    backfill: bool,
) -> list[Reading]:
    """Fetch daily AMI readings.

    If `backfill` is True, walks backward in MAX_DAILY_SPAN_DAYS-day windows
    from now until a run of empty windows confirms the meter's real
    AMI-enabled history boundary, or BACKFILL_DAILY_LOOKBACK_DAYS is
    reached -- whichever comes first. The hard cap matters because this
    runs synchronously during Home Assistant's config entry setup (which
    has its own timeout); it can't rely solely on the portal eventually
    returning an empty window. Otherwise fetches a single bounded recent
    window (`since`, or the last INCREMENTAL_DAILY_LOOKBACK_DAYS days), which
    is normally just one request on an incremental poll.
    """
    inquiry_type = inquiry_type_of(meter)
    end = datetime.now(UTC)

    if not backfill:
        start = since or (end - timedelta(days=INCREMENTAL_DAILY_LOOKBACK_DAYS))
        readings = await _fetch_daily_window(client, meter, inquiry_type, start.date(), end.date())
        return readings or []

    span = timedelta(days=MAX_DAILY_SPAN_DAYS)
    earliest = end - timedelta(days=BACKFILL_DAILY_LOOKBACK_DAYS)
    readings: list[Reading] = []
    consecutive_empty = 0
    cursor_end = end
    while cursor_end > earliest:
        cursor_start = max(earliest, cursor_end - span)
        window = await _fetch_daily_window(client, meter, inquiry_type, cursor_start.date(), cursor_end.date())
        if window is None:
            consecutive_empty += 1
            _LOGGER.debug(
                "Meter %s: no daily AMI data for %s -> %s (%d/%d consecutive empty)",
                meter.meter_id, cursor_start.date(), cursor_end.date(), consecutive_empty, MAX_CONSECUTIVE_EMPTY_WINDOWS,
            )
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY_WINDOWS:
                break
        else:
            consecutive_empty = 0
            readings.extend(window)
        cursor_end = cursor_start

    _LOGGER.debug("Meter %s: parsed %d daily AMI reading(s) (backfill)", meter.meter_id, len(readings))
    return readings


async def _fetch_hourly_day(
    client: httpx.AsyncClient, meter: Meter, inquiry_type: str, day: date
) -> list[Reading] | None:
    query = (
        f"/app/capricorn?para=smartMeterConsumV3&inquiryType={inquiry_type}&type=hourly"
        f"&day={day.isoformat()}&tab=SMCONSUM&selectedMeterId={meter.meter_id}"
    )
    resp = await get(client, query)
    if is_error_page(resp.text):
        _LOGGER.warning("Meter %s: hourly AMI request rejected for %s", meter.meter_id, day)
        return None
    parsed = _verified_ami_fragment(resp.text, meter)
    if parsed is None:
        return None

    _labels, values, unit = parsed
    base = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    readings: list[Reading] = []
    for hour_index, value in enumerate(values):
        if value is None:
            continue
        period_start = base + timedelta(hours=hour_index)
        readings.append(
            Reading(
                meter_id=meter.meter_id,
                granularity=Granularity.HOURLY,
                period_start=period_start,
                period_end=period_start + timedelta(hours=1),
                value=value,
                unit=unit,
            )
        )
    return readings


async def fetch_hourly(
    client: httpx.AsyncClient,
    meter: Meter,
    *,
    since: datetime | None,
    backfill: bool,
) -> list[Reading]:
    """Fetch hourly AMI readings, walking backward day by day.

    Hourly AMI history is short-lived (roughly 2 weeks), so
    MAX_CONSECUTIVE_EMPTY_WINDOWS is expected to stop the walk well before
    BACKFILL_HOURLY_LOOKBACK_DAYS is reached -- but the hard cap is what
    actually guarantees a bounded first-refresh time if that heuristic
    doesn't trigger as expected (see BACKFILL_HOURLY_LOOKBACK_DAYS).
    """
    inquiry_type = inquiry_type_of(meter)
    end_date = datetime.now(UTC).date()
    if backfill:
        start_date = end_date - timedelta(days=BACKFILL_HOURLY_LOOKBACK_DAYS)
    else:
        start_date = (since or datetime.now(UTC) - timedelta(days=INCREMENTAL_HOURLY_LOOKBACK_DAYS)).date()

    readings: list[Reading] = []
    consecutive_empty = 0
    cursor = end_date
    while cursor >= start_date:
        day_readings = await _fetch_hourly_day(client, meter, inquiry_type, cursor)
        if day_readings is None:
            consecutive_empty += 1
            _LOGGER.debug(
                "Meter %s: no hourly AMI data for %s (%d/%d consecutive empty)",
                meter.meter_id, cursor, consecutive_empty, MAX_CONSECUTIVE_EMPTY_WINDOWS,
            )
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY_WINDOWS:
                break
        else:
            consecutive_empty = 0
            readings.extend(day_readings)
        cursor -= timedelta(days=1)

    _LOGGER.debug("Meter %s: parsed %d hourly AMI reading(s)", meter.meter_id, len(readings))
    return readings
