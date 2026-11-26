from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pypdf.errors import PdfReadError

from client import bill_pdf
from client.models import BillDetails, BillLineItem

from .synthetic_bill import (
    BILL_COMPONENTS,
    BILL_LINES,
    BILL_TEXT,
    BILL_TOTAL,
    METER_LINES,
    pdf_with_lines,
)


def _decimals(components: dict[str, float]) -> dict[str, Decimal]:
    return {key: Decimal(str(value)) for key, value in components.items()}


def test_parse_bill_text_reads_every_section_and_reconciles():
    breakdown = bill_pdf.parse_bill_text(BILL_TEXT)

    assert breakdown is not None
    assert breakdown.components == _decimals(BILL_COMPONENTS)
    assert breakdown.current_charges == Decimal("28.60")
    assert breakdown.problems == ()


def test_parse_bill_text_reads_line_items_with_usage_rate_and_credits():
    breakdown = bill_pdf.parse_bill_text(BILL_TEXT)

    assert breakdown.line_items == (
        BillLineItem(component="electric", description="ELECTRIC - PSCA", quantity=100.0, rate=0.01, amount=1.0),
        BillLineItem(component="electric", description="Electric - Customer Charge", amount=10.0),
        BillLineItem(
            component="ev_charge", description="EV OFF PEAK CHARGING CREDIT", quantity=10.0, rate=-0.04, amount=-0.4
        ),
        BillLineItem(component="water", description="Water - First 2 CCF", quantity=2.0, amount=5.0),
        BillLineItem(component="sewer", description="Sewer - First 2 CCF", quantity=2.0, amount=6.0),
        BillLineItem(component="trash", description="Refuse - Customer Charge", amount=7.0),
    )


def test_parse_bill_text_reads_meter_reads_with_the_dates_billed_for():
    breakdown = bill_pdf.parse_bill_text(BILL_TEXT)

    electric, ev, water = breakdown.meter_reads
    assert (electric.service, electric.meter_id, electric.read_type) == ("Electric", "900000001", "Actual Read")
    assert electric.previous_read_date == datetime(2026, 10, 1, tzinfo=UTC)
    assert electric.current_read_date == datetime(2026, 11, 2, tzinfo=UTC)
    assert (electric.billing_days, electric.previous_read, electric.current_read) == (32, 1000.0, 1100.0)
    assert (electric.multiplier, electric.usage, electric.unit) == (1.0, 100.0, "kWh")
    assert (ev.service, ev.usage, ev.unit) == ("Ev Charge", 10.0, "kWh")
    assert (water.previous_read_date, water.unit, water.usage) == (datetime(2026, 9, 30, tzinfo=UTC), "CCF", 2.0)
    assert breakdown.problems == ()


def test_parse_bill_text_accepts_plain_headings_and_collapses_whitespace():
    text = """
        Electric   Usage   Rate   Charges
        Electric - Customer Charge          $10.00
        $10.00
        CURRENT   CHARGES      $10.00
    """

    breakdown = bill_pdf.parse_bill_text(text)

    assert breakdown.components == {"electric": Decimal("10.00")}
    assert breakdown.problems == ()


def test_parse_bill_text_returns_none_without_sections():
    assert bill_pdf.parse_bill_text("PLEASE DETACH THIS PORTION\nCURRENT CHARGES $5.00") is None


def test_parse_bill_text_flags_section_lines_that_do_not_add_up():
    text = BILL_TEXT.replace("Electric - Customer Charge $10.00", "Electric - Customer Charge $9.00")

    breakdown = bill_pdf.parse_bill_text(text)

    # The printed subtotal wins; the disagreement is reported.
    assert breakdown.components["electric"] == Decimal("11.00")
    assert any("lines add up to 10.00 but the subtotal is 11.00" in p for p in breakdown.problems)


def test_parse_bill_text_flags_a_line_whose_usage_times_rate_is_not_its_charge():
    breakdown = bill_pdf.parse_bill_text(BILL_TEXT.replace("100.00 $0.01 $1.00", "100.00 $0.01 $2.00"))

    assert any("usage 100.00 x rate 0.01 is not 2.00" in p for p in breakdown.problems)


