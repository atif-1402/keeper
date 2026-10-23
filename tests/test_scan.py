"""Targeting filters and the byte-identical hash cap in the scanner."""

from __future__ import annotations

from keeper.scan import DEFAULT_MAX_HASH_MB, _hash_cap_ok, _keep_file, scan


def test_default_hash_cap_is_512mb():
    # The old 64 MB cap silently hid duplicated installers and videos.
    assert DEFAULT_MAX_HASH_MB == 512


def test_hash_cap_thresholds():
    assert _hash_cap_ok(100, 100)        # at the cap still counts
    assert not _hash_cap_ok(101, 100)    # above it, skipped
    assert _hash_cap_ok(10 ** 12, 0)     # 0 means no limit


def test_files_over_the_cap_are_not_hashed(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"z" * 4096)
    assert scan(tmp_path, max_hash_bytes=1024)[0].sha256 is None
    assert scan(tmp_path, max_hash_bytes=0)[0].sha256 is not None


def test_ext_filter_narrows_the_walk(tmp_path):
    (tmp_path / "a.pdf").write_text("alpha beta gamma delta epsilon")
    (tmp_path / "b.txt").write_text("alpha beta gamma delta epsilon")
    (tmp_path / "c.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 128)

    kept = {f.name for f in scan(tmp_path, exts={".pdf", ".jpg"})}
    assert kept == {"a.pdf", "c.jpg"}


def test_min_size_filter(tmp_path):
    (tmp_path / "tiny.txt").write_text("x")
    (tmp_path / "big.txt").write_text("y" * 5000)

    kept = {f.name for f in scan(tmp_path, min_size=1024)}
    assert kept == {"big.txt"}


def test_include_glob(tmp_path):
    (tmp_path / "top.pdf").write_text("alpha beta gamma delta")
    (tmp_path / "top.txt").write_text("alpha beta gamma delta")
    sub = tmp_path / "reports"
    sub.mkdir()
    (sub / "nested.pdf").write_text("alpha beta gamma delta")

    kept = {f.name for f in scan(tmp_path, include=["*.pdf"])}
    assert kept == {"top.pdf", "nested.pdf"}


def test_exclude_glob_drops_subfolder(tmp_path):
    (tmp_path / "keep.txt").write_text("alpha beta gamma delta")
    sub = tmp_path / "reports"
    sub.mkdir()
    (sub / "drop.txt").write_text("alpha beta gamma delta")

    kept = {f.rel for f in scan(tmp_path, exclude=["reports/*"])}
    assert kept == {"keep.txt"}


def test_exclude_matches_a_bare_file_name(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha beta gamma delta")
    (tmp_path / "notes.bak").write_text("alpha beta gamma delta")

    kept = {f.name for f in scan(tmp_path, exclude=["*.bak"])}
    assert kept == {"notes.txt"}


def test_include_and_exclude_can_be_combined(tmp_path):
    (tmp_path / "keep.pdf").write_text("alpha beta gamma delta")
    (tmp_path / "drop.pdf").write_text("alpha beta gamma delta")
    (tmp_path / "other.txt").write_text("alpha beta gamma delta")

    kept = {f.name for f in scan(tmp_path, include=["*.pdf"],
                                 exclude=["drop.*"])}
    assert kept == {"keep.pdf"}


def test_filters_apply_before_hashing(tmp_path):
    """A file the filters reject must never be hashed at all."""
    (tmp_path / "skip.txt").write_text("alpha beta gamma delta")
    (tmp_path / "take.txt").write_text("alpha beta gamma delta")

    facts = {f.name: f for f in scan(tmp_path, exts={".txt"},
                                     exclude=["skip.txt"])}
    assert set(facts) == {"take.txt"}
    assert facts["take.txt"].sha256


def test_keep_file_directly():
    from pathlib import Path

    p = Path("report_final.pdf")
    assert not _keep_file(p, "report_final.pdf", 10, exts={".txt"})
    assert not _keep_file(p, "report_final.pdf", 10, min_size=100)
    assert _keep_file(p, "report_final.pdf", 10 ** 6, min_size=100)
    assert _keep_file(p, "report_final.pdf", 10 ** 6, exts={".pdf"})
    assert _keep_file(p, "report_final.pdf", 10 ** 6)


def test_no_filters_means_everything_kept(tmp_path):
    (tmp_path / "a.txt").write_text("alpha beta gamma delta")
    (tmp_path / "b.bin").write_bytes(b"\x00" * 64)
    assert len(scan(tmp_path)) == 2
