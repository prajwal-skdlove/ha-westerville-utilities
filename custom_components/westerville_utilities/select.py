"""Select platform for Westerville Utilities: pick which bill to look at.

One per account. Its options are the bills read so far, newest first, and
its attributes are that bill's amount, dates, components, line items and
meter reads. The latest-bill sensor always shows the newest bill; this shows
any of them, so a dashboard can step back month by month
(`select.select_next` goes to the next older bill).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bill_history import bill_label
from .const import DOMAIN
from .coordinator import WestervilleConfigEntry, WestervilleCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WestervilleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one bill picker per account."""
    coordinator = entry.runtime_data
    created: set[str] = set()

    @callback
    def _update_entities() -> None:
        new_entities = [
            WestervilleBillSelect(coordinator, account_id)
            for account_id in coordinator.data.accounts
            if account_id not in created
        ]
        created.update(entity.account_id for entity in new_entities)
        if new_entities:
            async_add_entities(new_entities)

    _update_entities()
    entry.async_on_unload(coordinator.async_add_listener(_update_entities))


class WestervilleBillSelect(CoordinatorEntity[WestervilleCoordinator], SelectEntity):
    """Which of an account's bills its attributes describe."""

    _attr_has_entity_name = True
    _attr_translation_key = "bill_month"

    def __init__(self, coordinator: WestervilleCoordinator, account_id: str) -> None:
        super().__init__(coordinator)
        self.account_id = account_id
        self._selected_key: str | None = None
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{account_id}_bill_month"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name=f"Westerville account {account_id}",
            manufacturer="Westerville Utilities",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _records(self) -> dict[str, dict[str, Any]]:
        return self.coordinator.data.bill_history.get(self.account_id, {})

    def _current_key(self) -> str | None:
        """The chosen bill, or the newest when none is chosen or it is gone."""
        records = self._records()
        if self._selected_key in records:
            return self._selected_key
        return max(records, default=None)

    @property
    def available(self) -> bool:
        return super().available and bool(self._records())

    @property
    def options(self) -> list[str]:
        return [bill_label(key) for key in sorted(self._records(), reverse=True)]

    @property
    def current_option(self) -> str | None:
        key = self._current_key()
        return bill_label(key) if key else None

    async def async_select_option(self, option: str) -> None:
        # The select service has already checked `option` is one of `options`.
        self._selected_key = next(key for key in self._records() if bill_label(key) == option)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        key = self._current_key()
        if key is None:
            return {}
        record = self._records()[key]
        return {"amount": record["amount"], "bill_date": key, **record["attributes"]}
