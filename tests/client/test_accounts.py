"""Tests for client/accounts.py: account/meter discovery."""

from __future__ import annotations

import httpx
import respx

from client import shared
from client.accounts import (
    _first_row_meter_id,
    _parse_compare_accounts_options,
    inquiry_type_of,
    list_accounts,
    list_meters,
    supported_granularities,
)
from client.models import Account, Granularity, Meter, UtilityType

SELECT_ACCOUNT_PAGE = """
<html><body>
<a href="/app/capricorn?para=selectAccount&userAction=select&inAccountNumber=104758-000001&inMeterID=1&meterType=Electric">104758-000001</a>
<a href="/app/capricorn?para=selectAccount&userAction=select&inAccountNumber=104758-000001&inMeterID=1&meterType=Electric">104758-000001 (dup)</a>
<br>104758-000001: 789 Test St, Westerville, OH  43081-1234
</body></html>
"""

SELECT_ACCOUNT_PAGE_MULTI = """
<html><body>
<a href="?para=selectAccount&inAccountNumber=222-222">acct b</a>
<a href="?para=selectAccount&inAccountNumber=111-111">acct a</a>
222-222: 456 Oak Ave, Westerville, OH 43081
111-111: 123 Main St, Westerville, OH 43081
</body></html>
"""

SELECT_ACCOUNT_PAGE_EMPTY = "<html><body>no accounts here</body></html>"

SELECT_ACCOUNT_PAGE_NO_ADDRESS = """
<html><body>
<a href="?para=selectAccount&inAccountNumber=104758-000001">104758-000001</a>
</body></html>
"""

MY_ACCOUNT_PAGE = """
<html><body>
<form>
<input type="text" name="firstName" value="Jane" />
<input type="text" name="lastName" value="Tester" />
</form>
</body></html>
"""

MY_ACCOUNT_PAGE_NO_NAME = "<html><body>no name fields here</body></html>"

# Two electric meters on one account: a main "Electric" meter and a separate
# "EV CHARGE" submeter -- confirmed live. compareAccounts option value shape:
# "{inquiryType}_{accountId}_{meterId}"; text shape:
# "{address} - {meterId} - {description} - {accountId}".
CONSUMPTION_TABLE_MULTI_METER = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th><th>Date</th><th>Days</th><th>Usage in kWh</th></tr></thead>
<tbody><tr><td>900000002</td><td>Jul 22, 2026</td><td>30</td><td>10.0</td></tr></tbody>
</table>
<select name="compareAccounts" multiple="multiple">
<option value="hydro_104758-000001_900000002" selected>789 Test St - 900000002 - EV CHARGE - 104758-000001</option>
<option value="hydro_104758-000001_900000001">789 Test St - 900000001 - Electric - 104758-000001</option>
</select>
</body></html>
"""

CONSUMPTION_TABLE_SINGLE_METER_WITH_SELECTOR = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th></tr></thead>
<tbody><tr><td>900000003</td></tr></tbody>
</table>
<select name="compareAccounts" multiple="multiple">
<option value="water_104758-000001_900000003" selected>789 Test St - 900000003 - Water - 104758-000001</option>
</select>
</body></html>
"""

CONSUMPTION_TABLE_NO_SELECTOR_FALLBACK = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th></tr></thead>
<tbody><tr><td>99999</td><td>Jul 22, 2026</td></tr></tbody>
</table>
</body></html>
"""

CONSUMPTION_TABLE_MALFORMED_OPTION = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th></tr></thead>
<tbody><tr><td>1</td></tr></tbody>
</table>
<select name="compareAccounts" multiple="multiple">
<option value="not-enough-parts">weird text with no dashes</option>
</select>
</body></html>
"""

CONSUMPTION_TABLE_EMPTY = """
<html><body>
<table id="consumptionTable">
<thead><tr><th>Meter</th></tr></thead>
<tbody></tbody>
</table>
</body></html>
"""

CONSUMPTION_TABLE_MISSING = "<html><body>no table</body></html>"


def _ami_fragment(meter_id: str, has_data: bool = True) -> str:
    if has_data:
        return f"""
        <script>
        var xAxisLabelArray = ['2024-01-01'];
        Highcharts.stockChart('c', {{
            subtitle: {{ text: "Total Consumption of 1.00 KWH For Jan 1 <br>Meter ID: {meter_id}" }},
            series: [{{ name: "Usage", data: [1.0] }}],
        }});
        </script>
        """
    return f"""
    <script>
    var xAxisLabelArray = [];
    Highcharts.stockChart('c', {{
        subtitle: {{ text: "<br>Meter ID: {meter_id}" }},
        series: [{{ name: "Usage", data: [null] }}],
    }});
    </script>
    """


@respx.mock
async def test_list_accounts_dedupes_and_includes_address_and_name(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE))

    async with httpx.AsyncClient() as client:
        result = await list_accounts(client)

    assert result == [
        Account(
            account_id="104758-000001",
            service_address="789 Test St, Westerville, OH 43081-1234",
            name="Jane Tester",
        )
    ]


@respx.mock
async def test_list_accounts_multiple_are_sorted_with_own_addresses(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE_MULTI)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE_NO_NAME))

    async with httpx.AsyncClient() as client:
        result = await list_accounts(client)

    assert [a.account_id for a in result] == ["111-111", "222-222"]
    assert result[0].service_address == "123 Main St, Westerville, OH 43081"
    assert result[1].service_address == "456 Oak Ave, Westerville, OH 43081"
    assert result[0].name is None


