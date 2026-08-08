"""Accounts/meters discovery for the Westerville portal.

Ported from `utility-reader`'s `providers/westerville/accounts.py`.

An account can have more than one physical meter per utility (e.g. a main
"Electric" meter plus a separate "EV CHARGE" submeter, confirmed live on a
real account) -- these are NOT alternate IDs for the same meter, they are
genuinely distinct meters with their own billing and (sometimes) AMI
history. The real list of meters per inquiryType comes from the
`compareAccounts` multiselect on the billed-usage (`consumptionInquiry`)
page: each `<option>` encodes one meter as
`value="{inquiryType}_{accountId}_{meterId}"` with human text
`"{address} - {meterId} - {description} - {accountId}"`. That option value
also doubles as the `compareAccounts` query param needed to fetch *that*
meter's billed-usage rows (see usage.py) -- without it, the portal only ever
returns whichever meter happens to be selected by default, silently hiding
the others.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from .models import Account, Granularity, Meter, UtilityType
from .shared import (
    CONSUMPTION_REPORT_BY_INQUIRY_TYPE,
    INQUIRY_TYPE_TO_UTILITY,
    UTILITY_TO_INQUIRY_TYPE,
    get,
    parse_ami_fragment,
    subtitle_meter_id,
)

_LOGGER = logging.getLogger(__name__)

_ACCOUNT_NUMBER_RE = re.compile(r"inAccountNumber=([\w-]+)")


def _account_address(html: str, account_id: str) -> str | None:
    # The selectAccount page prints "{accountId}: {address}" right next to
    # the account link/dropdown entry.
    match = re.search(rf"{re.escape(account_id)}:\s*([^\n<]+)", html)
    if not match:
        return None
    return " ".join(match.group(1).split()) or None


async def _account_holder_name(client: httpx.AsyncClient) -> str | None:
    resp = await get(client, "/app/capricorn?para=myAccount&tab=MYACCT")
    soup = BeautifulSoup(resp.text, "html.parser")
    first = soup.find("input", {"name": "firstName"})
    last = soup.find("input", {"name": "lastName"})
    parts = [i.get("value", "").strip() for i in (first, last) if i is not None and i.get("value")]
    return " ".join(parts) if parts else None


async def list_accounts(client: httpx.AsyncClient) -> list[Account]:
    resp = await get(client, "/app/capricorn?para=selectAccount")
    account_ids = sorted(set(_ACCOUNT_NUMBER_RE.findall(resp.text)))
    if not account_ids:
        _LOGGER.warning("No accounts found on the Westerville dashboard for this login")
        return []

    name = await _account_holder_name(client)
    _LOGGER.debug("Found %d Westerville account(s)", len(account_ids))
    return [
        Account(
            account_id=account_id,
            service_address=_account_address(resp.text, account_id),
            name=name,
        )
        for account_id in account_ids
    ]


def inquiry_type_of(meter: Meter) -> str:
    return UTILITY_TO_INQUIRY_TYPE[meter.utility_type.value]


def _parse_compare_accounts_options(html: str) -> list[tuple[str, str, str, str | None, str | None]]:
    """Returns `(inquiry_type, account_id, meter_id, description, address)` for
    every meter listed in the page's `compareAccounts` selector.
    """
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"name": "compareAccounts"})
    if select is None:
        return []

    results = []
    for option in select.find_all("option"):
        value = option.get("value") or ""
        parts = value.split("_", 2)
        if len(parts) != 3:
            _LOGGER.debug("Skipping unparseable compareAccounts option value: %r", value)
            continue
        inquiry_type, account_id, meter_id = parts

        text_parts = option.get_text(strip=True).split(" - ")
        address = description = None
        if len(text_parts) == 4:
            address = text_parts[0] or None
            description = text_parts[2] or None
        else:
            _LOGGER.debug("Unexpected compareAccounts option text shape: %r", option.get_text(strip=True))

        results.append((inquiry_type, account_id, meter_id, description, address))
    return results


def _consumption_table_has_rows(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="consumptionTable")
    if table is None:
        return False
    tbody = table.find("tbody")
    return tbody is not None and tbody.find("tr") is not None


def _first_row_meter_id(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="consumptionTable")
    tbody = table.find("tbody") if table else None
    row = tbody.find("tr") if tbody else None
    if row is None:
        return None
    cells = row.find_all(["td", "th"])
    return cells[0].get_text(strip=True) if cells else None


async def list_meters(client: httpx.AsyncClient, account: Account) -> list[Meter]:
    meters: list[Meter] = []
    for inquiry_type, utility_value in INQUIRY_TYPE_TO_UTILITY.items():
        report = CONSUMPTION_REPORT_BY_INQUIRY_TYPE.get(inquiry_type, "")
        query = f"/app/capricorn?para=consumptionInquiry&inquiryType={inquiry_type}&tab=probe"
        if report:
            query += f"&report={report}"
        resp = await get(client, query)

        options = _parse_compare_accounts_options(resp.text)
        if options:
            for opt_inquiry_type, opt_account_id, meter_id, description, address in options:
                meters.append(
                    Meter(
                        account_id=opt_account_id,
                        meter_id=meter_id,
                        utility_type=UtilityType(INQUIRY_TYPE_TO_UTILITY[opt_inquiry_type]),
                        description=description,
                        service_address=address,
                    )
                )
            _LOGGER.debug(
                "Account %s has %d %s meter(s): %s",
                account.account_id, len(options), inquiry_type, [o[2] for o in options],
            )
        elif _consumption_table_has_rows(resp.text):
            # No compareAccounts selector rendered (seen for some layouts) but
            # there's still billed-usage history -- fall back to the single
            # meter the table itself reports rather than losing it entirely.
            meter_id = _first_row_meter_id(resp.text)
            if meter_id:
                meters.append(
                    Meter(
                        account_id=account.account_id,
                        meter_id=meter_id,
                        utility_type=UtilityType(utility_value),
                    )
                )
                _LOGGER.debug(
                    "Account %s has one %s meter (no compareAccounts selector): %s",
                    account.account_id, inquiry_type, meter_id,
                )
        else:
            _LOGGER.debug("Account %s has no %s billed-usage history; skipping", account.account_id, inquiry_type)

    _LOGGER.debug("Account %s has %d active meter(s)/service(s)", account.account_id, len(meters))
    return meters


async def supported_granularities(client: httpx.AsyncClient, meter: Meter) -> list[Granularity]:
    inquiry_type = inquiry_type_of(meter)
    resp = await get(
        client,
        f"/app/capricorn?para=smartMeterConsumV3&inquiryType={inquiry_type}&tab=SMCONSUM&selectedMeterId={meter.meter_id}",
    )
    # A meter with no AMI registration isn't rejected -- the portal silently
    # falls back to whatever meter was last active in the session and
    # returns *that* meter's data (confirmed live). Only trust a response
    # whose subtitle actually reports the meter we asked for.
    if subtitle_meter_id(resp.text) != meter.meter_id:
        _LOGGER.debug(
            "Meter %s: AMI probe returned a different meter's data (server fallback); treating as no AMI data",
            meter.meter_id,
        )
        return [Granularity.MONTHLY]
    if parse_ami_fragment(resp.text) is not None:
        _LOGGER.debug("Meter %s has advanced-meter (AMI) data available: hourly/daily supported", meter.meter_id)
        return [Granularity.HOURLY, Granularity.DAILY, Granularity.MONTHLY]
    _LOGGER.debug("Meter %s has no advanced-meter data; monthly only", meter.meter_id)
    return [Granularity.MONTHLY]
