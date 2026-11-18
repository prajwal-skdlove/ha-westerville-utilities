"""Tests for WestervilleCoordinator: auth error mapping, backfill vs
incremental behavior, and statistics import.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.westerville_utilities.client import CannotConnect, InvalidAuth
from custom_components.westerville_utilities.client.models import Account, Meter, UtilityType
from custom_components.westerville_utilities.const import DOMAIN
from custom_components.westerville_utilities.coordinator import (
    WestervilleCoordinator,
    _bill_cost_statistic_id,
    _bill_statistic_id,
    _statistic_id,
)

BASE_URL = "https://billpay.westerville.org"

LOGIN_PAGE = """
<html><body>
<form id="login-form" method="post" action="/app/capricorn?para=index&platform=&deviceOS=">
    <input type="hidden" name="jspCSRFToken" value="test-csrf-token" />
</form>
</body></html>
"""
DASHBOARD_AFTER_LOGIN = "<html><body><div>Welcome</div></body></html>"

SELECT_ACCOUNT_PAGE = """
<html><body>
<a href="?para=selectAccount&inAccountNumber=100000-000001">100000-000001</a>
<br>100000-000001: 789 Test St, Westerville, OH 43081-1234
</body></html>
"""
MY_ACCOUNT_PAGE = """
<html><body><form>
<input type="text" name="firstName" value="Jane" />
<input type="text" name="lastName" value="Tester" />
</form></body></html>
"""

BILL_TABLE = """
<html><body><table id="billTable">
<thead><tr><th>View Bill</th><th>Date</th><th>Bill Amount</th><th>Due Date</th></tr></thead>
<tbody>
<td><a>View</a></td><td>Jul 30, 2026</td><td>$150.00</td><td>Aug 15, 2026</td>
</tbody>
</table></body></html>
"""

# One electric meter, no compareAccounts selector rendered but a row present
# (the "single meter, no selector" fallback path -- keeps this fixture set
# small while still exercising a real discovery path).
HYDRO_CONSUMPTION_TABLE = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th><th>Date</th><th>Days</th><th>Usage in kWh</th></tr></thead>
<tbody><tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr></tbody>
</table>
</body></html>
"""
NO_TABLE = "<html><body>no table</body></html>"

# No AMI data for this meter -- keeps the happy-path test to monthly-only,
# which is enough to exercise the coordinator's statistics-import wiring
# without needing a full daily/hourly fixture set (that's covered directly
# against client/usage.py in tests/client/).
AMI_NO_DATA = """
<script>
var xAxisLabelArray = [];
Highcharts.stockChart('c', {
    subtitle: { text: "<br>Meter ID: 900000001" },
    series: [{ name: "Usage", data: [null] }],
});
</script>
"""


def _mock_happy_path(consumption_table: str = HYDRO_CONSUMPTION_TABLE, bill_table: str = BILL_TABLE) -> respx.Route:
    respx.get(f"{BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(200, text=DASHBOARD_AFTER_LOGIN)
    )
    respx.get(f"{BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE))
    bill_route = respx.get(url__regex=r".*para=billInquirySelect.*").mock(
        return_value=httpx.Response(200, text=bill_table)
    )
    respx.get(url__regex=r".*inquiryType=hydro.*tab=probe.*").mock(
        return_value=httpx.Response(200, text=consumption_table)
    )
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(return_value=httpx.Response(200, text=AMI_NO_DATA))
    return bill_route


def _bill_table(*bills: tuple[str, str]) -> str:
    cells = "".join(
        f"<td><a>View</a></td><td>{bill_date}</td><td>${amount}</td><td>Oct 15, 2026</td>"
        for bill_date, amount in bills
    )
    return (
        '<html><body><table id="billTable"><thead><tr><th>View Bill</th><th>Date</th>'
        f"<th>Bill Amount</th><th>Due Date</th></tr></thead><tbody>{cells}</tbody></table></body></html>"
    )


ALL_FOUR_BILLS = _bill_table(
    ("Sep 29, 2026", "200.00"), ("Aug 30, 2026", "150.00"), ("Jul 31, 2026", "120.00"), ("Jul 1, 2026", "100.00")
)
NEWEST_TWO_BILLS = _bill_table(("Sep 29, 2026", "200.00"), ("Aug 30, 2026", "150.00"))
# Two readings starting Jun 22 and Sep 22, so the months between hold no row.
SPARSE_CONSUMPTION_TABLE = HYDRO_CONSUMPTION_TABLE.replace(
    "<tbody><tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr></tbody>",
    "<tbody>"
    "<tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr>"
    "<tr><td>900000001</td><td>Oct 22, 2026</td><td>30</td><td>20.0</td></tr>"
    "</tbody>",
)


def _local_midnight(day: date) -> float:
    return dt_util.start_of_local_day(day).timestamp()


async def _stored_sums(hass: HomeAssistant, statistic_id: str) -> dict[float, float]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return {row["start"]: row["sum"] for row in rows.get(statistic_id, [])}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "hunter2"},
    )
    entry.add_to_hass(hass)
    return entry


