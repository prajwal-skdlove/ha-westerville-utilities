"""Shared constants/helpers for the Westerville (AUS Capricorn) client.

Ported from the `utility-reader` project's `providers/westerville/_shared.py`
(async'd for Home Assistant) -- see that project's PLAN.md for how these
facts about the portal were originally discovered. Login is a plain
CSRF-token form POST (no JS/AJAX auth); the dashboard's tabs are
server-rendered HTML fragments fetched via `/app/capricorn?para=...`. Chart
data arrives embedded in a `<script>` block as JS array literals, which we
regex out rather than parse as JS.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://billpay.westerville.org"

# Politeness delay between requests to the portal, and the max date span
# smartMeterConsumV3 accepts per request before it rejects with
# "Cannot Process Request" (confirmed: 60 days OK, 90 days rejected -- 55
# gives comfortable headroom without an extra round trip per chunk).
REQUEST_DELAY_SECONDS = 0.3
MAX_DAILY_SPAN_DAYS = 55

# hydro=electric, water=water. Sewer typically has no advanced-meter data of
# its own (it's derived from water usage), so it isn't in this AMI map, but
# is still handled for monthly billed usage/bills.
INQUIRY_TYPE_TO_UTILITY = {
    "hydro": "electric",
    "water": "water",
    "sewer": "sewer",
}
UTILITY_TO_INQUIRY_TYPE = {v: k for k, v in INQUIRY_TYPE_TO_UTILITY.items()}

CONSUMPTION_REPORT_BY_INQUIRY_TYPE = {
    "hydro": "ELECONRP",
    "water": "WATCONRP",
}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(httpx.HTTPError),
)
async def get(client: httpx.AsyncClient, path_and_query: str) -> httpx.Response:
    """GET a Westerville portal path with politeness delay + retry."""
    url = f"{BASE_URL}{path_and_query}"
    _LOGGER.debug("GET %s", url)
    await asyncio.sleep(REQUEST_DELAY_SECONDS)
    resp = await client.get(url)
    resp.raise_for_status()
    _LOGGER.debug("GET %s -> %s (%d bytes)", url, resp.status_code, len(resp.content))
    return resp


def is_error_page(html: str) -> bool:
    return "errorInvalidInput" in html or "Cannot Process Request" in html


_LABEL_ARRAY_RE = re.compile(r"xAxisLabelArray\s*=\s*\[(?P<body>[^\]]*)\]")
_USAGE_SERIES_RE = re.compile(
    r'name:\s*"Usage".{0,300}?data:\s*\[(?P<body>[^\]]*)\]', re.DOTALL
)
_SUBTITLE_RE = re.compile(r'subtitle:\s*\{\s*text:\s*"(?P<body>[^"]*)"')
_TOTAL_CONSUMPTION_UNIT_RE = re.compile(r"Total Consumption of [\d,.]+\s*(?P<unit>[A-Za-z]+)")
_SUBTITLE_METER_ID_RE = re.compile(r"Meter ID:\s*(?P<meter_id>\w+)")


def subtitle_meter_id(html: str) -> str | None:
    """The meter ID a smartMeterConsumV3 fragment's subtitle actually reports.

    A `selectedMeterId` for a meter with no AMI registration isn't rejected
    by the portal -- it silently falls back to whatever meter was last
    active in the session and returns *that* meter's data (confirmed live).
    Callers MUST compare this against the meter_id they actually requested
    before trusting a response, or risk attributing one meter's readings to
    a different meter.
    """
    match = _SUBTITLE_METER_ID_RE.search(html)
    return match.group("meter_id") if match else None


def parse_ami_fragment(html: str) -> tuple[list[str], list[float | None], str] | None:
    """Parse a smartMeterConsumV3 HTML fragment.

    Returns `(labels, values, unit)` in the order the portal reports them
    (chronological ascending for daily; hour-of-day ascending for hourly),
    or `None` if the fragment shows no data for the requested period (a
    normal, expected response for dates outside the meter's actual
    AMI-enabled history).
    """
    subtitle_match = _SUBTITLE_RE.search(html)
    if not subtitle_match or "Total Consumption" not in subtitle_match.group("body"):
        return None

    # xAxisLabelArray is sometimes declared empty (`= []`) and reassigned in
    # a later statement (seen on hourly fragments) -- take the longest match,
    # not the first, so we don't pick up the empty declaration.
    label_matches = list(_LABEL_ARRAY_RE.finditer(html))
    series_match = _USAGE_SERIES_RE.search(html)
    if not label_matches or not series_match:
        _LOGGER.warning("AMI fragment had a subtitle but no parseable chart data; treating as no data")
        return None
    label_match = max(label_matches, key=lambda m: len(m.group("body")))

    labels = [s.strip().strip("'\"") for s in label_match.group("body").split(",") if s.strip()]
    values: list[float | None] = []
    for raw in series_match.group("body").split(","):
        raw = raw.strip()
        if not raw or raw == "null":
            values.append(None)
        else:
            values.append(float(raw))

    unit_match = _TOTAL_CONSUMPTION_UNIT_RE.search(subtitle_match.group("body"))
    unit = unit_match.group("unit") if unit_match else "unknown"

    if len(labels) != len(values):
        _LOGGER.warning(
            "AMI fragment label/value count mismatch (%d labels vs %d values); truncating to shorter",
            len(labels),
            len(values),
        )
        n = min(len(labels), len(values))
        labels, values = labels[:n], values[:n]

    return labels, values, unit
