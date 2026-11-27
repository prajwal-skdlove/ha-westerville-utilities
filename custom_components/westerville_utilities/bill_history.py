"""Per-bill detail kept across restarts, so past bills can be shown.

The portal lists about two years of bills, and only each bill's PDF holds the
line items and meter reads. A PDF is read once; what it held is saved here as
the same numbers and dates the latest-bill sensor shows. Never the PDF text,
its link, the customer's name or the address.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .client import Bill
from .const import DOMAIN

STORAGE_VERSION = 1

# account_id -> bill date (ISO) -> {"amount": float, "attributes": {...}}
type BillRecords = dict[str, dict[str, dict[str, Any]]]


def bill_key(bill: Bill) -> str:
    """The bill's date, which names it in the history and in the picker."""
    return bill.period_end.date().isoformat()


def bill_label(key: str) -> str:
    """A bill's key as shown in the picker, e.g. "Nov 5, 2026"."""
    day = date.fromisoformat(key)
    return f"{day:%b} {day.day}, {day.year}"


def bill_attributes(bill: Bill) -> dict[str, Any]:
    """Dates, component amounts, line items and meter reads of one bill."""
    attributes: dict[str, Any] = {
        "due_date": bill.due_date.isoformat() if bill.due_date else None,
        "period_start": bill.period_start.isoformat(),
        "period_end": bill.period_end.isoformat(),
    }

    details = bill.details
    if details.components:
        attributes["components"] = dict(details.components)
        attributes["line_items"] = [
            {
                "component": item.component,
                "description": item.description,
                "usage": item.quantity,
                "rate": item.rate,
                "amount": item.amount,
            }
            for item in details.line_items
        ]
    if details.meter_reads:
        # The dates the meters were actually billed for, unlike the period
        # above, which is only estimated from the neighbouring bill's date.
        attributes["billing_period_start"] = min(r.previous_read_date for r in details.meter_reads).date().isoformat()
        attributes["billing_period_end"] = max(r.current_read_date for r in details.meter_reads).date().isoformat()
        attributes["meter_reads"] = [
            {
                "service": read.service,
                "meter_id": read.meter_id,
                "read_type": read.read_type,
                "previous_read_date": read.previous_read_date.date().isoformat(),
                "current_read_date": read.current_read_date.date().isoformat(),
                "billing_days": read.billing_days,
                "previous_read": read.previous_read,
                "current_read": read.current_read,
                "multiplier": read.multiplier,
                "usage": read.usage,
                "unit": read.unit,
            }
            for read in details.meter_reads
        ]
    return attributes


def _store(hass: HomeAssistant, entry_id: str) -> Store[BillRecords]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.bills_{entry_id}")


async def async_remove_bill_history(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the saved history of a removed config entry."""
    await _store(hass, entry_id).async_remove()


class BillHistory:
    """The bills read so far, per account, saved in Home Assistant's storage."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _store(hass, entry_id)
        self._records: BillRecords | None = None

    async def async_load(self) -> None:
        if self._records is None:
            self._records = await self._store.async_load() or {}

    def records(self, account_id: str) -> dict[str, dict[str, Any]]:
        assert self._records is not None, "async_load() first"
        return self._records.get(account_id, {})

    def has_detail(self, account_id: str, key: str) -> bool:
        """Whether this bill's PDF has been read and held charges."""
        return bool(self.records(account_id).get(key, {}).get("attributes", {}).get("components"))

    async def async_update(self, account_id: str, bills: list[Bill]) -> None:
        """Add bills not seen before and fill in the detail of ones read now.

        A bill is never replaced by a version with less detail: older bills
        reach this method without their PDF read once it is saved.
        """
        records = dict(self.records(account_id))
        changed = False
        for bill in bills:
            key = bill_key(bill)
            attributes = bill_attributes(bill)
            if key in records and not (attributes.get("components") and not self.has_detail(account_id, key)):
                continue
            records[key] = {"amount": bill.amount_total, "attributes": attributes}
            changed = True
        if changed:
            assert self._records is not None
            self._records[account_id] = records
            await self._store.async_save(self._records)
