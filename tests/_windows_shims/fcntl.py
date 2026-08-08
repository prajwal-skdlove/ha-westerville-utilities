"""Minimal stub of the POSIX-only `fcntl` module, for running Home Assistant's
test harness on Windows.

Home Assistant core (`homeassistant/runner.py`) imports `fcntl` at module
level to advisory-lock its config directory against a second instance
starting up -- a real filesystem lock that pytest's `hass` fixture never
triggers (it constructs a HomeAssistant object directly, not via
`runner.py`'s actual daemon entrypoint). `fcntl` doesn't exist on Windows at
all, so the import alone is fatal without something on `sys.path` named
`fcntl` -- this satisfies that import; nothing here is meant to actually
lock anything. Not used on Linux/macOS, where the real `fcntl` shadows this
file via the standard library import order.

See tests/README.md for how this gets onto sys.path.
"""

from __future__ import annotations

LOCK_EX = 2
LOCK_SH = 1
LOCK_UN = 8
LOCK_NB = 4


def flock(fd: int, operation: int) -> None:
    """No-op: nothing in the test suite depends on this actually locking."""


def fcntl(fd: int, cmd: int, arg: int = 0) -> int:
    return 0


def ioctl(fd: int, request: int, arg: int = 0, mutate_flag: bool = True) -> int:
    return 0


def lockf(fd: int, cmd: int, len: int = 0, start: int = 0, whence: int = 0) -> None:
    """No-op, same rationale as flock() above."""