def test_parse_bill_text_flags_missing_subtotal_and_uses_line_sum():
    text = BILL_TEXT.replace("$7.00\nCURRENT", "CURRENT")

    breakdown = bill_pdf.parse_bill_text(text)

    assert breakdown.components["trash"] == Decimal("7.00")
    assert any("no subtotal line" in p for p in breakdown.problems)


def test_parse_bill_text_flags_unrecognised_section():
    text = BILL_TEXT.replace(
        "CURRENT CHARGES $28.60", "Late Fee Charges\nLate fee $2.00\n$2.00\nCURRENT CHARGES $30.60"
    )

    breakdown = bill_pdf.parse_bill_text(text)

    assert breakdown.components["other_late_fee"] == Decimal("2.00")
    assert any("unrecognised section 'Late Fee'" in p for p in breakdown.problems)


def test_parse_bill_text_flags_sections_that_miss_current_charges():
    breakdown = bill_pdf.parse_bill_text(BILL_TEXT.replace("CURRENT CHARGES $28.60", "CURRENT CHARGES $99.99"))

    assert any("CURRENT CHARGES is 99.99" in p for p in breakdown.problems)


def test_parse_bill_text_flags_missing_current_charges_line():
    breakdown = bill_pdf.parse_bill_text("\n".join(BILL_LINES[:-2]))

    assert "no CURRENT CHARGES line" in breakdown.problems


def test_parse_bill_text_adds_up_headings_that_map_to_the_same_component():
    text = BILL_TEXT + "\nWATER Usage ChargesRate\nWater - Extra $1.00\n$1.00"

    breakdown = bill_pdf.parse_bill_text(text)

    assert breakdown.components["water"] == Decimal("6.00")


def test_parse_bill_text_ignores_blank_lines():
    breakdown = bill_pdf.parse_bill_text("\n\n".join(BILL_LINES))

    assert breakdown.components == _decimals(BILL_COMPONENTS)
    assert len(breakdown.meter_reads) == 3


def test_parse_bill_text_flags_a_meter_whose_usage_does_not_match_its_reads():
    text = BILL_TEXT.replace("1000.00 100.0000Actual Read", "1000.00 999.0000Actual Read")

    breakdown = bill_pdf.parse_bill_text(text)

    assert any("usage 999.0000 is not (current 1100.00 - previous 1000.00) x 1" in p for p in breakdown.problems)
    assert len(breakdown.meter_reads) == 3


def test_parse_bill_text_flags_billing_days_that_do_not_match_the_read_dates():
    text = BILL_TEXT.replace("1 kWh 32 1100.00", "1 kWh 31 1100.00")

    breakdown = bill_pdf.parse_bill_text(text)

    assert any("billing days are not the 32 between its read dates" in p for p in breakdown.problems)


def test_parse_bill_text_skips_a_meter_row_with_the_wrong_number_of_readings():
    text = BILL_TEXT.replace("Water 900000003 20.00 2.0000Actual Read", "Water 900000003 2.0000Actual Read")

    breakdown = bill_pdf.parse_bill_text(text)

    assert [read.service for read in breakdown.meter_reads] == ["Electric", "Ev Charge"]
    assert any("expected 3 readings and a multiplier" in p for p in breakdown.problems)


def test_parse_bill_text_skips_a_meter_row_with_an_impossible_date():
    text = BILL_TEXT.replace("10/01/2026 11/02/2026 1 kWh 32 1100.00", "13/45/2026 11/02/2026 1 kWh 32 1100.00")

    breakdown = bill_pdf.parse_bill_text(text)

    assert any("unreadable read dates" in p for p in breakdown.problems)
    assert [read.service for read in breakdown.meter_reads] == ["Ev Charge", "Water"]


