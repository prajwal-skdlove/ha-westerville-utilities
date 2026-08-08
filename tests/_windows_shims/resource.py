"""Minimal stub of the POSIX-only `resource` module, for running Home
Assistant's test harness on Windows. See fcntl.py in this same directory for
the full rationale -- same story here: `homeassistant/util/resource.py`
imports this to raise the process's open-file-descriptor limit at daemon
startup, which pytest's `hass` fixture never triggers.
"""

from __future__ import annotations

RLIMIT_NOFILE = 7


def getrlimit(resource: int) -> tuple[int, int]:
    return (1024, 4096)


def setrlimit(resource: int, limits: tuple[int, int]) -> None:
    """No-op: nothing in the test suite depends on this actually changing."""
