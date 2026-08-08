"""Confirms the test harness can discover and import the integration at all."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.westerville_utilities.const import DOMAIN


async def test_domain_constant() -> None:
    assert DOMAIN == "westerville_utilities"


async def test_component_is_discoverable(recorder_mock, hass: HomeAssistant) -> None:
    """If this fails, custom_components/ isn't wired up for the test harness.

    recorder_mock must be listed before hass: manifest.json declares
    recorder as a hard dependency, and this plugin's recorder setup must
    resolve before the hass fixture is instantiated, not after.
    """
    # config_flow-only components don't have async_setup_component do much,
    # but this exercises manifest.json parsing and module import via HA's
    # own component loader -- exactly what breaks if the package layout is wrong.
    result = await async_setup_component(hass, DOMAIN, {})
    assert result is True
