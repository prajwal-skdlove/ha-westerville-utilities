"""A synthetic Westerville bill: invented amounts, no real customer data.

The lines mimic the real bill's text as pypdf's layout mode returns it,
including its glued column headers ("Usage ChargesRate").
"""

from __future__ import annotations

# Meter table rows in the column order pypdf's layout mode really produces:
# previous read, usage glued to the read type, the two read dates, multiplier,
# unit, billing days, current read.
METER_LINES = [
    "Electric 900000001 1000.00 100.0000Actual Read10/01/2026 11/02/2026 1 kWh 32 1100.00",
    "Ev Charge 900000002 50.0000 10.0000Actual Read10/01/2026 11/02/2026 1 kWh 32 60.0000",
    "Water 900000003 20.00 2.0000Actual Read09/30/2026 11/01/2026 1 CCF 32 22.00",
]

# Sections: electric 11.00, EV -0.40, water 5.00, sewer 6.00, trash 7.00 = 28.60.
BILL_LINES = [
    *METER_LINES,
    "Electric Usage ChargesRate",
    "ELECTRIC - PSCA 100.00 $0.01 $1.00",
    "Electric - Customer Charge $10.00",
    "$11.00",
    "EV CHARGE Usage ChargesRate",
    "EV OFF PEAK CHARGING CREDIT 10.0000 ($0.04) -$0.40",
    "-$0.40",
    "Water Usage ChargesRate",
    "Water - First 2 CCF 2.00 $5.00",
    "$5.00",
    "Sewer Usage ChargesRate",
    "Sewer - First 2 CCF 2.00 $6.00",
    "$6.00",
    "Refuse Charges",
    "Refuse - Customer Charge $7.00",
    "$7.00",
    "CURRENT CHARGES $28.60",
    "PREVIOUS CHARGES $0.00",
]
BILL_TEXT = "\n".join(BILL_LINES)
BILL_TOTAL = 28.60
BILL_COMPONENTS = {"electric": 11.00, "ev_charge": -0.40, "water": 5.00, "sewer": 6.00, "trash": 7.00}


def pdf_with_lines(lines: list[str]) -> bytes:
    """A one-page PDF with each line of text on its own row."""

    def escaped(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 10 Tf 12 TL 40 760 Td " + " ".join(f"({escaped(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF".encode()
    return out
