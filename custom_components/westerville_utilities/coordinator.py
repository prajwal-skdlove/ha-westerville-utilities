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

from dataclasses import dataclass, field
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
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .client import Account, Bill, CannotConnect, InvalidAuth, Meter, UtilityType, authenticate
from .client.accounts import list_accounts, list_meters, supported_granularities
from .client.bills import fetch_bills
from .client.merge import merge_readings
from .client.models import Granularity
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
    last_updated: datetime = field(default_factory=dt_util.utcnow)


def _sanitize_id_part(value: str) -> str:
    """Make a value safe for use inside a statistic_id.

    Statistic ids only allow lowercase letters, digits, and underscores
    (see homeassistant.core.VALID_STATISTIC_ID) -- account ids like
    "104758-000001" contain a dash and would otherwise produce an invalid
    id and fail to import.
    """
    return value.lower().replace("-", "_").strip("_")


def _statistic_id(meter: Meter) -> str:
    return f"{DOMAIN}:{meter.utility_type.value}_{_sanitize_id_part(meter.meter_id)}"


def _bill_statistic_id(account: Account) -> str:
    return f"{DOMAIN}:{_sanitize_id_part(account.account_id)}_bill_amount"


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
        self._backfill_daily_days = options.get(CONF_BACKFILL_DAILY_DAYS, DEFAULT_BACKFILL_DAILY_DAYS)
        self._backfill_hourly_days = options.get(CONF_BACKFILL_HOURLY_DAYS, DEFAULT_BACKFILL_HOURLY_DAYS)
        # A dedicated client (not the shared HA-wide one) so this entry's
        # login-session cookies never mix with another entry's -- mirrors
        # Opower's async_create_clientsession(hass, cookie_jar=...) pattern.
        self._client: httpx.AsyncClient = create_async_httpx_client(
            hass, cookies=httpx.Cookies(), follow_redirects=True, timeout=30.0
        )

    async def _async_update_data(self) -> WestervilleData:
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
            return MeterData(meter=meter, latest_value=prior_sum or None)

        running_sum = prior_sum
        stats: list[StatisticData] = []
        for reading in merged:
            running_sum += reading.value
            stats.append(StatisticData(start=reading.period_start, state=reading.value, sum=running_sum))

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Westerville {meter.utility_type.value} meter {meter.meter_id}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=_CONSUMPTION_UNIT_CLASS_BY_UTILITY[meter.utility_type],
            unit_of_measurement=_CONSUMPTION_UNIT_BY_UTILITY[meter.utility_type],
        )
        _LOGGER.debug("Meter %s: importing %d new statistic(s)", meter.meter_id, len(stats))
        async_add_external_statistics(self.hass, metadata, stats)

        return MeterData(meter=meter, latest_value=running_sum)

    async def _async_update_bill_statistics(self, account: Account, bills: list[Bill]) -> None:
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
        statistic_id = _bill_statistic_id(account)
        recorder = get_instance(self.hass)

        last_stat = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        prior_sum = 0.0
        last_start: float | None = None
        if last_stat:
            row = last_stat[statistic_id][0]
            prior_sum = float(row.get("sum") or 0.0)
            last_start = row["start"]

        new_bills = sorted(bills, key=lambda b: b.period_start)
        if last_start is not None:
            new_bills = [b for b in new_bills if b.period_start.timestamp() > last_start]

        if not new_bills:
            _LOGGER.debug("Account %s: no new bills this cycle", account.account_id)
            return

        running_sum = prior_sum
        stats: list[StatisticData] = []
        for bill in new_bills:
            running_sum += bill.amount_total
            stats.append(StatisticData(start=bill.period_start, state=bill.amount_total, sum=running_sum))

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Westerville bill amount ({account.account_id})",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency or "USD",
        )
        _LOGGER.debug("Account %s: importing %d new bill statistic(s)", account.account_id, len(stats))
        async_add_external_statistics(self.hass, metadata, stats)
