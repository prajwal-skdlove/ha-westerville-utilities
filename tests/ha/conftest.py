"""Fixtures for tests that need a real Home Assistant core object."""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    # Windows' default ProactorEventLoop needs a real socket internally
    # (self-pipe via socket.socketpair()) even for purely in-memory async
    # code -- which pytest-socket's network-safety guard (which this plugin
    # enables) blocks, since it can't distinguish that from a test actually
    # hitting the network. SelectorEventLoop's self-pipe uses os.pipe()
    # instead, avoiding the conflict. Nothing this test suite exercises
    # (no subprocess handling) needs Proactor-only features.
    #
    # In practice this alone hasn't been enough to get this subpackage's
    # tests running on native Windows -- homeassistant.runner reasserts its
    # own event loop policy regardless. See tests/README.md: run these in
    # CI (Linux) instead.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Make custom_components/ discoverable, with recorder set up first.

    manifest.json declares recorder as a hard dependency, so every test
    here needs it regardless. Critically, recorder_mock is listed *before*
    enable_custom_integrations (which depends on `hass` directly): this
    fixture being autouse means it resolves before any explicitly-requested
    fixture in a test, `hass` included, so if recorder_mock weren't listed
    first here, `hass` would get created (via enable_custom_integrations)
    before recorder is wired up, and pytest-homeassistant-custom-component
    asserts against exactly that ordering.
    """
    yield
