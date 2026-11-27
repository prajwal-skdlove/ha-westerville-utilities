"""Coordinator for the Westerville Utilities integration.

Logs in to the portal, fetches accounts/meters/readings/bills, and imports
historical readings into Home Assistant's long-term statistics so usage
shows up correctly in the Energy dashboard -- mirroring how the built-in
Opower integration (`homeassistant/components/opower`) feeds the Energy
dashboard from a utility portal with no native HA statistics API.

We use *external* statistics (`async_add_external_statistics`, ids of the
form `westerville_utilities:<meter>`) rather than statistics tied to a
sensor entity's own id. That's a deliberate choice, not a shortcut: entity
IDs can be renamed by users and don't exist yet on the very first
coordinator refresh (which runs before entities are created), so tying
long-term history to one would be fragile. The per-meter sensor entities in
sensor.py still show current usage; the external statistic is what feeds
the Energy dashboard with full history.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging

import httpx

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .bill_history import BillHistory, BillRecords, bill_key
from .client import Account, Bill, CannotConnect, InvalidAuth, Meter, UtilityType, authenticate
from .client.accounts import list_accounts, list_meters, supported_granularities
from .client.bill_pdf import read_bill
from .client.bills import download_bill_pdf, fetch_bills
from .client.merge import merge_readings
from .client.models import Granularity
from .client.shared import redact_link_keys
from .client.usage import fetch_daily, fetch_hourly, fetch_monthly
from .const import (
    CONF_BACKFILL_DAILY_DAYS,
    CONF_BACKFILL_HOURLY_DAYS,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_BACKFILL_DAILY_DAYS,
    DEFAULT_BACKFILL_HOURLY_DAYS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type WestervilleConfigEntry = ConfigEntry[WestervilleCoordinator]

# Overlap applied on top of the last imported statistic's start time when
# doing an incremental (non-backfill) fetch, to catch late corrections the
# portal sometimes makes to recent daily/hourly AMI data.
INCREMENTAL_OVERLAP = timedelta(days=3)

_CONSUMPTION_UNIT_BY_UTILITY = {
    UtilityType.ELECTRIC: UnitOfEnergy.KILO_WATT_HOUR,
    UtilityType.WATER: UnitOfVolume.CUBIC_FEET,
}
_CONSUMPTION_UNIT_CLASS_BY_UTILITY = {
    UtilityType.ELECTRIC: EnergyConverter.UNIT_CLASS,
    UtilityType.WATER: VolumeConverter.UNIT_CLASS,
}


@dataclass
class MeterData:
    """Latest known state for one meter, for the sensor platform."""

    meter: Meter
    latest_value: float | None


@dataclass
class WestervilleData:
    """Data handed to sensor entities after each coordinator refresh."""

    accounts: dict[str, Account]
    meters: dict[str, MeterData]
    bills: dict[str, Bill]  # account_id -> most recent bill
    bill_history: BillRecords  # every bill read so far, for the bill month picker
    last_updated: datetime = field(default_factory=dt_util.utcnow)


def _sanitize_id_part(value: str) -> str:
    """Make a value safe for use inside a statistic_id.

    Statistic ids only allow lowercase letters, digits, and underscores
    (see homeassistant.core.VALID_STATISTIC_ID) -- account ids like
    "100000-000001" contain a dash and would otherwise produce an invalid
    id and fail to import.
    """
    return value.lower().replace("-", "_").strip("_")


def _statistic_id(meter: Meter) -> str:
    return f"{DOMAIN}:{meter.utility_type.value}_{_sanitize_id_part(meter.meter_id)}"


def _meter_metadata(meter: Meter) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"Westerville {meter.utility_type.value} meter {meter.meter_id}",
        source=DOMAIN,
        statistic_id=_statistic_id(meter),
        unit_class=_CONSUMPTION_UNIT_CLASS_BY_UTILITY[meter.utility_type],
        unit_of_measurement=_CONSUMPTION_UNIT_BY_UTILITY[meter.utility_type],
    )


def _bill_statistic_id(account: Account) -> str:
    return f"{DOMAIN}:{_sanitize_id_part(account.account_id)}_bill_amount"


def _bill_amount(bill: Bill) -> float:
    return bill.amount_total


def _bill_component_statistic_id(account: Account, component: str) -> str:
    return f"{DOMAIN}:{_sanitize_id_part(account.account_id)}_bill_cost_{component}"


def _bill_cost_statistic_id(account: Account) -> str:
    return f"{DOMAIN}:{_sanitize_id_part(account.account_id)}_bill_cost"


def _bill_cost_start(bill: Bill) -> datetime:
    """Local midnight on the bill's period_end date.

    The portal gives a bare date, parsed as UTC midnight. In a US timezone
    that instant is the previous local evening, so a bill dated the 1st
    would land in the previous month's cost.
    """
    return dt_util.start_of_local_day(bill.period_end.date())


def _with_month_anchors(
    stats: list[StatisticData], prior_start_ts: float | None, prior_sum: float
) -> list[StatisticData]:
    """Add zero-change rows at local midnight on the 1st of each month.

    A meter billed monthly yields about one row a month, so a calendar
    month can hold none -- and a month with no row is absent from Home
    Assistant's monthly statistics instead of showing a change of zero.
    Each anchor repeats the cumulative sum of the row before it.

    `stats` must be sorted by start. Anchors cover the span from the
    last already-imported row (`prior_start_ts`, if any) to the last new
    row, and never go past it: the newest row has to stay a real one, or
    the next refresh would read an anchor's start as "newest imported" and
    drop real readings dated before it. A real row at the same instant
    always wins over an anchor.
    """
    if not stats:
        return stats
    starts = [s["start"] for s in stats]
    first = dt_util.utc_from_timestamp(prior_start_ts) if prior_start_ts is not None else starts[0]
    last = starts[-1]
    taken = set(starts)

    anchors: list[StatisticData] = []
    month = dt_util.as_local(first).date().replace(day=1)
    while (anchor_start := dt_util.start_of_local_day(month)) < last:
        if anchor_start > first and anchor_start not in taken:
            idx = bisect_left(starts, anchor_start)
            carried_sum = stats[idx - 1]["sum"] if idx else prior_sum
            anchors.append(StatisticData(start=anchor_start, state=0.0, sum=carried_sum))
        month = (month + timedelta(days=32)).replace(day=1)
    return sorted(stats + anchors, key=lambda s: s["start"])


class WestervilleCoordinator(DataUpdateCoordinator[WestervilleData]):
    """Fetches Westerville Utilities data and updates HA statistics."""

    config_entry: WestervilleConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: WestervilleConfigEntry) -> None:
        options = config_entry.options
        update_interval_hours = options.get(CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(hours=update_interval_hours),
        )
        self._username: str = config_entry.data[CONF_USERNAME]
        self._password: str = config_entry.data[CONF_PASSWORD]
        self._bill_history = BillHistory(hass, config_entry.entry_id)
        self._backfill_daily_days = options.get(CONF_BACKFILL_DAILY_DAYS, DEFAULT_BACKFILL_DAILY_DAYS)
        self._backfill_hourly_days = options.get(CONF_BACKFILL_HOURLY_DAYS, DEFAULT_BACKFILL_HOURLY_DAYS)
        # A dedicated client (not the shared HA-wide one) so this entry's
        # login-session cookies never mix with another entry's -- mirrors
        # Opower's async_create_clientsession(hass, cookie_jar=...) pattern.
        self._client: httpx.AsyncClient = create_async_httpx_client(
            hass, cookies=httpx.Cookies(), follow_redirects=True, timeout=30.0
        )

    async def _async_update_data(self) -> WestervilleData:
        await self._bill_history.async_load()
        try:
            await authenticate(self._client, self._username, self._password)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed("Westerville rejected the account credentials") from err
        except CannotConnect as err:
            raise UpdateFailed(f"Could not reach the Westerville portal: {err}") from err

        try:
            accounts = await list_accounts(self._client)
        except httpx.HTTPError as err:
            raise UpdateFailed(f"Error listing Westerville accounts: {err}") from err

        if not accounts:
            raise UpdateFailed("Logged in to Westerville but found no accounts on this login")

        accounts_by_id: dict[str, Account] = {}
        meters_by_id: dict[str, MeterData] = {}
        bills_by_account: dict[str, Bill] = {}

        for account in accounts:
            accounts_by_id[account.account_id] = account

            try:
                bills = await fetch_bills(self._client, account)
            except httpx.HTTPError as err:
                _LOGGER.warning("Error fetching bills for account %s: %s", account.account_id, err)
                bills = []
            if bills:
                bills = await self._async_attach_bill_details(account, bills)
                await self._bill_history.async_update(account.account_id, bills)
                bills_by_account[account.account_id] = max(bills, key=lambda b: b.period_end)
                await self._async_update_bill_statistics(account, bills)

            try:
                meters = await list_meters(self._client, account)
            except httpx.HTTPError as err:
                raise UpdateFailed(
                    f"Error listing meters for account {account.account_id}: {err}"
                ) from err

            for meter in meters:
                if meter.utility_type not in _CONSUMPTION_UNIT_BY_UTILITY:
                    # Sewer has no AMI/consumption data of its own; nothing
                    # to build a statistic series from.
                    continue
                try:
                    meters_by_id[meter.meter_id] = await self._async_update_meter_statistics(meter)
                except httpx.HTTPError as err:
                    _LOGGER.warning(
                        "Error fetching readings for meter %s this cycle; will retry next cycle: %s",
                        meter.meter_id, err,
                    )

        return WestervilleData(
            accounts=accounts_by_id,
            meters=meters_by_id,
            bills=bills_by_account,
            bill_history={
                account_id: self._bill_history.records(account_id) for account_id in accounts_by_id
            },
        )

    async def _async_update_meter_statistics(self, meter: Meter) -> MeterData:
        """Fetch new readings for one meter and import them as HA statistics.

        Returns the meter's latest known cumulative value (for the sensor's
        live state), regardless of whether any *new* statistics were found
        this cycle.
        """
        statistic_id = _statistic_id(meter)
        recorder = get_instance(self.hass)

        last_stat = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        backfill = not last_stat
        prior_sum = 0.0
        last_start: float | None = None
        since = None
        if last_stat:
            row = last_stat[statistic_id][0]
            prior_sum = float(row.get("sum") or 0.0)
            last_start = row["start"]
            since = dt_util.utc_from_timestamp(last_start) - INCREMENTAL_OVERLAP

        granularities = await supported_granularities(self._client, meter)
        # Meters with hourly/daily data have rows in every month already.
        monthly_only = granularities == [Granularity.MONTHLY]

        readings = list(await fetch_monthly(self._client, meter))
        if Granularity.DAILY in granularities:
            readings += await fetch_daily(
                self._client, meter, since=since, backfill=backfill,
                backfill_lookback_days=self._backfill_daily_days,
            )
        if Granularity.HOURLY in granularities:
            readings += await fetch_hourly(
                self._client, meter, since=since, backfill=backfill,
                backfill_lookback_days=self._backfill_hourly_days,
            )

        merged = merge_readings(readings)
        if last_start is not None:
            merged = [r for r in merged if r.period_start.timestamp() > last_start]

        if not merged:
            _LOGGER.debug("Meter %s: no new readings this cycle", meter.meter_id)
            if monthly_only:
                await self._async_backfill_month_anchors(_meter_metadata(meter))
            return MeterData(meter=meter, latest_value=prior_sum or None)

        running_sum = prior_sum
        stats: list[StatisticData] = []
        for reading in merged:
            running_sum += reading.value
            stats.append(StatisticData(start=reading.period_start, state=reading.value, sum=running_sum))
        stats = _with_month_anchors(stats, last_start, prior_sum)

        metadata = _meter_metadata(meter)
        _LOGGER.debug("Meter %s: importing %d new statistic(s)", meter.meter_id, len(stats))
        async_add_external_statistics(self.hass, metadata, stats)
        if monthly_only:
            await self._async_backfill_month_anchors(metadata)

        return MeterData(meter=meter, latest_value=running_sum)

    async def _async_update_bill_statistics(self, account: Account, bills: list[Bill]) -> None:
        """Import bill amounts as two cumulative statistics.

        `_bill_amount` is dated at each bill's period_start (the original
        series, kept so existing history stays valid). `_bill_cost` is
        dated at period_end, when the cost is actually incurred.

        The portal only returns its most recent ~2 years of bills, but
        `_bill_amount` keeps every bill ever imported. `_bill_cost` came
        later, so on an existing install its first import would start at
        the portal's oldest bill. The older bills are recovered from
        `_bill_amount`, and a cost series that lacks them is rebuilt once.
        """
        amount_statistic_id = _bill_statistic_id(account)
        amount_name = f"Westerville bill amount ({account.account_id})"
        await self._async_import_bill_series(
            account,
            bills,
            statistic_id=amount_statistic_id,
            name=amount_name,
            dated_at=lambda bill: bill.period_start,
        )

        cost_statistic_id = _bill_cost_statistic_id(account)
        cost_name = f"Westerville bill cost ({account.account_id})"
        older_bills = await self._async_bills_missing_from_portal(account, bills)
        cost_rows = await self._async_statistic_rows(cost_statistic_id)
        # Older bills shift the cumulative sum of every later row, so the
        # whole series is rewritten rather than appended to.
        rebuild = bool(
            older_bills
            and cost_rows
            and _bill_cost_start(older_bills[0]).timestamp() < min(row["start"] for row in cost_rows)
        )
        await self._async_import_bill_series(
            account,
            older_bills + bills,
            statistic_id=cost_statistic_id,
            name=cost_name,
            dated_at=_bill_cost_start,
            rebuild=rebuild,
        )

        for statistic_id, name in ((amount_statistic_id, amount_name), (cost_statistic_id, cost_name)):
            await self._async_backfill_month_anchors(self._bill_metadata(statistic_id, name))

        await self._async_update_component_statistics(account, bills)

    async def _async_attach_bill_details(self, account: Account, bills: list[Bill]) -> list[Bill]:
        """Read the PDF of each bill that has not been read yet and attach what it holds.

        A bill's PDF is read once: bills dated after the newest row of the
        electric component series, and bills whose detail the bill history
        does not hold yet (so the first run after an update reads the older
        ones). The newest bill is always read, because the latest-bill
        sensor shows its detail.
        """
        electric_rows = await self._async_statistic_rows(_bill_component_statistic_id(account, "electric"))
        newest_read = max((row["start"] for row in electric_rows), default=None)
        latest = max(bills, key=lambda bill: bill.period_end)
        return [
            await self._async_read_bill(bill)
            if bill is latest
            or newest_read is None
            or _bill_cost_start(bill).timestamp() > newest_read
            or not self._bill_history.has_detail(account.account_id, bill_key(bill))
            else bill
            for bill in bills
        ]

    async def _async_read_bill(self, bill: Bill) -> Bill:
        label = bill.period_end.date().isoformat()
        try:
            pdf = await download_bill_pdf(self._client, bill)
        except httpx.HTTPError as err:
            # The error text contains the bill link, key included.
            _LOGGER.warning(
                "Bill %s: could not download the PDF (%s); detail left empty", label, redact_link_keys(str(err))
            )
            return bill
        if pdf is None:
            return bill
        # pypdf is synchronous and CPU-bound, so keep it off the event loop.
        details = await self.hass.async_add_executor_job(read_bill, pdf, bill.amount_total, label)
        return replace(bill, details=details)

    async def _async_update_component_statistics(self, account: Account, bills: list[Bill]) -> None:
        """Import each bill component (electric, EV charge, water, ...) as its own cumulative cost series."""
        for component in sorted({key for bill in bills for key in bill.details.components}):
            statistic_id = _bill_component_statistic_id(account, component)
            name = f"Westerville bill cost, {component.replace('_', ' ')} ({account.account_id})"
            await self._async_import_bill_series(
                account,
                [bill for bill in bills if component in bill.details.components],
                statistic_id=statistic_id,
                name=name,
                dated_at=_bill_cost_start,
                amount_of=lambda bill, component=component: bill.details.components[component],
            )
            await self._async_backfill_month_anchors(self._bill_metadata(statistic_id, name))

    async def _async_statistic_rows(self, statistic_id: str) -> list[StatisticsRow]:
        """Every stored row of a statistic, oldest first, with `state` and `sum`."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.utc_from_timestamp(0),
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        return rows.get(statistic_id, [])

    async def _async_bills_missing_from_portal(self, account: Account, bills: list[Bill]) -> list[Bill]:
        """Bills older than the portal's oldest, rebuilt from `_bill_amount`.

        Each `_bill_amount` row starts at its bill's period_start and holds
        the amount as `state`, so a bill ends where the next row starts.
        Rows with a zero state are month anchors, not bills.
        """
        rows = [row for row in await self._async_statistic_rows(_bill_statistic_id(account)) if row["state"]]
        portal_oldest_end = min(bill.period_end for bill in bills)
        older: list[Bill] = []
        for row, next_row in zip(rows, rows[1:]):
            period_end = dt_util.utc_from_timestamp(next_row["start"])
            if period_end >= portal_oldest_end:
                break
            older.append(
                Bill(
                    account_id=account.account_id,
                    period_start=dt_util.utc_from_timestamp(row["start"]),
                    period_end=period_end,
                    amount_total=row["state"],
                )
            )
        return older

    async def _async_import_bill_series(
        self,
        account: Account,
        bills: list[Bill],
        *,
        statistic_id: str,
        name: str,
        dated_at: Callable[[Bill], datetime],
        amount_of: Callable[[Bill], float] = _bill_amount,
        rebuild: bool = False,
    ) -> None:
        """Import bill amounts as a cumulative HA statistic, month over month.

        Mirrors _async_update_meter_statistics's running-sum approach so
        bill cost is graphable in HA's history/statistics views the same
        way meter usage is -- one point per billing period, sum
        monotonically increasing (total billed to date). `unit_class` is
        None because currency isn't a convertible "unit class" in HA's unit
        system the way energy/volume are -- but `unit_of_measurement` is
        still set to a real string (the account's configured currency),
        since a chart card needs *some* unit to label its axis, and a
        statistic with no unit at all may not show up as selectable there
        even though the data exists.
        """
        recorder = get_instance(self.hass)

        # A rebuild ignores what is stored and re-imports every bill from a
        # zero sum, which overwrites the existing rows by start time.
        last_stat = (
            {}
            if rebuild
            else await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
        )
        prior_sum = 0.0
        last_start: float | None = None
        if last_stat:
            row = last_stat[statistic_id][0]
            prior_sum = float(row.get("sum") or 0.0)
            last_start = row["start"]

        new_bills = sorted(bills, key=dated_at)
        if last_start is not None:
            new_bills = [b for b in new_bills if dated_at(b).timestamp() > last_start]

        if not new_bills:
            _LOGGER.debug("Account %s: no new bills for %s this cycle", account.account_id, statistic_id)
            return

        running_sum = prior_sum
        stats: list[StatisticData] = []
        for bill in new_bills:
            running_sum += amount_of(bill)
            stats.append(StatisticData(start=dated_at(bill), state=amount_of(bill), sum=running_sum))
        stats = _with_month_anchors(stats, last_start, prior_sum)

        _LOGGER.debug("Account %s: importing %d new statistic(s) for %s", account.account_id, len(stats), statistic_id)
        async_add_external_statistics(self.hass, self._bill_metadata(statistic_id, name), stats)

    def _bill_metadata(self, statistic_id: str, name: str) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency or "USD",
        )

    async def _async_backfill_month_anchors(self, metadata: StatisticMetaData) -> None:
        """Add the month anchors a statistic's stored history is missing.

        Anchors are only added to new imports, so history imported earlier
        has none. Every 1st-of-month local midnight between the first and
        the newest stored row that has no row gets one; nothing is added
        past the newest row. Rows come from what the recorder has already
        committed, so rows imported in this same refresh are covered by the
        next one. Once every such start has a row, this adds nothing.
        """
        statistic_id = metadata["statistic_id"]
        stored = [row for row in await self._async_statistic_rows(statistic_id) if row["sum"] is not None]
        if len(stored) < 2:
            return
        stored_stats = [
            StatisticData(start=dt_util.utc_from_timestamp(row["start"]), state=row["state"] or 0.0, sum=row["sum"])
            for row in sorted(stored, key=lambda row: row["start"])
        ]
        stored_starts = {stat["start"] for stat in stored_stats}
        missing = [
            stat for stat in _with_month_anchors(stored_stats, None, 0.0) if stat["start"] not in stored_starts
        ]
        if not missing:
            return
        _LOGGER.debug("Backfilling %d month anchor(s) for %s", len(missing), statistic_id)
        async_add_external_statistics(self.hass, metadata, missing)
