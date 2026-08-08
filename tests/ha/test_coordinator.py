"""Tests for WestervilleCoordinator: auth error mapping, backfill vs
incremental behavior, and statistics import.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.westerville_utilities.client import CannotConnect, InvalidAuth
from custom_components.westerville_utilities.client.models import Account, Meter, UtilityType
from custom_components.westerville_utilities.const import DOMAIN
from custom_components.westerville_utilities.coordinator import (
    WestervilleCoordinator,
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
<a href="?para=selectAccount&inAccountNumber=104758-000001">104758-000001</a>
<br>104758-000001: 789 Test St, Westerville, OH 43081-1234
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


def _mock_happy_path() -> None:
    respx.get(f"{BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(200, text=DASHBOARD_AFTER_LOGIN)
    )
    respx.get(f"{BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE))
    respx.get(url__regex=r".*para=billInquirySelect.*").mock(return_value=httpx.Response(200, text=BILL_TABLE))
    respx.get(url__regex=r".*inquiryType=hydro.*tab=probe.*").mock(
        return_value=httpx.Response(200, text=HYDRO_CONSUMPTION_TABLE)
    )
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(return_value=httpx.Response(200, text=AMI_NO_DATA))


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "hunter2"},
    )
    entry.add_to_hass(hass)
    return entry


@respx.mock
async def test_first_refresh_imports_monthly_and_bill_statistics(hass: HomeAssistant, recorder_mock) -> None:
    _mock_happy_path()
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    assert coordinator.last_update_success is True
    assert "900000001" in coordinator.data.meters
    assert coordinator.data.meters["900000001"].latest_value == 10.0
    assert "104758-000001" in coordinator.data.bills
    assert coordinator.data.bills["104758-000001"].amount_total == 150.00

    meter_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:electric_900000001", True, {"sum"}
    )
    assert meter_stats["westerville_utilities:electric_900000001"][0]["sum"] == 10.0

    bill_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, "westerville_utilities:104758_000001_bill_amount", True, {"sum"}
    )
    assert bill_stats["westerville_utilities:104758_000001_bill_amount"][0]["sum"] == 150.00


@respx.mock
async def test_second_refresh_does_not_reimport_unchanged_data(hass: HomeAssistant, recorder_mock) -> None:
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


async def test_invalid_auth_raises_config_entry_auth_failed(hass: HomeAssistant, recorder_mock, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    async def _raise_invalid_auth(*args, **kwargs):
        raise InvalidAuth("bad credentials")

    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.authenticate", _raise_invalid_auth)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_cannot_connect_raises_update_failed(hass: HomeAssistant, recorder_mock, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    async def _raise_cannot_connect(*args, **kwargs):
        raise CannotConnect("portal unreachable")

    monkeypatch.setattr("custom_components.westerville_utilities.coordinator.authenticate", _raise_cannot_connect)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_no_accounts_raises_update_failed(hass: HomeAssistant, recorder_mock, monkeypatch) -> None:
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
    meter = Meter(account_id="104758-000001", meter_id="900000001", utility_type=UtilityType.ELECTRIC)
    assert _statistic_id(meter) == "westerville_utilities:electric_900000001"

    account = Account(account_id="104758-000001")
    assert _bill_statistic_id(account) == "westerville_utilities:104758_000001_bill_amount"


async def test_options_configure_update_interval_and_backfill_depth(hass: HomeAssistant, recorder_mock) -> None:
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


async def test_options_default_when_unset(hass: HomeAssistant, recorder_mock) -> None:
    entry = _entry(hass)
    coordinator = WestervilleCoordinator(hass, entry)

    assert coordinator.update_interval.total_seconds() == 24 * 3600
    assert coordinator._backfill_daily_days == 400
    assert coordinator._backfill_hourly_days == 30
