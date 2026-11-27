"""Tests for the bill month picker (select.py) and the bill history behind it."""

from __future__ import annotations

import httpx
import respx

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.client.synthetic_bill import BILL_COMPONENTS, BILL_LINES, pdf_with_lines
from tests.ha.test_sensor import _mock_happy_path

from custom_components.westerville_utilities.const import DOMAIN

BILL_SELECT = "select.westerville_account_100000_000001_bill_month"

TWO_LINKED_BILLS = """
<html><body><table id="billTable">
<thead><tr><th>View Bill</th><th>Date</th><th>Bill Amount</th><th>Due Date</th></tr></thead>
<tbody>
<td><a data-url="/app/viewBill?key=new">View</a></td><td>Jul 30, 2026</td><td>$28.60</td><td>Aug 15, 2026</td>
<td><a data-url="/app/viewBill?key=old">View</a></td><td>Jun 26, 2026</td><td>$28.60</td><td>Jul 15, 2026</td>
</tbody>
</table></body></html>
"""

LINKLESS_OLD_BILL = TWO_LINKED_BILLS.replace('<a data-url="/app/viewBill?key=old">View</a>', "<a>View</a>")


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@respx.mock
async def test_picker_lists_bills_newest_first_and_steps_back(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path(TWO_LINKED_BILLS)
    respx.get(url__regex=r".*viewBill.*").mock(return_value=httpx.Response(200, content=pdf_with_lines(BILL_LINES)))
    await _setup(hass)

    state = hass.states.get(BILL_SELECT)
    assert state.state == "Jul 30, 2026"
    assert state.attributes["options"] == ["Jul 30, 2026", "Jun 26, 2026"]
    assert state.attributes["amount"] == 28.6

    await hass.services.async_call("select", "select_next", {"entity_id": BILL_SELECT, "cycle": False}, blocking=True)

    state = hass.states.get(BILL_SELECT)
    assert state.state == "Jun 26, 2026"
    assert state.attributes["amount"] == 28.6
    assert state.attributes["bill_date"] == "2026-06-26"
    assert state.attributes["components"] == BILL_COMPONENTS
    assert "key=old" not in str(state.attributes)


@respx.mock
async def test_old_bills_are_read_once_and_kept_across_a_reload(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path(TWO_LINKED_BILLS)
    pdf_route = respx.get(url__regex=r".*viewBill.*").mock(
        return_value=httpx.Response(200, content=pdf_with_lines(BILL_LINES))
    )
    entry = await _setup(hass)
    assert pdf_route.call_count == 2

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Only the newest bill is read again; the older one comes from storage.
    assert pdf_route.call_count == 3
    await hass.services.async_call("select", "select_next", {"entity_id": BILL_SELECT, "cycle": False}, blocking=True)
    assert hass.states.get(BILL_SELECT).attributes["components"] == BILL_COMPONENTS


@respx.mock
async def test_bill_without_a_pdf_link_is_listed_without_detail(recorder_mock, hass: HomeAssistant) -> None:
    _mock_happy_path(LINKLESS_OLD_BILL)
    respx.get(url__regex=r".*viewBill.*").mock(return_value=httpx.Response(200, content=pdf_with_lines(BILL_LINES)))
    await _setup(hass)

    await hass.services.async_call("select", "select_next", {"entity_id": BILL_SELECT, "cycle": False}, blocking=True)

    state = hass.states.get(BILL_SELECT)
    assert state.state == "Jun 26, 2026"
    assert state.attributes["amount"] == 28.6
    assert "components" not in state.attributes
