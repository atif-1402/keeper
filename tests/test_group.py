"""Clustering: identical copies and near-duplicates end up in the same groups."""

from keeper.extract import extract_text
from keeper.group import cluster, norm_stem, tokens_of
from keeper.scan import scan


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
    assert norm_stem("rental_agreement_final_v2(1).pdf") == "rental agreement 1"
    assert norm_stem("rental_agreement.pdf") == "rental agreement"


def test_tokens_of_filename():
    assert tokens_of("Contract FINAL.docx") == {"contract", "final"}


def test_unrelated_files_never_cluster(tmp_path):
    (tmp_path / "a_report.txt").write_text("alpha beta gamma delta epsilon zeta")
    (tmp_path / "b_novel.txt").write_text("once upon a time in a distant kingdom")
    assert cluster(_facts(tmp_path)) == []