@respx.mock
async def test_first_refresh_imports_monthly_and_bill_statistics(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    assert coordinator.last_update_success is True
    assert "900000001" in coordinator.data.meters
    assert coordinator.data.meters["900000001"].latest_value == 10.0
    assert "100000-000001" in coordinator.data.bills
    assert coordinator.data.bills["100000-000001"].amount_total == 150.00

    meter_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:electric_900000001", True, {"sum"}
    )
    assert meter_stats["westerville_utilities:electric_900000001"][0]["sum"] == 10.0

    bill_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:100000_000001_bill_amount", True, {"sum"}
    )
    assert bill_stats["westerville_utilities:100000_000001_bill_amount"][0]["sum"] == 150.00


@respx.mock
async def test_bill_cost_statistic_is_dated_at_period_end_local_midnight(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    coordinator = WestervilleCoordinator(hass, _entry(hass))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    cost_id = _bill_cost_statistic_id(Account(account_id="100000-000001"))
    assert cost_id == "westerville_utilities:100000_000001_bill_cost"
    cost_stats = await hass.async_add_executor_job(get_last_statistics, hass, 1, cost_id, True, {"sum"})
    row = cost_stats[cost_id][0]
    assert row["sum"] == 150.00
    # BILL_TABLE's only bill is dated Jul 30, 2026.
    assert row["start"] == dt_util.start_of_local_day(date(2026, 7, 30)).timestamp()


@respx.mock
async def test_sparse_monthly_readings_get_first_of_month_anchors(recorder_mock, hass: HomeAssistant) -> None:
    sparse_table = HYDRO_CONSUMPTION_TABLE.replace(
        "<tbody><tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr></tbody>",
        "<tbody>"
        "<tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr>"
        "<tr><td>900000001</td><td>Oct 22, 2026</td><td>30</td><td>20.0</td></tr>"
        "</tbody>",
    )
    _mock_happy_path(sparse_table)
    coordinator = WestervilleCoordinator(hass, _entry(hass))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    statistic_id = "westerville_utilities:electric_900000001"
    rows = await hass.async_add_executor_job(get_last_statistics, hass, 10, statistic_id, True, {"sum"})
    by_start = {row["start"]: row["sum"] for row in rows[statistic_id]}
    # Readings start Jun 22 (sum 10) and Sep 22 (sum 30); Jul 1, Aug 1 and Sep 1 hold none.
    for anchor in (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)):
        assert by_start[dt_util.start_of_local_day(anchor).timestamp()] == 10.0
    assert max(by_start.values()) == 30.0
    assert len(by_start) == 5

    # A second refresh with unchanged data must not add or alter anything.
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    again = await hass.async_add_executor_job(get_last_statistics, hass, 10, statistic_id, True, {"sum"})
    assert {row["start"]: row["sum"] for row in again[statistic_id]} == by_start


