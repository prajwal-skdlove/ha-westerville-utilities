"""Exceptions raised by the Westerville client."""

from __future__ import annotations


class WestervilleError(Exception):
    """Base class for Westerville client errors."""


class InvalidAuth(WestervilleError):
    """The portal rejected the supplied username/password."""


class CannotConnect(WestervilleError):
    """The portal could not be reached, or returned an unexpected response."""
