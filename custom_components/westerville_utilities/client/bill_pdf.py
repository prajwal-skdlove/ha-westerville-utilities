"""Detail of a Westerville bill, read from the bill's PDF.

Ported from `utility-reader`'s `providers/westerville/bill_pdf.py`. The
portal's bill table only has a total. The PDF itemises it:

* a meter table: for each meter, the dates it was actually read and billed
  for, the reads, the multiplier and the usage;
* charge sections ("Electric Usage Rate Charges", "EV CHARGE ...", "Water ...",
  "Sewer ...", "Refuse Charges"), each a list of line items (usage, rate,
  charge) ending in a subtotal on its own line, and a "CURRENT CHARGES" total.

The numbers are audited, not trusted: a section's lines must add up to its
subtotal and the subtotals to CURRENT CHARGES; a line's usage times rate must
equal its charge; a meter's days must match its dates and its usage must equal
(current read - previous read) x multiplier. Whatever fails an audit is still
returned, with a warning, so a bad parse shows up in the data and the log
instead of being dropped. Anything the sections do not account for against the
portal's bill amount is stored under `unallocated`.

pypdf's layout mode reads the charge lines cleanly but scrambles the column
order of the meter table. Its numbers always come out as previous read, usage,
... current read (identical on every one of 24 real bills, 72 meter rows), so
that order is relied on and the arithmetic above is the check.

Parsing is synchronous and CPU-bound: call `read_bill` from an executor.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import BillDetails, BillLineItem, BillMeterRead

_LOGGER = logging.getLogger(__name__)

# Section heading (lowercased) -> component key.
COMPONENT_BY_HEADING = {
    "electric": "electric",
    "ev charge": "ev_charge",
    "water": "water",
    "sewer": "sewer",
    "refuse": "trash",
}
UNALLOCATED = "unallocated"

# The PDF is untrusted input to a parser: a real bill is ~100 KB and 2 pages.
MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_PAGES = 5

_AMOUNT = r"-?\$[\d,]+\.\d{2}"
_RATE = r"\(?\$[\d,]*\.\d+\)?"
# Layout mode glues the column headers, so "Electric Usage Rate Charges" can
# come out as "Electric Usage ChargesRate"; "Refuse Charges" has no Usage/Rate.
_HEADING_RE = re.compile(r"^(?P<name>.+?)\s+(?:Usage\s+)?(?:Rate\s+)?Charges(?:\s*Rate)?$")
_STANDALONE_AMOUNT_RE = re.compile(rf"^(?P<amount>{_AMOUNT})$")
_LINE_ITEM_RE = re.compile(
    rf"^(?P<description>.+?)(?:\s+(?P<quantity>[\d,]+(?:\.\d+)?))?(?:\s+(?P<rate>{_RATE}))?\s+(?P<amount>{_AMOUNT})$"
)
_CURRENT_RE = re.compile(rf"^CURRENT CHARGES\s+(?P<amount>{_AMOUNT})$")
_PREVIOUS_RE = re.compile(rf"^PREVIOUS CHARGES\s+(?P<amount>{_AMOUNT})$")
_METER_RE = re.compile(r"^(?P<service>[A-Za-z][A-Za-z ]*?)\s+(?P<meter>\d{5,12})\s+(?P<rest>.+)$")
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_READ_TYPE_RE = re.compile(r"[A-Za-z]+\s+Read")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class BillBreakdown:
    components: dict[str, Decimal]
    line_items: tuple[BillLineItem, ...]
    meter_reads: tuple[BillMeterRead, ...]
    current_charges: Decimal | None
    problems: tuple[str, ...]


def _money(text: str) -> Decimal:
    return Decimal(text.replace("$", "").replace(",", ""))


def _rate(text: str) -> Decimal:
    """A printed rate; a rate in parentheses is a credit, so negative."""
    value = Decimal(text.strip("()").replace("$", "").replace(",", ""))
    return -value if text.startswith("(") else value


def _component_key(heading: str) -> str:
    known = COMPONENT_BY_HEADING.get(heading.lower())
    return known or "other_" + re.sub(r"\W+", "_", heading.lower()).strip("_")


def _parse_meter_read(line: str) -> tuple[BillMeterRead | None, list[str]]:
    """A meter-table row, or (None, []) when the line is not one."""
    match = _METER_RE.match(line)
    if match is None:
        return None, []
    rest = match["rest"]
    date_texts = _DATE_RE.findall(rest)
    if len(date_texts) != 2:
        return None, []

    name = f"{match['service']} meter {match['meter']}"
    try:
        previous_date, current_date = (datetime.strptime(d, "%m/%d/%Y").replace(tzinfo=UTC) for d in date_texts)
    except ValueError:
        return None, [f"{name}: unreadable read dates {date_texts}"]

    read_type_match = _READ_TYPE_RE.search(rest)
    read_type = read_type_match[0] if read_type_match else "unknown"
    stripped = _DATE_RE.sub(" ", rest)
    if read_type_match:
        stripped = stripped.replace(read_type, " ")
    words = re.findall(r"[A-Za-z]+", stripped)
    numbers = _NUMBER_RE.findall(stripped)
    integers = [n for n in numbers if "." not in n]
    decimals = [Decimal(n.replace(",", "")) for n in numbers if "." in n]

    days = (current_date - previous_date).days
    if str(days) in integers:
        integers.remove(str(days))
        problems = []
    else:
        problems = [f"{name}: billing days are not the {days} between its read dates"]
    if len(integers) != 1 or len(decimals) != 3:
        return None, [*problems, f"{name}: expected 3 readings and a multiplier, found {numbers}"]

    multiplier = Decimal(integers[0])
    previous_read, usage, current_read = decimals
    if (current_read - previous_read) * multiplier != usage:
        problems.append(
            f"{name}: usage {usage} is not (current {current_read} - previous {previous_read}) x {multiplier}"
        )
    return (
        BillMeterRead(
            service=match["service"],
            meter_id=match["meter"],
            read_type=read_type,
            previous_read_date=previous_date,
            current_read_date=current_date,
            billing_days=days,
            previous_read=float(previous_read),
            current_read=float(current_read),
            multiplier=float(multiplier),
            usage=float(usage),
            unit=words[0] if words else "",
        ),
        problems,
    )


def parse_bill_text(text: str) -> BillBreakdown | None:
    """Read the meter table and charge sections out of a bill's text, or None if there are no sections."""
    sections: dict[str, dict] = {}
    meter_reads: list[BillMeterRead] = []
    problems: list[str] = []
    current_charges: Decimal | None = None
    open_heading: str | None = None

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if match := _CURRENT_RE.match(line):
            current_charges = _money(match["amount"])
            open_heading = None
        elif _PREVIOUS_RE.match(line):
            open_heading = None
        elif (match := _HEADING_RE.match(line)) and match["name"].upper() not in ("CURRENT", "PREVIOUS"):
            open_heading = match["name"]
            sections.setdefault(open_heading, {"items": [], "subtotal": None})
        elif open_heading is not None:
            section = sections[open_heading]
            if match := _STANDALONE_AMOUNT_RE.match(line):
                if section["subtotal"] is None:
                    section["subtotal"] = _money(match["amount"])
                open_heading = None
            elif match := _LINE_ITEM_RE.match(line):
                section["items"].append(match)
        elif not sections:
            read, read_problems = _parse_meter_read(line)
            problems.extend(read_problems)
            if read is not None:
                meter_reads.append(read)

    if not sections:
        return None

    components: dict[str, Decimal] = {}
    line_items: list[BillLineItem] = []
    for heading, section in sections.items():
        key = _component_key(heading)
        if key.startswith("other_"):
            problems.append(f"unrecognised section {heading!r}")
        items_total = Decimal(0)
        for match in section["items"]:
            amount = _money(match["amount"])
            quantity = Decimal(match["quantity"].replace(",", "")) if match["quantity"] else None
            rate = _rate(match["rate"]) if match["rate"] else None
            if quantity is not None and rate is not None and abs(quantity * rate - amount) > Decimal("0.011"):
                problems.append(f"{match['description']}: usage {quantity} x rate {rate} is not {amount}")
            items_total += amount
            line_items.append(
                BillLineItem(
                    component=key,
                    description=match["description"],
                    amount=float(amount),
                    quantity=float(quantity) if quantity is not None else None,
                    rate=float(rate) if rate is not None else None,
                )
            )
        if section["subtotal"] is None:
            problems.append(f"{heading}: no subtotal line; used the sum of its lines")
            value = items_total
        else:
            value = section["subtotal"]
            if items_total != value:
                problems.append(f"{heading}: lines add up to {items_total} but the subtotal is {value}")
        components[key] = components.get(key, Decimal(0)) + value

    if current_charges is None:
        problems.append("no CURRENT CHARGES line")
    elif sum(components.values(), Decimal(0)) != current_charges:
        problems.append(
            f"sections add up to {sum(components.values(), Decimal(0))} but CURRENT CHARGES is {current_charges}"
        )
    return BillBreakdown(
        components=components,
        line_items=tuple(line_items),
        meter_reads=tuple(meter_reads),
        current_charges=current_charges,
        problems=tuple(problems),
    )


