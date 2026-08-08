"""The Westerville Utilities integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import WestervilleConfigEntry, WestervilleCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: WestervilleConfigEntry) -> bool:
    """Set up Westerville Utilities from a config entry."""
    coordinator = WestervilleCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WestervilleConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
