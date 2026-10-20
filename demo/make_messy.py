#!/usr/bin/env python3
"""Build demo/messy-demo/ — a realistic version-clone mess for screenshots/GIFs.

    uv run python demo/make_messy.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import RENTAL_SIGNED, RENTAL_V1, RENTAL_V2, make_pdf  # noqa: E402

DEMO_ROOT = Path(__file__).resolve().parent / "messy-demo"

CONTRACT_TEXT = (
    "INDEPENDENT CONTRACTOR AGREEMENT\n\n"
    "Acme Corp engages the contractor for software services at 50 dollars per "
    "hour, invoiced monthly. Either party may terminate with 14 days written "
    "notice. The contractor owns the tools, Acme owns the deliverables.\n"
)

INSURANCE_CLAIM = (
    "INSURANCE CLAIM FORM\n\nPolicy 88214, claimant Alex Rivera. Incident on "
    "2026-03-02: water damage to kitchen ceiling. Adjuster visit scheduled. "
    "Estimated repair cost 2400 dollars. Photographs attached separately.\n"
)


def make_blank_scan() -> bytes:
    """An image-only PDF: real structure, no text layer -> Keeper flags it."""
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n183\n%%EOF"
    )


def build() -> None:
    if DEMO_ROOT.exists():
        import shutil
        shutil.rmtree(DEMO_ROOT)
    DEMO_ROOT.mkdir(parents=True)

    # Cluster 1: rental agreement — three evolving versions, signed one newest.
    (DEMO_ROOT / "rental_agreement.txt").write_text(RENTAL_V1)
    (DEMO_ROOT / "rental_agreement_final.txt").write_text(RENTAL_V2)
    (DEMO_ROOT / "rental_agreement_final_v2(1).txt").write_text(RENTAL_SIGNED)

    # Cluster 2: scanned-to-PDF pair with text layer (name twins).
    (DEMO_ROOT / "scan001.pdf").write_bytes(
        make_pdf("LEASE 123 Main Street rent 1200 deposit 1200")
    )
    (DEMO_ROOT / "scan001_copy.pdf").write_bytes(
        make_pdf("LEASE 123 Main Street rent 1200 deposit 1200 revised")
    )

    # Cluster 3: a DOCX contract and its plain-text export.
    from docx import Document

    doc = Document()
    doc.add_paragraph(CONTRACT_TEXT)
    doc.save(DEMO_ROOT / "contract_signed.docx")
    (DEMO_ROOT / "contract_signed.txt").write_text(CONTRACT_TEXT)

    # Cluster 4: byte-identical scanned PDFs (no text layer -> honest flag).
    (DEMO_ROOT / "passport_scan.pdf").write_bytes(make_blank_scan())
    (DEMO_ROOT / "passport_scan2.pdf").write_bytes(make_blank_scan())

    # Cluster 5: two insurance claim versions (content differs, same doc).
    (DEMO_ROOT / "insurance_claim.txt").write_text(INSURANCE_CLAIM)
    (DEMO_ROOT / "insurance_claim_v2.txt").write_text(
        INSURANCE_CLAIM.replace("2400 dollars", "3150 dollars after inspection")
    )

    # Loners that must NOT be grouped.
    (DEMO_ROOT / "groceries.txt").write_text("milk, eggs, bread\n")
    (DEMO_ROOT / "reading_list.md").write_text("- Pachinko\n- The Dispossessed\n")

    # Deterministic mtimes: evolving versions get progressively newer stamps.
    base = time.time() - 30 * 86_400
    order = [
        "rental_agreement.txt",
        "rental_agreement_final.txt",
        "rental_agreement_final_v2(1).txt",
        "scan001.pdf",
        "scan001_copy.pdf",
        "contract_signed.txt",
        "contract_signed.docx",
        "insurance_claim.txt",
        "insurance_claim_v2.txt",
    ]
    for i, name in enumerate(order):
        when = base + i * 6 * 3600
        os.utime(DEMO_ROOT / name, (when, when))

    count = sum(1 for p in DEMO_ROOT.iterdir() if p.is_file())
    print(f"Built {count} files in {DEMO_ROOT}")


if __name__ == "__main__":
    build()
