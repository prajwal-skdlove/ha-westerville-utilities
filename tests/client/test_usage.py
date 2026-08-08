"""Tests for client/usage.py: monthly billed usage + daily/hourly AMI fetch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from client import shared, usage
from client.models import Granularity, Meter, UtilityType

METER = Meter(account_id="104758-000001", meter_id="900000001", utility_type=UtilityType.ELECTRIC)
WATER_METER = Meter(account_id="104758-000001", meter_id="900000003", utility_type=UtilityType.WATER)

ERROR_PAGE = "<!-- errorInvalidInput.jsp --><title>Cannot Process Request</title>"


# --- fetch_monthly ---------------------------------------------------------

# Mirrors the real hydro consumptionTable layout (12 columns, includes "Type").
CONSUMPTION_TABLE = """
<html><body>
<table id="consumptionTable">
<thead><tr>
<th>Meter</th><th>Date</th><th>Read Description</th><th>Days</th><th>Type</th>
<th>PreviousReading</th><th>CurrentReading</th><th>Usage in kWh</th><th>Amount$</th>
<th>Avg/Day</th><th>Units</th><th>Multiplier</th>
</tr></thead>
<tbody>
<tr><td>900000001</td><td>Jul 22, 2026</td><td>d</td><td>30</td><td>t</td><td>0</td><td>1</td><td>10.0</td><td>$0</td><td>0.3</td><td>kWh</td><td>1.0</td></tr>
<tr><td>900000001</td><td>Jan 22, 2020</td><td>d</td><td>30</td><td>t</td><td>0</td><td>1</td><td>5.0</td><td>$0</td><td>0.1</td><td>kWh</td><td>1.0</td></tr>
<tr><td>900000001</td><td>not-a-date</td><td>d</td><td>30</td><td>t</td><td>0</td><td>1</td><td>1.0</td><td>$0</td><td>0.1</td><td>kWh</td><td>1.0</td></tr>
<tr><td>900000002</td><td>Jul 20, 2026</td><td>d</td><td>30</td><td>t</td><td>0</td><td>1</td><td>99.0</td><td>$0</td><td>0.1</td><td>kWh</td><td>1.0</td></tr>
<tr><td>900000001</td><td>too</td><td>few</td></tr>
</tbody>
</table>
</body></html>
"""

# Mirrors the real water consumptionTable layout (no "Type" column, unlike
# hydro -- confirmed live; this shifted "Usage in CCF" to a different index
# than hydro's "Usage in kWh", which is exactly the bug this fixture guards.
CONSUMPTION_TABLE_WATER_LAYOUT = """
<html><body>
<table id="consumptionTable">
<thead><tr>
<th>Meter</th><th>Date</th><th>Reading Description</th><th>Days</th>
<th>Previous Reading</th><th>Current Reading</th><th>Usage in CCF</th>
<th>Water Amount $</th><th>Sewer Amount $</th><th>Average CCF per day</th>
<th>Multiplier</th><th>Face</th>
</tr></thead>
<tbody>
<tr><td>900000003</td><td>Jul 21, 2026</td><td>Water</td><td>30</td><td>823.000</td><td>829.000</td><td>6.000</td><td>$0.00</td><td>$0.00</td><td>0.200</td><td>1.0</td><td></td></tr>
</tbody>
</table>
</body></html>
"""

CONSUMPTION_TABLE_MISSING_HEADERS = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th><th>Something Else</th></tr></thead>
<tbody><tr><td>1</td><td>2</td></tr></tbody>
</table>
</body></html>
"""


