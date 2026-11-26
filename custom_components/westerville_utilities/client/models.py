"""Plain data shapes for Westerville portal data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class UtilityType(str, Enum):
    ELECTRIC = "electric"
    WATER = "water"
    SEWER = "sewer"


class Granularity(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


# Lower value = finer (shorter) period.
GRANULARITY_FINENESS: dict[Granularity, int] = {
    Granularity.HOURLY: 0,
    Granularity.DAILY: 1,
    Granularity.MONTHLY: 2,
}


@dataclass(frozen=True)
class Account:
    account_id: str
    service_address: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Meter:
    account_id: str
    meter_id: str
    utility_type: UtilityType
    description: str | None = None
    service_address: str | None = None


@dataclass(frozen=True)
class Reading:
    meter_id: str
    granularity: Granularity
    period_start: datetime
    period_end: datetime
    value: float
    unit: str


@dataclass(frozen=True)
class BillLineItem:
    """One charge line from a bill's itemised PDF, e.g. a rate tier."""

    component: str  # electric, ev_charge, water, sewer, trash
    description: str
    amount: float
    quantity: float | None = None  # usage billed on this line, when it has one
    rate: float | None = None  # per-unit rate; negative for a credit


@dataclass(frozen=True)
class BillMeterRead:
    """One meter's read from a bill's PDF: the dates it was actually billed for."""

    service: str  # as printed, e.g. "Electric", "Ev Charge", "Water"
    meter_id: str
    read_type: str
    previous_read_date: datetime
    current_read_date: datetime
    billing_days: int
    previous_read: float
    current_read: float
    multiplier: float
    usage: float
    unit: str


@dataclass(frozen=True)
class BillDetails:
    """What a bill's PDF holds; empty when it was not read."""

    components: dict[str, float] = field(default_factory=dict)
    line_items: tuple[BillLineItem, ...] = ()
    meter_reads: tuple[BillMeterRead, ...] = ()


@dataclass(frozen=True)
class Bill:
    account_id: str
    period_start: datetime
    period_end: datetime
    amount_total: float
    due_date: datetime | None = None
    # Path of the bill's PDF on the portal. It carries an opaque key: keep it
    # in memory, never in an attribute, statistic or log line.
    pdf_url: str | None = field(default=None, repr=False)
    details: BillDetails = field(default_factory=BillDetails)
