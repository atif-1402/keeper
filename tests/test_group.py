"""Clustering: identical copies and near-duplicates end up in the same groups."""

from keeper.extract import extract_text
from keeper.group import cluster, norm_stem, tokens_of
from keeper.scan import scan
from conftest import make_pdf


def _facts(root):
    facts = scan(root)
    for f in facts:
        f.text, f.status = extract_text(f.path)
    return facts


def test_messy_folder_finds_expected_clusters(messy_dir):
    clusters = cluster(_facts(messy_dir))
    sizes = sorted(len(c) for c in clusters)
    # rental (3), pdf pair (2), contract pair (2), passport pair (2)
    assert sizes == [2, 2, 2, 3]


def test_loners_stay_out(messy_dir):
    clusters = cluster(_facts(messy_dir))
    all_names = {f.name for c in clusters for f in c}
    assert "groceries.txt" not in all_names


def test_rental_versions_cluster_together(messy_dir):
    clusters = cluster(_facts(messy_dir))
    rental = next(c for c in clusters if any("rental" in f.name for f in c))
    names = {f.name for f in rental}
    assert names == {
        "rental_agreement.txt",
        "rental_agreement_final.txt",
        "rental_agreement_final_v2(1).txt",
    }


def test_identical_files_cluster_by_hash(messy_dir):
    clusters = cluster(_facts(messy_dir))
    passports = next(c for c in clusters if any("passport" in f.name for f in c))
    assert len({f.sha256 for f in passports}) == 1  # byte-identical twins


def test_norm_stem_strips_file_management_words():
    # The `(1)` download-collision suffix is stripped together with the noise
    # words: this file is just another copy of v2.
    assert norm_stem("rental_agreement_final_v2(1).pdf") == "rental agreement"
    assert norm_stem("rental_agreement.pdf") == "rental agreement"
    assert norm_stem("anom-os-site (14).zip") == "anom os site"


def test_tokens_of_filename():
    assert tokens_of("Contract FINAL.docx") == {"contract", "final"}
    # The collision suffix is not itself a token.
    assert tokens_of("report (2).html") == {"report"}


def test_tv_episodes_never_cluster(tmp_path):
    # Same release group, same season — but every episode is a different file.
    base = "The Mentalist (2008) - S07E{} - x (1080p AMZN WEB-DL x265 RZeroX).mkv"
    for ep in ("02", "04", "05"):
        (tmp_path / base.format(ep)).write_bytes(b"\x00\x01video" + ep.encode())
    assert cluster(_facts(tmp_path)) == []


def test_different_lesson_numbers_never_cluster(tmp_path):
    for n in (13, 14):
        (tmp_path / f"311_Maths_Eng_Lesson{n}.pdf").write_bytes(
            make_pdf(f"Lesson {n} exercise text here")
        )
    assert cluster(_facts(tmp_path)) == []


def test_browser_copies_of_same_name_cluster(tmp_path):
    data = "the same payload for both copies of this page"
    (tmp_path / "site-index.html").write_text(data)
    (tmp_path / "site-index (2).html").write_text(data)
    assert len(cluster(_facts(tmp_path))) == 1


def test_page_components_never_cluster(tmp_path):
    # A saved webpage's stylesheet shares nearly all its tokens with the page
    # that inlined it (cosine ~0.9) — but a CSS file is not a version of an
    # HTML file. Content matches must stay within one format, and these two
    # don't even share a name.
    css = ".site-header { color: #333; background: white; padding: 12px; }"
    (tmp_path / "site-theme.css").write_text(css)
    (tmp_path / "index.html").write_text(
        f"<html><head><style>{css}</style></head><body>hi</body></html>"
    )
    assert cluster(_facts(tmp_path)) == []


def test_cross_format_twins_cluster_by_name(tmp_path):
    # A .docx export and its .txt twin share the stem -> still one cluster.
    from docx import Document

    text = "INDEPENDENT CONTRACTOR AGREEMENT with hourly rates and notice terms"
    doc = Document()
    doc.add_paragraph(text)
    doc.save(tmp_path / "contract_signed.docx")
    (tmp_path / "contract_signed.txt").write_text(text)
    assert len(cluster(_facts(tmp_path))) == 1


def test_unrelated_files_never_cluster(tmp_path):
    (tmp_path / "a_report.txt").write_text("alpha beta gamma delta epsilon zeta")
    (tmp_path / "b_novel.txt").write_text("once upon a time in a distant kingdom")
    assert cluster(_facts(tmp_path)) == []
