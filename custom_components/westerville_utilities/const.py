"""Constants for the Westerville Utilities integration."""

from __future__ import annotations

DOMAIN = "westerville_utilities"

# Options flow keys, plus the defaults matching the fixed values this
# integration originally shipped with.
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
CONF_BACKFILL_DAILY_DAYS = "backfill_daily_days"
CONF_BACKFILL_HOURLY_DAYS = "backfill_hourly_days"
# Off by default: attributes are stored in Home Assistant's history database
# and are visible to dashboards, voice assistants and anything else that can
# read states, so the service address and account holder's name stay out
# of them unless the user opts in.
CONF_EXPOSE_ACCOUNT_DETAILS = "expose_account_details"

# Westerville's own data lags a day or two behind "now"; polling more often
# than this just re-requests data that hasn't changed yet.
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_BACKFILL_DAILY_DAYS = 400
DEFAULT_BACKFILL_HOURLY_DAYS = 30
DEFAULT_EXPOSE_ACCOUNT_DETAILS = False