@respx.mock
async def test_fetch_monthly_sends_compare_accounts_and_parses_rows_for_requested_meter(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    route = respx.get(url__regex=r".*consumptionInquiry.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE))

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_monthly(client, METER)

    sent_url = route.calls.last.request.url
    assert sent_url.params["compareAccounts"] == "hydro_104758-000001_900000001"

    # fetch_monthly returns the meter's full unbounded history in one shot
    # (no date filtering here -- the coordinator decides what's new); both
    # well-formed rows for meter 900000001 are returned, the malformed
    # "too/few" row and the other meter's row are not.
    assert len(readings) == 2
    newest = next(r for r in readings if r.period_end == datetime(2026, 7, 22, tzinfo=UTC))
    assert newest.value == 10.0
    assert newest.unit == "kWh"
    assert newest.period_start == datetime(2026, 6, 22, tzinfo=UTC)  # 30 days back


@respx.mock
async def test_fetch_monthly_skips_rows_belonging_to_a_different_meter(monkeypatch):
    # Defense in depth: even though compareAccounts should already scope the
    # table to one meter, a row reporting a different meter must never be
    # attributed to the requested meter.
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*consumptionInquiry.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE))

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_monthly(client, METER)

    assert all(r.value != 99.0 for r in readings)  # the 900000002 row never appears


@respx.mock
async def test_fetch_monthly_handles_water_layout_missing_type_column(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*consumptionInquiry.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_WATER_LAYOUT))

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_monthly(client, WATER_METER)

    assert len(readings) == 1
    assert readings[0].value == 6.0
    assert readings[0].unit == "CCF"
    assert readings[0].period_end == datetime(2026, 7, 21, tzinfo=UTC)


@respx.mock
async def test_fetch_monthly_missing_table_returns_empty(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*consumptionInquiry.*").mock(return_value=httpx.Response(200, text="<html><body>no table</body></html>"))

    async with httpx.AsyncClient() as client:
        assert await usage.fetch_monthly(client, METER) == []


@respx.mock
async def test_fetch_monthly_missing_date_or_usage_header_returns_empty(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*consumptionInquiry.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING_HEADERS))

    async with httpx.AsyncClient() as client:
        assert await usage.fetch_monthly(client, METER) == []


# --- fetch_daily -------------------------------------------------------


def _daily_fragment(meter_id: str, label: str, value: float) -> str:
    return f"""
    <script>
    var xAxisLabelArray = ['{label}'];
    Highcharts.stockChart('c', {{
        subtitle: {{ text: "Total Consumption of {value} KWH For {label} <br>Meter ID: {meter_id}" }},
        series: [{{ name: "Usage", data: [{value}] }}],
    }});
    </script>
    """


AMI_NO_DATA_FOR = "900000001"
AMI_NO_DATA = f"""
<script>
var xAxisLabelArray = [];
Highcharts.stockChart('c', {{
    subtitle: {{ text: "<br>Meter ID: {AMI_NO_DATA_FOR}" }},
    series: [{{ name: "Usage", data: [null] }}],
}});
</script>
"""