def test_parse_bill_text_reads_a_meter_row_without_a_read_type():
    text = BILL_TEXT.replace("Actual Read", " ")

    breakdown = bill_pdf.parse_bill_text(text)

    assert breakdown.meter_reads[0].read_type == "unknown"


def test_parse_bill_text_does_not_mistake_other_lines_for_meter_rows():
    text = "Customer # - Account # 100000 - 000001\nDue Date 12-15-2026\nReference 123456 paid 10/01/2026\n" + BILL_TEXT

    breakdown = bill_pdf.parse_bill_text(text)

    assert len(breakdown.meter_reads) == 3
    assert breakdown.problems == ()


def test_extract_text_reads_rows_from_a_real_pdf():
    text = bill_pdf.extract_text(pdf_with_lines(BILL_LINES))

    breakdown = bill_pdf.parse_bill_text(text)
    assert breakdown.components == _decimals(BILL_COMPONENTS)
    assert len(breakdown.meter_reads) == 3
    assert breakdown.problems == ()


def test_read_bill_returns_floats_without_unallocated_when_reconciled(caplog):
    with caplog.at_level(logging.WARNING):
        details = bill_pdf.read_bill(pdf_with_lines(BILL_LINES), BILL_TOTAL, "2026-01-01")

    assert details.components == BILL_COMPONENTS
    assert len(details.line_items) == 6
    assert len(details.meter_reads) == 3
    assert caplog.records == []


def test_read_bill_stores_the_difference_from_the_bill_amount_as_unallocated(caplog):
    with caplog.at_level(logging.WARNING):
        details = bill_pdf.read_bill(pdf_with_lines(BILL_LINES), 30.10, "2026-01-01")

    assert details.components == {**BILL_COMPONENTS, "unallocated": 1.5}
    assert "2026-01-01" in caplog.text
    assert "stored 1.50 as unallocated" in caplog.text


def test_read_bill_keeps_the_detail_and_warns_when_the_pdf_does_not_reconcile(caplog):
    lines = [line.replace("CURRENT CHARGES $28.60", "CURRENT CHARGES $99.99") for line in BILL_LINES]

    with caplog.at_level(logging.WARNING):
        details = bill_pdf.read_bill(pdf_with_lines(lines), BILL_TOTAL, "2026-01-01")

    assert details.components == BILL_COMPONENTS
    assert len(details.line_items) == 6
    assert "does not reconcile" in caplog.text


def test_read_bill_is_empty_when_the_pdf_has_no_sections(caplog):
    with caplog.at_level(logging.WARNING):
        details = bill_pdf.read_bill(pdf_with_lines(["Nothing to see"]), BILL_TOTAL, "2026-01-01")

    assert details == BillDetails()
    assert "no charge sections found" in caplog.text


def test_read_bill_is_empty_when_the_pdf_is_unreadable(monkeypatch, caplog):
    def _raise(_pdf: bytes) -> str:
        raise PdfReadError("broken")

    monkeypatch.setattr(bill_pdf, "extract_text", _raise)

    with caplog.at_level(logging.WARNING):
        assert bill_pdf.read_bill(b"%PDF-garbage", BILL_TOTAL, "2026-01-01") == BillDetails()
    assert "could not read the PDF" in caplog.text


def test_read_bill_refuses_an_oversized_pdf(monkeypatch, caplog):
    monkeypatch.setattr(bill_pdf, "MAX_PDF_BYTES", 10)

    with caplog.at_level(logging.WARNING):
        assert bill_pdf.read_bill(pdf_with_lines(BILL_LINES), BILL_TOTAL, "2026-01-01") == BillDetails()
    assert "over the 10 limit" in caplog.text


@pytest.mark.parametrize("heading", ["Electric", "EV CHARGE", "Water", "Sewer", "Refuse"])
def test_every_known_heading_maps_to_a_component(heading):
    assert heading.lower() in bill_pdf.COMPONENT_BY_HEADING


def test_meter_lines_are_not_part_of_any_section():
    assert bill_pdf.parse_bill_text("\n".join(METER_LINES)) is None
