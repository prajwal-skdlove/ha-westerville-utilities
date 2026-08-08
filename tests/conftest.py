"""Root conftest.

Deliberately empty: Home Assistant-specific setup lives in
tests/ha/conftest.py, scoped to only the tests that need a real `hass`
fixture. tests/client/ tests are plain async httpx/respx tests with no HA
dependency -- see tests/README.md.
"""

from __future__ import annotations
