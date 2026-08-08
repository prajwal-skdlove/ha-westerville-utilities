"""Async client for the Westerville, Ohio (billpay.westerville.org) utility
portal.

Ported from the `utility-reader` project's `providers/westerville/` adapter
(https://github.com/prajwal-skdlove/utility-reader) -- see that project for
the original sync/CLI version and its PLAN.md for how the portal's behavior
was reverse-engineered.
"""

from __future__ import annotations

from .auth import authenticate
from .exceptions import CannotConnect, InvalidAuth, WestervilleError
from .models import Account, Bill, Granularity, Meter, Reading, UtilityType

__all__ = [
    "Account",
    "Bill",
    "CannotConnect",
    "Granularity",
    "InvalidAuth",
    "Meter",
    "Reading",
    "UtilityType",
    "WestervilleError",
    "authenticate",
]