@respx.mock
async def test_bill_cost_is_rebuilt_with_bills_the_portal_no_longer_returns(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    # Four bills 30 days apart. Later the portal only returns the newest two.
    bill_route = _mock_happy_path(bill_table=ALL_FOUR_BILLS)
    coordinator = WestervilleCoordinator(hass, _entry(hass))
    cost_id = _bill_cost_statistic_id(Account(account_id="100000-000001"))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert (await _stored_sums(hass, cost_id))[_local_midnight(date(2026, 9, 29))] == 570.0

    # Recreate what the previous release left behind: a cost series that
    # only holds what the portal still returns.
    get_instance(hass).async_clear_statistics([cost_id])
    await async_wait_recording_done(hass)
    bill_route.mock(return_value=httpx.Response(200, text=NEWEST_TWO_BILLS))

    async def _no_older_bills(self, account, bills):
        return []

    monkeypatch.setattr(WestervilleCoordinator, "_async_bills_missing_from_portal", _no_older_bills)
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert (await _stored_sums(hass, cost_id))[_local_midnight(date(2026, 9, 29))] == 350.0
    monkeypatch.undo()

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    rebuilt = await _stored_sums(hass, cost_id)

    # The older bills are back and every later sum, anchors included, shifted.
    assert rebuilt[_local_midnight(date(2026, 7, 1))] == 100.0
    assert rebuilt[_local_midnight(date(2026, 7, 31))] == 220.0
    assert rebuilt[_local_midnight(date(2026, 8, 1))] == 220.0
    assert rebuilt[_local_midnight(date(2026, 8, 30))] == 370.0
    assert rebuilt[_local_midnight(date(2026, 9, 1))] == 370.0
    assert rebuilt[_local_midnight(date(2026, 9, 29))] == 570.0
    assert max(rebuilt.values()) == 570.0

    # The rebuild happens once: another refresh changes nothing.
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert await _stored_sums(hass, cost_id) == rebuilt


@respx.mock
async def test_month_anchors_are_backfilled_across_existing_history_once(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    # Import history the way the previous release did: without anchors.
    monkeypatch.setattr(
        "custom_components.westerville_utilities.coordinator._with_month_anchors",
        lambda stats, prior_start_ts, prior_sum: stats,
    )
    _mock_happy_path(SPARSE_CONSUMPTION_TABLE, ALL_FOUR_BILLS)
    coordinator = WestervilleCoordinator(hass, _entry(hass))
    meter_id = "westerville_utilities:electric_900000001"
    amount_id = "westerville_utilities:100000_000001_bill_amount"
    cost_id = _bill_cost_statistic_id(Account(account_id="100000-000001"))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert len(await _stored_sums(hass, meter_id)) == 2
    assert len(await _stored_sums(hass, cost_id)) == 4
    amount_rows_before = len(await _stored_sums(hass, amount_id))
    monkeypatch.undo()

    # Nothing new on the portal, yet the stored history gains its anchors.
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    meter_sums = await _stored_sums(hass, meter_id)
    cost_sums = await _stored_sums(hass, cost_id)
    assert len(meter_sums) == 5
    assert len(cost_sums) == 6
    assert len(await _stored_sums(hass, amount_id)) > amount_rows_before
    for anchor in (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)):
        assert meter_sums[_local_midnight(anchor)] == 10.0
    assert cost_sums[_local_midnight(date(2026, 8, 1))] == 220.0
    assert cost_sums[_local_midnight(date(2026, 9, 1))] == 370.0
    # Never past the newest real row.
    assert max(meter_sums) == datetime(2026, 9, 22, tzinfo=UTC).timestamp()
    assert max(cost_sums) == _local_midnight(date(2026, 9, 29))

    # Idempotent: every later refresh adds nothing.
    stored = (meter_sums, cost_sums, await _stored_sums(hass, amount_id))
    for _ in range(2):
        await coordinator.async_refresh()
        await async_wait_recording_done(hass)
    assert (
        await _stored_sums(hass, meter_id),
        await _stored_sums(hass, cost_id),
        await _stored_sums(hass, amount_id),
    ) == stored


@respx.mock
async def test_second_refresh_does_not_reimport_unchanged_data(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    first_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:electric_900000001", True, {"sum"}
    )
    first_sum = first_stats["westerville_utilities:electric_900000001"][0]["sum"]

    # Nothing changed on the portal between refreshes -- the same monthly
    # row comes back again. The running sum must not double-count it.
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    second_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:electric_900000001", True, {"sum"}
    )
    second_sum = second_stats["westerville_utilities:electric_900000001"][0]["sum"]

    assert second_sum == first_sum == 10.0


async def test_invalid_auth_raises_config_entry_auth_failed(recorder_mock, hass: HomeAssistant, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    async def _raise_invalid_auth(*args, **kwargs):
        raise InvalidAuth("bad credentials")

    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.authenticate", _raise_invalid_auth)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_cannot_connect_raises_update_failed(recorder_mock, hass: HomeAssistant, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    async def _raise_cannot_connect(*args, **kwargs):
        raise CannotConnect("portal unreachable")

    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.authenticate", _raise_cannot_connect)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_no_accounts_raises_update_failed(recorder_mock, hass: HomeAssistant, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    async def _noop_authenticate(*args, **kwargs):
        return None

    async def _empty_accounts(*args, **kwargs):
        return []

    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.authenticate", _noop_authenticate)
    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.list_accounts", _empty_accounts)

    with pytest.raises(UpdateFailed, match="no accounts"):
        await coordinator._async_update_data()


def test_statistic_id_sanitizes_dashes_in_account_id() -> None:
    meter = Meter(account_id="100000-000001", meter_id="900000001", utility_type=UtilityType.ELECTRIC)
    assert _statistic_id(meter) == "westerville_utilities:electric_900000001"

    account = Account(account_id="100000-000001")
    assert _bill_statistic_id(account) == "westerville_utilities:100000_000001_bill_amount"


async def test_options_configure_update_interval_and_backfill_depth(recorder_mock, hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "hunter2"},
        options={
            "update_interval_hours": 6,
            "backfill_daily_days": 100,
            "backfill_hourly_days": 10,
        },
    )
    entry.add_to_hass(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    assert coordinator.update_interval.total_seconds() == 6 * 3600
    assert coordinator._backfill_daily_days == 100
    assert coordinator._backfill_hourly_days == 10


async def test_options_default_when_unset(recorder_mock, hass: HomeAssistant) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    assert coordinator.update_interval.total_seconds() == 24 * 3600
    assert coordinator._backfill_daily_days == 400
    assert coordinator._backfill_hourly_days == 30
