"""Tests for client/shared.py: AMI fragment parsing + the retrying GET."""

from __future__ import annotations

import httpx
import pytest
import respx

from client import shared

DAILY_FRAGMENT = """
<script>
var xAxisLabelArray = ['2024-01-01', '2024-01-02', '2024-01-03'];
Highcharts.stockChart('container', {
    subtitle: {
        text: "Total Consumption of 150.00 KWH <br/> For the period of January 1, 2024 - January 3, 2024 <br>Meter ID: 123456",
    },
    series: [{
        id: "consumptionDataDefault_FLATPeak",
        type: 'column',
        name: "Usage",
        data: [50.0, null, 100.0],
    }],
});
</script>
"""

HOURLY_FRAGMENT_WITH_REASSIGNED_LABELS = """
<script>
var xLabels = ["12:00 am","1:00 am"];
var xAxisLabelArray = [];
xAxisLabelArray = ['1:00 am', '2:00 am'];
Highcharts.stockChart('container', {
    subtitle: {
        text: "Total Consumption of 5.00 KWH<br/>For January 1, 2024 <br>Meter ID: 123456",
    },
    series: [{
        name: "Usage",
        data: [2.5, 2.5],
    }],
});
</script>
"""

NO_DATA_FRAGMENT = """
<script>
var xAxisLabelArray = [];
Highcharts.stockChart('container', {
    subtitle: {
        text: "<br>Meter ID: 123456",
    },
    series: [{
        name: "Usage",
        data: [null],
    }],
});
</script>
"""

ERROR_PAGE = """
<html><head><title>Cannot Process Request</title></head>
<!-- errorInvalidInput.jsp -->
<body></body></html>
"""


def test_parse_ami_fragment_daily_data():
    result = shared.parse_ami_fragment(DAILY_FRAGMENT)
    assert result is not None
    labels, values, unit = result
    assert labels == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert values == [50.0, None, 100.0]
    assert unit == "KWH"


def test_parse_ami_fragment_picks_reassigned_label_array_not_empty_declaration():
    result = shared.parse_ami_fragment(HOURLY_FRAGMENT_WITH_REASSIGNED_LABELS)
    assert result is not None
    labels, values, _unit = result
    assert labels == ["1:00 am", "2:00 am"]
    assert values == [2.5, 2.5]


def test_parse_ami_fragment_no_data_returns_none():
    assert shared.parse_ami_fragment(NO_DATA_FRAGMENT) is None


def test_parse_ami_fragment_missing_subtitle_returns_none():
    assert shared.parse_ami_fragment("<html>nothing here</html>") is None


def test_parse_ami_fragment_subtitle_present_but_no_chart_data_returns_none():
    html = '<script>subtitle: { text: "Total Consumption of 1.0 KWH For Jan" },</script>'
    assert shared.parse_ami_fragment(html) is None


def test_parse_ami_fragment_mismatched_lengths_truncates():
    html = """
    <script>
    var xAxisLabelArray = ['2024-01-01', '2024-01-02'];
    Highcharts.stockChart('c', {
        subtitle: { text: "Total Consumption of 1.00 KWH For Jan" },
        series: [{ name: "Usage", data: [1.0, 2.0, 3.0] }],
    });
    </script>
    """
    result = shared.parse_ami_fragment(html)
    assert result is not None
    labels, values, _unit = result
    assert len(labels) == len(values) == 2


def test_subtitle_meter_id_extracts_id():
    assert shared.subtitle_meter_id(DAILY_FRAGMENT) == "123456"


def test_subtitle_meter_id_missing_returns_none():
    assert shared.subtitle_meter_id("<html>no meter id here</html>") is None


def test_is_error_page():
    assert shared.is_error_page(ERROR_PAGE) is True
    assert shared.is_error_page(DAILY_FRAGMENT) is False


@respx.mock
async def test_get_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    route = respx.get(f"{shared.BASE_URL}/app/flaky").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, text="ok")]
    )
    async with httpx.AsyncClient() as client:
        resp = await shared.get(client, "/app/flaky")

    assert resp.text == "ok"
    assert route.call_count == 2


@respx.mock
async def test_get_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/always-down").mock(return_value=httpx.Response(500))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await shared.get(client, "/app/always-down")