@respx.mock
async def test_fetch_daily_incremental_fetches_single_window_from_since(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    route = respx.get(url__regex=r".*smartMeterConsumV3.*type=daily.*").mock(
        return_value=httpx.Response(200, text=_daily_fragment("900000001", "2026-01-05", 5.0))
    )
    since = datetime(2026, 1, 1, tzinfo=UTC)

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_daily(client, METER, since=since, backfill=False)

    assert route.call_count == 1
    sent_url = route.calls.last.request.url
    assert sent_url.params["dailyFromDate"] == "2026-01-01"
    assert len(readings) == 1
    assert readings[0].value == 5.0


@respx.mock
async def test_fetch_daily_backfill_stops_after_consecutive_empty_windows(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(usage, "MAX_DAILY_SPAN_DAYS", 2)
    monkeypatch.setattr(usage, "MAX_CONSECUTIVE_EMPTY_WINDOWS", 2)

    # Relative to "now" (whatever it is when the test runs) rather than a
    # hardcoded calendar date, since fetch_daily's backfill walk always
    # starts from datetime.now(UTC). Only the most recent 6 days have data.
    has_data_cutoff = datetime.now(UTC).date() - timedelta(days=6)

    def handler(request: httpx.Request) -> httpx.Response:
        to_date = datetime.strptime(request.url.params["dailyToDate"], "%Y-%m-%d").date()
        if to_date > has_data_cutoff:
            return httpx.Response(200, text=_daily_fragment("900000001", to_date.isoformat(), 1.0))
        return httpx.Response(200, text=AMI_NO_DATA)

    route = respx.get(url__regex=r".*smartMeterConsumV3.*type=daily.*").mock(side_effect=handler)

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_daily(client, METER, since=None, backfill=True)

    # 3 two-day windows have data before 2 consecutive empty windows stop the walk.
    assert len(readings) == 3
    assert route.call_count == 5  # 3 with data + 2 empty before stopping


@respx.mock
async def test_fetch_daily_backfill_respects_lookback_cap_even_without_empty_window(monkeypatch):
    # Regression test: if the portal never returns an empty window, the walk
    # must still stop at backfill_lookback_days, not run forever. This is
    # exactly the bug that got a real coordinator refresh cancelled by
    # Home Assistant's setup timeout.
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(usage, "MAX_DAILY_SPAN_DAYS", 10)

    route = respx.get(url__regex=r".*smartMeterConsumV3.*type=daily.*").mock(
        side_effect=lambda request: httpx.Response(
            200, text=_daily_fragment("900000001", request.url.params["dailyFromDate"], 1.0)
        )
    )

    async with httpx.AsyncClient() as client:
        await usage.fetch_daily(client, METER, since=None, backfill=True, backfill_lookback_days=25)

    # ~25 days / 10-day spans -> 3 requests, not an unbounded walk.
    assert route.call_count == 3


@respx.mock
async def test_fetch_daily_window_rejects_session_fallback_to_different_meter(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    # We ask for 900000002 but the portal falls back to returning 900000001's data.
    respx.get(url__regex=r".*smartMeterConsumV3.*type=daily.*").mock(
        return_value=httpx.Response(200, text=_daily_fragment("900000001", "2026-01-01", 1.0))
    )
    other_meter = Meter(account_id="104758-000001", meter_id="900000002", utility_type=UtilityType.ELECTRIC)

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_daily(
            client, other_meter, since=datetime(2026, 1, 1, tzinfo=UTC), backfill=False
        )

    assert readings == []


@respx.mock
async def test_fetch_daily_window_error_page_treated_as_no_data(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*smartMeterConsumV3.*type=daily.*").mock(return_value=httpx.Response(200, text=ERROR_PAGE))

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_daily(
            client, METER, since=datetime(2026, 1, 1, tzinfo=UTC), backfill=False
        )

    assert readings == []


# --- fetch_hourly --------------------------------------------------------


def _hourly_fragment(meter_id: str, value: float) -> str:
    return f"""
    <script>
    var xAxisLabelArray = [];
    xAxisLabelArray = ['1:00 am'];
    Highcharts.stockChart('c', {{
        subtitle: {{ text: "Total Consumption of {value} KWH For today <br>Meter ID: {meter_id}" }},
        series: [{{ name: "Usage", data: [{value}] }}],
    }});
    </script>
    """


@respx.mock
async def test_fetch_hourly_incremental_walks_from_since_to_today(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    route = respx.get(url__regex=r".*smartMeterConsumV3.*type=hourly.*").mock(
        return_value=httpx.Response(200, text=_hourly_fragment("900000001", 2.0))
    )
    since = datetime.now(UTC) - timedelta(days=2)

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_hourly(client, METER, since=since, backfill=False)

    assert route.call_count == 3  # since's day, +1, today (inclusive walk)
    assert len(readings) == 3  # one reading/day from this fragment


@respx.mock
async def test_fetch_hourly_backfill_respects_lookback_cap(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    route = respx.get(url__regex=r".*smartMeterConsumV3.*type=hourly.*").mock(
        return_value=httpx.Response(200, text=_hourly_fragment("900000001", 1.0))
    )

    async with httpx.AsyncClient() as client:
        await usage.fetch_hourly(client, METER, since=None, backfill=True, backfill_lookback_days=5)

    assert route.call_count == 6  # today + 5 days back, inclusive


@respx.mock
async def test_fetch_hourly_day_rejects_session_fallback_to_different_meter(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*smartMeterConsumV3.*type=hourly.*").mock(
        return_value=httpx.Response(200, text=_hourly_fragment("900000001", 5.0))
    )
    other_meter = Meter(account_id="104758-000001", meter_id="900000002", utility_type=UtilityType.ELECTRIC)

    async with httpx.AsyncClient() as client:
        readings = await usage.fetch_hourly(
            client, other_meter, since=datetime.now(UTC), backfill=False
        )

    assert readings == []
