"""Tests for sensor.py: meter usage and bill sensor entities, via a full
config entry setup (exercises __init__.py + coordinator.py + sensor.py
together).
"""

from __future__ import annotations

import httpx
import respx

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.client.synthetic_bill import BILL_COMPONENTS, BILL_LINES, pdf_with_lines

from custom_components.westerville_utilities.const import DOMAIN

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

HYDRO_CONSUMPTION_TABLE = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th><th>Date</th><th>Days</th><th>Usage in kWh</th></tr></thead>
<tbody><tr><td>900000001</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr></tbody>
</table>
</body></html>
"""
NO_TABLE = "<html><body>no table</body></html>"

AMI_NO_DATA = """
<script>
var xAxisLabelArray = [];
Highcharts.stockChart('c', {
    subtitle: { text: "<br>Meter ID: 900000001" },
    series: [{ name: "Usage", data: [null] }],
});
</script>
"""


def _mock_happy_path(bill_table: str = BILL_TABLE) -> None:
    respx.get(f"{BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(200, text=DASHBOARD_AFTER_LOGIN)
    )
    respx.get(f"{BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE))
    respx.get(url__regex=r".*para=billInquirySelect.*").mock(return_value=httpx.Response(200, text=bill_table))
    respx.get(url__regex=r".*inquiryType=hydro.*tab=probe.*").mock(
        return_value=httpx.Response(200, text=HYDRO_CONSUMPTION_TABLE)
    )
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=NO_TABLE))
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(return_value=httpx.Response(200, text=AMI_NO_DATA))


@respx.mock
async def test_meter_and_bill_sensors_created_on_entry_setup(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"}
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    meter_state = hass.states.get("sensor.electric_900000001_usage")
    assert meter_state is not None
    assert meter_state.state == "10.0"
    assert meter_state.attributes["device_class"] == SensorDeviceClass.ENERGY
    assert meter_state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
    assert meter_state.attributes["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR

    bill_state = hass.states.get("sensor.westerville_account_100000_000001_latest_bill")
    assert bill_state is not None
    assert bill_state.state == "150.0"
    assert bill_state.attributes["device_class"] == SensorDeviceClass.MONETARY
    assert bill_state.attributes["due_date"] == "2026-08-15T00:00:00+00:00"
    # The address and name stay out of the history database unless the user opts in.
    assert "service_address" not in bill_state.attributes
    assert "account_holder" not in bill_state.attributes


@respx.mock
async def test_unload_entry_removes_entities(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"}
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.electric_900000001_usage") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Unload marks the entity unavailable rather than removing its state
    # outright (HA keeps it around, restorable, until the entity/entry is
    # actually deleted) -- that's the real, correct behavior here.
    state = hass.states.get("sensor.electric_900000001_usage")
    assert state is not None
    assert state.state == "unavailable"


LINKED_BILL_TABLE = """
<html><body><table id="billTable">
<thead><tr><th>View Bill</th><th>Date</th><th>Bill Amount</th><th>Due Date</th></tr></thead>
<tbody>
<td><a data-url="/app/viewBill?key=abc123" href="#">View</a></td><td>Jul 30, 2026</td><td>$28.60</td><td>Aug 15, 2026</td>
</tbody>
</table></body></html>
"""
BILL_SENSOR = "sensor.westerville_account_100000_000001_latest_bill"


@respx.mock
async def test_bill_sensor_shows_address_and_holder_only_when_opted_in(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "hunter2"},
        options={"expose_account_details": True},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    attributes = hass.states.get(BILL_SENSOR).attributes
    assert attributes["service_address"] == "789 Test St, Westerville, OH 43081-1234"
    assert attributes["account_holder"] == "Jane Tester"


@respx.mock
async def test_bill_sensor_shows_pdf_detail_without_the_link_or_personal_data(
    recorder_mock, hass: HomeAssistant
) -> None:
    _mock_happy_path(LINKED_BILL_TABLE)
    respx.get(url__regex=r".*viewBill.*").mock(return_value=httpx.Response(200, content=pdf_with_lines(BILL_LINES)))
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    attributes = hass.states.get(BILL_SENSOR).attributes
    assert attributes["components"] == BILL_COMPONENTS
    assert attributes["billing_period_start"] == "2026-09-30"
    assert attributes["billing_period_end"] == "2026-11-02"
    assert [item["description"] for item in attributes["line_items"]][:2] == [
        "ELECTRIC - PSCA",
        "Electric - Customer Charge",
    ]
    assert attributes["line_items"][0] == {
        "component": "electric",
        "description": "ELECTRIC - PSCA",
        "usage": 100.0,
        "rate": 0.01,
        "amount": 1.0,
    }
    assert [read["service"] for read in attributes["meter_reads"]] == ["Electric", "Ev Charge", "Water"]
    assert attributes["meter_reads"][0]["previous_read_date"] == "2026-10-01"
    assert attributes["meter_reads"][0]["usage"] == 100.0
    assert "abc123" not in str(attributes)
    assert "service_address" not in attributes
    assert "account_holder" not in attributes


@respx.mock
async def test_bill_sensor_has_no_pdf_detail_when_the_bill_has_no_link(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path()
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    attributes = hass.states.get(BILL_SENSOR).attributes
    assert "components" not in attributes
    assert "meter_reads" not in attributes
