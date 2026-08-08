"""Config flow for Westerville Utilities."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode

from .client import CannotConnect, InvalidAuth, authenticate
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

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class WestervilleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Westerville Utilities."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WestervilleOptionsFlow:
        """Get the options flow for this handler."""
        return WestervilleOptionsFlow()

    async def _validate(self, username: str, password: str) -> None:
        """Validate credentials against the real portal. Raises on failure."""
        client = create_async_httpx_client(
            self.hass, cookies=httpx.Cookies(), follow_redirects=True, timeout=30.0
        )
        await authenticate(client, username, password)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: username + password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})
            try:
                await self._validate(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Westerville credentials")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Westerville Utilities ({user_input[CONF_USERNAME]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_DATA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth triggered when the portal rejects a stored password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh password and re-validate."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await self._validate(reauth_entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Westerville credentials")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": reauth_entry.data[CONF_USERNAME]},
        )


class WestervilleOptionsFlow(OptionsFlow):
    """Options for polling interval and backfill depth.

    `self.config_entry` is provided automatically by the base class -- no
    __init__ override needed (and none should be added; explicitly storing
    it is deprecated in current Home Assistant).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL_HOURS,
                    default=current.get(CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS),
                ): NumberSelector(NumberSelectorConfig(min=1, max=168, step=1, mode=NumberSelectorMode.BOX)),
                vol.Optional(
                    CONF_BACKFILL_DAILY_DAYS,
                    default=current.get(CONF_BACKFILL_DAILY_DAYS, DEFAULT_BACKFILL_DAILY_DAYS),
                ): NumberSelector(NumberSelectorConfig(min=30, max=3650, step=1, mode=NumberSelectorMode.BOX)),
                vol.Optional(
                    CONF_BACKFILL_HOURLY_DAYS,
                    default=current.get(CONF_BACKFILL_HOURLY_DAYS, DEFAULT_BACKFILL_HOURLY_DAYS),
                ): NumberSelector(NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.BOX)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
