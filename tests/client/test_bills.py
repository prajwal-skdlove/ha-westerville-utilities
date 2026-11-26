"""Tests for client/bills.py: bill history parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from client import shared
from client.bills import download_bill_pdf, fetch_bills
from client.models import Account, Bill


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

ACCOUNT = Account(account_id="100000-000001")


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


# --- bill PDF link and download -------------------------------------------

PDF_PATH = "/app/viewBill?key=abc123"


def _linked_table(pdf_path: str | None) -> str:
    link = f'<a data-url="{pdf_path}" href="#">View</a>' if pdf_path else "<a>View</a>"
    return _bill_table(f"<td>{link}</td><td>Jul 30, 2026</td><td>$28.60</td><td>Aug 15, 2026</td>")


def _bill(pdf_url: str | None) -> Bill:
    return Bill(
        account_id="100000-000001",
        period_start=datetime(2026, 6, 26, tzinfo=UTC),
        period_end=datetime(2026, 7, 30, tzinfo=UTC),
        amount_total=28.60,
        pdf_url=pdf_url,
    )


@respx.mock
async def test_fetch_bills_keeps_each_rows_pdf_link(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=_linked_table(PDF_PATH)))

    async with httpx.AsyncClient() as client:
        parsed = await fetch_bills(client, ACCOUNT)

    assert parsed[0].pdf_url == PDF_PATH


@respx.mock
async def test_fetch_bills_without_a_link_has_no_pdf_url(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*billInquirySelect.*").mock(return_value=httpx.Response(200, text=_linked_table(None)))

    async with httpx.AsyncClient() as client:
        parsed = await fetch_bills(client, ACCOUNT)

    assert parsed[0].pdf_url is None


def test_the_pdf_link_is_not_in_a_bills_repr():
    assert "abc123" not in repr(_bill(PDF_PATH))


@respx.mock
async def test_download_bill_pdf_returns_the_pdf_bytes(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*viewBill.*").mock(return_value=httpx.Response(200, content=b"%PDF-1.4 body"))

    async with httpx.AsyncClient() as client:
        assert await download_bill_pdf(client, _bill(PDF_PATH)) == b"%PDF-1.4 body"


@respx.mock
async def test_download_bill_pdf_is_none_without_a_link():
    async with httpx.AsyncClient() as client:
        assert await download_bill_pdf(client, _bill(None)) is None


@respx.mock
async def test_download_bill_pdf_never_follows_a_link_off_the_portal():
    elsewhere = respx.get(url__regex=r".*example\.test.*").mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as client:
        assert await download_bill_pdf(client, _bill("https://example.test/bill.pdf")) is None
    assert not elsewhere.called


@respx.mock
async def test_download_bill_pdf_is_none_when_the_link_does_not_return_a_pdf(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(url__regex=r".*viewBill.*").mock(return_value=httpx.Response(200, text="<html>login</html>"))

    async with httpx.AsyncClient() as client:
        assert await download_bill_pdf(client, _bill(PDF_PATH)) is None
