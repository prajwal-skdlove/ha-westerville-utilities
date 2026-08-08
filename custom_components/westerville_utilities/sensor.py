"""Sensor platform for Westerville Utilities.

Two kinds of entities:
  * One per meter: current cumulative usage (device_class energy/water,
    state_class total_increasing). Full historical depth is imported
    directly into HA's long-term statistics by the coordinator (see
    coordinator.py) rather than derived from this entity's state history.
  * One per account: the most recent bill, with due date/billing
    period/service address/account holder as attributes. A plain sensor,
    not statistics-backed.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import UtilityType
from .const import DOMAIN
from .coordinator import MeterData, WestervilleConfigEntry, WestervilleCoordinator

PARALLEL_UPDATES = 0

_METER_UNIT = {
    UtilityType.ELECTRIC: UnitOfEnergy.KILO_WATT_HOUR,
    UtilityType.WATER: UnitOfVolume.CUBIC_FEET,
}
_METER_DEVICE_CLASS = {
    UtilityType.ELECTRIC: SensorDeviceClass.ENERGY,
    UtilityType.WATER: SensorDeviceClass.WATER,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WestervilleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Westerville Utilities sensors."""
    coordinator = entry.runtime_data
    created: set[str] = set()

    @callback
    def _update_entities() -> None:
        new_entities: list[SensorEntity] = []
        for meter_id, meter_data in coordinator.data.meters.items():
            if meter_id not in created and meter_data.meter.utility_type in _METER_UNIT:
                created.add(meter_id)
                new_entities.append(WestervilleMeterSensor(coordinator, meter_id))
        for account_id in coordinator.data.accounts:
            key = f"bill_{account_id}"
            if key not in created:
                created.add(key)
                new_entities.append(WestervilleBillSensor(coordinator, account_id))
        if new_entities:
            async_add_entities(new_entities)

    _update_entities()
    entry.async_on_unload(coordinator.async_add_listener(_update_entities))


class WestervilleMeterSensor(CoordinatorEntity[WestervilleCoordinator], SensorEntity):
    """A single Westerville meter's cumulative usage."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "meter_usage"

    def __init__(self, coordinator: WestervilleCoordinator, meter_id: str) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        meter = coordinator.data.meters[meter_id].meter
        device_name = f"{meter.description or meter.utility_type.value.title()} ({meter_id})"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{meter_id}"
        self._attr_device_class = _METER_DEVICE_CLASS[meter.utility_type]
        self._attr_native_unit_of_measurement = _METER_UNIT[meter.utility_type]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id)},
            name=device_name,
            manufacturer="Westerville Utilities",
            model=meter.utility_type.value.title(),
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return super().available and self._meter_id in self.coordinator.data.meters

    @property
    def native_value(self) -> StateType:
        data: MeterData = self.coordinator.data.meters[self._meter_id]
        return data.latest_value


class WestervilleBillSensor(CoordinatorEntity[WestervilleCoordinator], SensorEntity):
    """Most recent bill for a Westerville account."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_translation_key = "latest_bill"

    def __init__(self, coordinator: WestervilleCoordinator, account_id: str) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{account_id}_bill"
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name=f"Westerville account {account_id}",
            manufacturer="Westerville Utilities",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return super().available and self._account_id in self.coordinator.data.bills

    @property
    def native_value(self) -> StateType:
        bill = self.coordinator.data.bills.get(self._account_id)
        return bill.amount_total if bill else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        bill = self.coordinator.data.bills.get(self._account_id)
        if bill is None:
            return {}
        account = self.coordinator.data.accounts.get(self._account_id)
        return {
            "due_date": bill.due_date.isoformat() if bill.due_date else None,
            "period_start": bill.period_start.isoformat(),
            "period_end": bill.period_end.isoformat(),
            "service_address": account.service_address if account else None,
            "account_holder": account.name if account else None,
        }