@respx.mock
async def test_list_accounts_empty_does_not_fetch_my_account(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE_EMPTY)
    )

    async with httpx.AsyncClient() as client:
        assert await list_accounts(client) == []


@respx.mock
async def test_list_accounts_missing_address_is_none(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/capricorn?para=selectAccount").mock(
        return_value=httpx.Response(200, text=SELECT_ACCOUNT_PAGE_NO_ADDRESS)
    )
    respx.get(url__regex=r".*para=myAccount.*").mock(return_value=httpx.Response(200, text=MY_ACCOUNT_PAGE_NO_NAME))

    async with httpx.AsyncClient() as client:
        result = await list_accounts(client)

    assert result[0].service_address is None


@respx.mock
async def test_list_meters_discovers_multiple_meters_per_inquiry_type(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*inquiryType=hydro.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MULTI_METER))
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_SINGLE_METER_WITH_SELECTOR))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))

    account = Account(account_id="104758-000001")
    async with httpx.AsyncClient() as client:
        result = await list_meters(client, account)

    assert len(result) == 3
    ev_charge = next(m for m in result if m.meter_id == "900000002")
    electric = next(m for m in result if m.meter_id == "900000001")
    water = next(m for m in result if m.meter_id == "900000003")

    assert ev_charge.description == "EV CHARGE"
    assert ev_charge.service_address == "789 Test St"
    assert ev_charge.utility_type == UtilityType.ELECTRIC
    assert electric.description == "Electric"
    assert electric.utility_type == UtilityType.ELECTRIC
    assert water.description == "Water"
    assert water.utility_type == UtilityType.WATER


@respx.mock
async def test_list_meters_falls_back_to_table_meter_when_no_selector(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*inquiryType=hydro.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_NO_SELECTOR_FALLBACK))
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))

    account = Account(account_id="104758-000001")
    async with httpx.AsyncClient() as client:
        result = await list_meters(client, account)

    assert len(result) == 1
    assert result[0].meter_id == "99999"
    assert result[0].description is None


@respx.mock
async def test_list_meters_falls_back_when_compare_accounts_option_is_malformed(monkeypatch):
    # If every option fails to parse, we still shouldn't silently lose the
    # meter -- fall back to whatever the table itself reports.
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*inquiryType=hydro.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MALFORMED_OPTION))
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))

    account = Account(account_id="104758-000001")
    async with httpx.AsyncClient() as client:
        result = await list_meters(client, account)

    assert len(result) == 1
    assert result[0].meter_id == "1"


@respx.mock
async def test_list_meters_no_service_at_all(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*inquiryType=hydro.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_EMPTY))
    respx.get(url__regex=r".*inquiryType=water.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))
    respx.get(url__regex=r".*inquiryType=sewer.*").mock(return_value=httpx.Response(200, text=CONSUMPTION_TABLE_MISSING))

    account = Account(account_id="104758-000001")
    async with httpx.AsyncClient() as client:
        assert await list_meters(client, account) == []


@respx.mock
async def test_supported_granularities_with_ami_data_for_requested_meter(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(return_value=httpx.Response(200, text=_ami_fragment("900000001")))

    meter = Meter(account_id="a1", meter_id="900000001", utility_type=UtilityType.ELECTRIC)
    async with httpx.AsyncClient() as client:
        result = await supported_granularities(client, meter)

    assert result == [Granularity.HOURLY, Granularity.DAILY, Granularity.MONTHLY]


@respx.mock
async def test_supported_granularities_without_ami_data(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(
        return_value=httpx.Response(200, text=_ami_fragment("900000003", has_data=False))
    )

    meter = Meter(account_id="a1", meter_id="900000003", utility_type=UtilityType.SEWER)
    async with httpx.AsyncClient() as client:
        result = await supported_granularities(client, meter)

    assert result == [Granularity.MONTHLY]


@respx.mock
async def test_supported_granularities_rejects_session_fallback_to_different_meter(monkeypatch):
    # The portal doesn't reject selectedMeterId for a meter with no AMI
    # registration -- it silently returns whatever meter was last active in
    # the session. Requesting 900000002 (no AMI) but getting 900000001's
    # data back must NOT be treated as "900000002 supports AMI".
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*smartMeterConsumV3.*").mock(return_value=httpx.Response(200, text=_ami_fragment("900000001")))

    meter = Meter(account_id="a1", meter_id="900000002", utility_type=UtilityType.ELECTRIC)
    async with httpx.AsyncClient() as client:
        result = await supported_granularities(client, meter)

    assert result == [Granularity.MONTHLY]


def test_inquiry_type_of_derives_from_utility_type():
    meter = Meter(account_id="a1", meter_id="900000001", utility_type=UtilityType.ELECTRIC)
    assert inquiry_type_of(meter) == "hydro"

    water_meter = Meter(account_id="a1", meter_id="900000003", utility_type=UtilityType.WATER)
    assert inquiry_type_of(water_meter) == "water"


def test_parse_compare_accounts_options_handles_unexpected_text_shape():
    html = """
    <select name="compareAccounts">
    <option value="hydro_104758-000001_1">weird text with no dashes at all</option>
    </select>
    """
    results = _parse_compare_accounts_options(html)
    assert results == [("hydro", "104758-000001", "1", None, None)]


def test_first_row_meter_id_returns_none_when_tbody_has_no_rows():
    html = '<table id="consumptionTable"><tbody></tbody></table>'
    assert _first_row_meter_id(html) is None
