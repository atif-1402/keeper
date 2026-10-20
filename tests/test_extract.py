"""Text extraction across formats, including the honest 'no text layer' flag."""

from keeper.extract import extract_text

from conftest import make_pdf


def test_plain_text(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello world, this is text")
    text, status = extract_text(p)
    assert status == "ok"
    assert "hello world" in text


def test_pdf_with_text_layer(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(make_pdf("The quick brown lease agreement"))
    text, status = extract_text(p)
    assert status == "ok"
    assert "quick brown" in text


def test_pdf_without_text_layer_is_flagged(tmp_path):
    # A PDF whose content stream carries no text operators = a scan.
    p = tmp_path / "scan.pdf"
    p.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n183\n%%EOF"
    )
    text, status = extract_text(p)
    assert status == "no_text_layer"
    assert text == ""


def test_docx(tmp_path):
    from docx import Document

    p = tmp_path / "report.docx"
    doc = Document()
    doc.add_paragraph("Quarterly numbers look good")
    doc.save(p)
    text, status = extract_text(p)
    assert status == "ok"
    assert "Quarterly numbers" in text


def test_binary_is_detected(tmp_path):
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    text, status = extract_text(p)
    assert status == "binary"
    assert text == ""


def test_media_extension_is_never_text(tmp_path):
    # JPEG bytes with no NUL: decoding them once produced garbage "text" that
    # made random photos cluster together. Media extensions are binary, period.
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0JFIF with readable metadata " * 8)
    text, status = extract_text(p)
    assert status == "binary"
    assert text == ""

    video = tmp_path / "clip.mkv"
    video.write_bytes(b"\x1a\x45\xdf\xa3 EBML header readable string")
    assert extract_text(video) == ("", "binary")
