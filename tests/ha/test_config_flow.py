"""Tests for the config flow and options flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.westerville_utilities.client import CannotConnect, InvalidAuth
from custom_components.westerville_utilities.const import DOMAIN

PATCH_TARGET = "custom_components.westerville_utilities.config_flow.authenticate"


async def _start_user_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_step_creates_entry_on_success(hass: HomeAssistant, monkeypatch) -> None:
    async def _ok(*args, **kwargs):
        return None

    monkeypatch.setattr(PATCH_TARGET, _ok)

    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "user@example.com", "password": "hunter2"}
    )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Westerville Utilities (user@example.com)"
    assert result2["data"] == {"username": "user@example.com", "password": "hunter2"}


async def test_user_step_shows_invalid_auth_error(hass: HomeAssistant, monkeypatch) -> None:
    async def _bad_auth(*args, **kwargs):
        raise InvalidAuth("nope")

    monkeypatch.setattr(PATCH_TARGET, _bad_auth)

    result = await _start_user_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "user@example.com", "password": "wrong"}
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_user_step_shows_cannot_connect_error(hass: HomeAssistant, monkeypatch) -> None:
    async def _unreachable(*args, **kwargs):
        raise CannotConnect("timeout")

    monkeypatch.setattr(PATCH_TARGET, _unreachable)

    result = await _start_user_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "user@example.com", "password": "hunter2"}
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_user_step_shows_unknown_error_on_unexpected_exception(hass: HomeAssistant, monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        raise ValueError("something unrelated broke")

    monkeypatch.setattr(PATCH_TARGET, _boom)

    result = await _start_user_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "user@example.com", "password": "hunter2"}
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}


async def test_already_configured_aborts(hass: HomeAssistant, monkeypatch) -> None:
    async def _ok(*args, **kwargs):
        return None

    monkeypatch.setattr(PATCH_TARGET, _ok)

    existing = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"}
    )
    existing.add_to_hass(hass)

    result = await _start_user_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "user@example.com", "password": "hunter2"}
    )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_reauth_flow_updates_password(hass: HomeAssistant, monkeypatch) -> None:
    async def _ok(*args, **kwargs):
        return None

    monkeypatch.setattr(PATCH_TARGET, _ok)

    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "old-password"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new-password"}
    )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    assert entry.data["username"] == "user@example.com"  # untouched


async def test_reauth_flow_shows_invalid_auth_error(hass: HomeAssistant, monkeypatch) -> None:
    async def _bad_auth(*args, **kwargs):
        raise InvalidAuth("still wrong")

    monkeypatch.setattr(PATCH_TARGET, _bad_auth)

    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "old-password"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "still-wrong"}
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old-password"  # not updated on failure


async def test_options_flow_saves_values(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "hunter2"}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "update_interval_hours": 6,
            "backfill_daily_days": 100,
            "backfill_hourly_days": 10,
        },
    )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "update_interval_hours": 6.0,
        "backfill_daily_days": 100.0,
        "backfill_hourly_days": 10.0,
    }
