"""Shared fixtures: a realistic messy folder with version-clone files."""

from __future__ import annotations

import time

import pytest
from docx import Document


def make_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF with a single text line (no dependencies)."""
    content = f"BT /F1 14 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


RENTAL_V1 = (
    "RENTAL AGREEMENT\n\nThis agreement is made between the landlord and the "
    "tenant for the property at 123 Main Street. The lease term is 12 months "
    "starting 1 January 2026. Monthly rent is 1200 dollars, due on the 1st. "
    "The security deposit is 1200 dollars. Pets are not allowed.\n"
)
RENTAL_V2 = RENTAL_V1.replace(
    "Pets are not allowed.", "One cat is allowed with written approval."
)
RENTAL_SIGNED = RENTAL_V2 + (
    "\nSIGNED: Jane Doe (tenant) 2026-02-14\nSIGNED: John Roe (landlord) 2026-02-15\n"
)


@pytest.fixture
def messy_dir(tmp_path):
    """A folder with identical copies, near-duplicates, a scan and a loner."""
    # Cluster A: three near-duplicate text versions (different mtimes, newest last).
    (tmp_path / "rental_agreement.txt").write_text(RENTAL_V1)
    (tmp_path / "rental_agreement_final.txt").write_text(RENTAL_V2)
    (tmp_path / "rental_agreement_final_v2(1).txt").write_text(RENTAL_SIGNED)

    # Cluster B: two PDFs with the same lease text (different bytes -> near-dup path).
    (tmp_path / "scan001.pdf").write_bytes(make_pdf("LEASE 123 Main Street rent 1200"))
    (tmp_path / "scan001_copy.pdf").write_bytes(make_pdf("LEASE 123 Main Street rent 1200 v2"))

    # Cluster C: a DOCX contract + a txt export of it (a *different* document).
    CONTRACT_TEXT = (
        "INDEPENDENT CONTRACTOR AGREEMENT\n\nAcme Corp engages the contractor "
        "for software services at 50 dollars per hour, invoiced monthly. "
        "Either party may terminate with 14 days written notice. The "
        "contractor owns the tools, Acme owns the deliverables.\n"
    )
    doc = Document()
    doc.add_paragraph(CONTRACT_TEXT)
    doc.save(tmp_path / "contract_signed.docx")
    (tmp_path / "contract_signed.txt").write_text(CONTRACT_TEXT)

    # Cluster D: byte-identical scanned PDFs (same bytes -> same sha256).
    twins = make_pdf("image only page")
    (tmp_path / "passport_scan.pdf").write_bytes(twins)
    (tmp_path / "passport_scan2.pdf").write_bytes(twins)

    # A loner that must NOT be grouped.
    (tmp_path / "groceries.txt").write_text("milk, eggs, bread\n")

    # Deterministic, distinct mtimes: v1 oldest ... signed newest.
    base = time.time() - 86_400
    stamps = {
        "rental_agreement.txt": base,
        "rental_agreement_final.txt": base + 3600,
        "rental_agreement_final_v2(1).txt": base + 7200,
    }
    for name, when in stamps.items():
        import os
        os.utime(tmp_path / name, (when, when))

    return tmp_path
