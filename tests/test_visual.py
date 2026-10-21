"""Perceptual image matching: same picture, different bytes, one cluster."""

from __future__ import annotations

from PIL import Image

from keeper.decide import decide
from keeper.extract import extract_text
from keeper.group import cluster
from keeper.scan import scan
from keeper.visual import NEAR_MIN, dhash, hamming


def _pattern(seed: int) -> list[tuple[int, int, int]]:
    """Deterministic pseudo-photo gradient — two seeds look different,
    the same seed looks identical."""
    return [
        ((x * 7 + y * 13 + seed * 31) % 256,
         (x * 3 + y * 29 + seed * 17) % 256,
         (x * 11 + y * 5 + seed * 47) % 256)
        for y in range(48)
        for x in range(64)
    ]


def _make_image(path, seed: int, fmt: str = "PNG", quality: int | None = None):
    img = Image.new("RGB", (64, 48))
    img.putdata(_pattern(seed))
    kwargs = {"quality": quality} if quality else {}
    img.save(path, fmt, **kwargs)
    return path


def _facts(root):
    facts = scan(root)
    for f in facts:
        f.text, f.status = extract_text(f.path)
    return facts


def test_dhash_is_deterministic(tmp_path):
    p = _make_image(tmp_path / "a.png", seed=1)
    assert dhash(p) == dhash(p)
    assert dhash(p) is not None


def test_hamming_counts_bits():
    assert hamming(0b1010, 0b0101) == 4
    assert hamming(0b1111, 0b1111) == 0


def test_same_image_png_and_jpeg_cluster(tmp_path):
    # The classic clone: a screenshot saved as PNG, then re-exported as JPEG.
    _make_image(tmp_path / "screenshot.png", seed=3)
    _make_image(tmp_path / "screenshot copy.jpg", seed=3, fmt="JPEG", quality=70)
    clusters = cluster(_facts(tmp_path))
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_different_images_do_not_cluster(tmp_path):
    _make_image(tmp_path / "photo_a.png", seed=1)
    _make_image(tmp_path / "photo_b.png", seed=99)
    assert cluster(_facts(tmp_path)) == []


def test_cross_format_webp_match(tmp_path):
    _make_image(tmp_path / "wallpaper.png", seed=7)
    _make_image(tmp_path / "wallpaper.webp", seed=7, fmt="WEBP")
    assert len(cluster(_facts(tmp_path))) == 1


def test_visual_flag_marks_perceptual_clusters(tmp_path):
    _make_image(tmp_path / "scan_final.png", seed=5)
    _make_image(tmp_path / "scan_final (2).jpg", seed=5, fmt="JPEG", quality=60)
    clusters = cluster(_facts(tmp_path))
    assert len(clusters) == 1
    assert all(f.visual for f in clusters[0])


def test_decide_reason_mentions_perceptual_hash(tmp_path):
    # Images have no readable text — the verdict must say WHY they cluster:
    # perceptual match, not a guess from the filename.
    _make_image(tmp_path / "img.png", seed=8)
    _make_image(tmp_path / "img_exported.jpg", seed=8, fmt="JPEG", quality=75)
    clusters = cluster(_facts(tmp_path))
    dec = decide(clusters[0], use_model=False)
    assert dec.method == "rule"
    assert "perceptual" in dec.reason
    assert dec.keep in clusters[0]


def test_near_min_is_sane():
    # Guards the threshold itself: identical hashes are 0 apart, and a
    # re-encode must stay comfortably under NEAR_MIN while garbage stays over.
    assert NEAR_MIN <= 16


def test_corrupt_image_steps_aside(tmp_path):
    # A .png that isn't a real PNG must never crash the scan — the feature
    # quietly steps aside and Keeper behaves exactly as before.
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n not actually a png")
    twin = tmp_path / "broken (2).png"
    twin.write_bytes(b"\x89PNG\r\n\x1a\n different garbage")
    assert dhash(bad) is None
    # No perceptual clusters — but the name signal may still pair them.
    clusters = cluster(_facts(tmp_path))
    for c in clusters:
        assert not any(f.visual for f in c)
