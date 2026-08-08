"""Bill history for the Westerville portal (`billInquirySelect`).

Ported from `utility-reader`'s `providers/westerville/bills.py`.

The `#billTable` markup the portal returns has its `<td>`s directly under
`<tbody>` with no `<tr>` wrapper (a real quirk of this JSP app, confirmed
against a live fetch) -- so we read cells in fixed groups of 4
(View Bill / Date / Bill Amount / Due Date) rather than iterating rows.

The table has no explicit billing-period-start column, only a bill date, so
we approximate `period_start` as the *previous* (older) bill's date -- bills
are contiguous roughly-monthly cycles, so this is accurate except for the
oldest bill fetched, where we fall back to 30 days before its bill date.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from .models import Account, Bill
from .shared import get

_LOGGER = logging.getLogger(__name__)

_FALLBACK_PERIOD_DAYS = 30


async def fetch_bills(client: httpx.AsyncClient, account: Account) -> list[Bill]:
    resp = await get(client, "/app/capricorn?para=billInquirySelect&tab=BILLINQ")
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="billTable")
    if table is None or table.find("tbody") is None:
        _LOGGER.debug("No bill table for account %s; nothing to sync", account.account_id)
        return []

    cells = [c.get_text(strip=True) for c in table.find("tbody").find_all("td")]
    rows = [cells[i : i + 4] for i in range(0, len(cells) - 3, 4)]

    parsed: list[tuple[datetime, float, datetime | None]] = []
    for _view_bill, date_text, amount_text, due_date_text in rows:
        try:
            bill_date = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=UTC)
            amount = float(amount_text.replace("$", "").replace(",", ""))
        except ValueError:
            _LOGGER.debug("Skipping unparseable bill row: %s", [date_text, amount_text, due_date_text])
            continue
        try:
            due_date = datetime.strptime(due_date_text, "%b %d, %Y").replace(tzinfo=UTC)
        except ValueError:
            due_date = None
        parsed.append((bill_date, amount, due_date))

    # Portal returns most-recent-first; keep that assumption explicit rather
    # than relying on incoming order.
    parsed.sort(key=lambda r: r[0], reverse=True)

    bills: list[Bill] = []
    for i, (bill_date, amount, due_date) in enumerate(parsed):
        period_start = parsed[i + 1][0] if i + 1 < len(parsed) else bill_date - timedelta(days=_FALLBACK_PERIOD_DAYS)
        bills.append(
            Bill(
                account_id=account.account_id,
                period_start=period_start,
                period_end=bill_date,
                amount_total=amount,
                due_date=due_date,
            )
        )

    _LOGGER.debug("Account %s: parsed %d bill(s)", account.account_id, len(bills))
    return bills
