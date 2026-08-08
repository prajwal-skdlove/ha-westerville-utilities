"""Constants for the Westerville Utilities integration."""

from __future__ import annotations

DOMAIN = "westerville_utilities"

# Options flow keys, plus the defaults matching the fixed values this
# integration originally shipped with.
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
CONF_BACKFILL_DAILY_DAYS = "backfill_daily_days"
CONF_BACKFILL_HOURLY_DAYS = "backfill_hourly_days"

# Westerville's own data lags a day or two behind "now"; polling more often
# than this just re-requests data that hasn't changed yet.
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_BACKFILL_DAILY_DAYS = 400
DEFAULT_BACKFILL_HOURLY_DAYS = 30
