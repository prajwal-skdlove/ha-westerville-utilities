"""Tests for client/bills.py: bill history parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from client import shared
from client.bills import fetch_bills
from client.models import Account


def _bill_table(rows_html: str) -> str:
    return f"""
    <html><body>
    <table id="billTable">
    <thead><tr><th>View Bill</th><th>Date</th><th>Bill Amount</th><th>Due Date</th></tr></thead>
    <tbody>
    {rows_html}
    </tbody>
    </table>
    </body></html>
    """


# Matches the real portal's quirk: <td>s directly under <tbody>, no <tr>.
THREE_BILLS = _bill_table(
    """
    <td><a>View</a></td><td>Jul 30, 2026</td><td>$462.11</td><td>Aug 15, 2026</td>
    <td><a>View</a></td><td>Jun 26, 2026</td><td>$413.76</td><td>Jul 15, 2026</td>
    <td><a>View</a></td><td>May 29, 2026</td><td>$325.40</td><td>Jun 15, 2026</td>
    """
)

WITH_MALFORMED_AND_BAD_DUE_DATE = _bill_table(
    """
    <td><a>View</a></td><td>Jul 30, 2026</td><td>$100.00</td><td>not-a-date</td>
    <td><a>View</a></td><td>not-a-date</td><td>$1.00</td><td>Aug 1, 2026</td>
    """
)

EMPTY_TABLE = _bill_table("")

NO_TABLE = "<html><body>no bill table</body></html>"

ACCOUNT = Account(account_id="104758-000001")


@respx.mock
async def test_fetch_bills_parses_amount_and_dates(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=THREE_BILLS))

    async with httpx.AsyncClient() as client:
        parsed = await fetch_bills(client, ACCOUNT)

    assert len(parsed) == 3
    newest = next(b for b in parsed if b.period_end == datetime(2026, 7, 30, tzinfo=UTC))
    assert newest.amount_total == 462.11
    assert newest.due_date == datetime(2026, 8, 15, tzinfo=UTC)
    # period_start derived from the next (older) bill's date.
    assert newest.period_start == datetime(2026, 6, 26, tzinfo=UTC)


@respx.mock
async def test_fetch_bills_oldest_bill_uses_fallback_period_start(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=THREE_BILLS))

    async with httpx.AsyncClient() as client:
        parsed = await fetch_bills(client, ACCOUNT)

    oldest = next(b for b in parsed if b.period_end == datetime(2026, 5, 29, tzinfo=UTC))
    assert oldest.period_start == datetime(2026, 5, 29, tzinfo=UTC) - timedelta(days=30)


@respx.mock
async def test_fetch_bills_skips_malformed_and_handles_bad_due_date(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=WITH_MALFORMED_AND_BAD_DUE_DATE))

    async with httpx.AsyncClient() as client:
        parsed = await fetch_bills(client, ACCOUNT)

    assert len(parsed) == 1
    assert parsed[0].amount_total == 100.00
    assert parsed[0].due_date is None


@respx.mock
async def test_fetch_bills_empty_table(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=EMPTY_TABLE))

    async with httpx.AsyncClient() as client:
        assert await fetch_bills(client, ACCOUNT) == []


@respx.mock
async def test_fetch_bills_missing_table(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=NO_TABLE))

    async with httpx.AsyncClient() as client:
        assert await fetch_bills(client, ACCOUNT) == []