def extract_text(pdf: bytes) -> str:
    """The PDF's text with each table row on one line (pypdf's layout mode)."""
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages[:MAX_PAGES])


def read_bill(pdf: bytes, amount_total: float, label: str) -> BillDetails:
    """Everything the PDF has for one bill, or an empty BillDetails when it has none to read.

    `amount_total` is the portal's bill amount. Any difference from the sum
    of the components is added as `unallocated`, so a bill that does not
    reconcile can be found in the data. `label` names the bill in logs.
    """
    if len(pdf) > MAX_PDF_BYTES:
        _LOGGER.warning(
            "Bill %s: the PDF is %d bytes, over the %d limit; detail left empty", label, len(pdf), MAX_PDF_BYTES
        )
        return BillDetails()
    try:
        breakdown = parse_bill_text(extract_text(pdf))
    except PyPdfError as err:
        _LOGGER.warning("Bill %s: could not read the PDF (%s); detail left empty", label, err)
        return BillDetails()
    if breakdown is None:
        _LOGGER.warning("Bill %s: no charge sections found in the PDF; detail left empty", label)
        return BillDetails()

    for problem in breakdown.problems:
        _LOGGER.warning("Bill %s does not reconcile: %s", label, problem)

    components = {key: float(value) for key, value in breakdown.components.items()}
    unallocated = Decimal(str(amount_total)) - sum(breakdown.components.values(), Decimal(0))
    if unallocated != 0:
        _LOGGER.warning(
            "Bill %s: components add up to %s but the bill amount is %s; stored %s as unallocated",
            label, sum(breakdown.components.values(), Decimal(0)), amount_total, unallocated,
        )
        components[UNALLOCATED] = float(unallocated)
    return BillDetails(
        components=components,
        line_items=breakdown.line_items,
        meter_reads=breakdown.meter_reads,
    )
