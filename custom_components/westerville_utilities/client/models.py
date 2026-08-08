"""Plain data shapes for Westerville portal data."""

from __future__ import annotations

from dataclasses import dataclass
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
class Bill:
    account_id: str
    period_start: datetime
    period_end: datetime
    amount_total: float
    due_date: datetime | None = None
