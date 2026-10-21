"""Perceptual image hashing — cluster visually near-identical images.

Two screenshots of the same page, or a PNG re-exported as JPEG, are
different bytes with the same picture: sha256 misses them and there is no
text to compare either. A 64-bit difference hash (dHash) doesn't miss
them — it fingerprints what the image *looks like*, not what it's made of.

This is the cheap-middle step of the pipeline: byte hash first (free),
perceptual hash second (milliseconds), model never (images have no text).
Pillow is the only dependency; if it's missing or a file can't be opened,
this feature quietly steps aside and Keeper behaves exactly as before.
"""

from __future__ import annotations

from pathlib import Path

from .scan import FileFact

# The raster family where cross-format re-exports actually happen:
# photo.png saved as photo.jpg, screenshot.webp exported from screenshot.png.
# (GIF/BMP/TIFF/ICO stay out: first-frame and palette quirks make their
# hashes noisy, and re-export clones of those are rare.)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Skip huge images the same way the sha256 pass does.
MAX_IMAGE_BYTES = 64 * 1024 * 1024

# Hamming distance (of 64 bits) at or below which two images count as
# visually near-identical. Re-encoding shifts roughly 0-5 bits; unrelated
# images land around 20+. 10 leaves headroom both ways.
NEAR_MIN = 10


def dhash(path: Path, hash_size: int = 8) -> int | None:
    """64-bit difference hash of an image, or None if we must step aside.

    Grayscale, downscale to (hash_size+1) x hash_size, then one bit per
    horizontally adjacent pixel pair: 1 when the left pixel is brighter.
    Robust to compression, resizing, and format re-exports; blind to what
    actually matters for duplicate detection.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            gray = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.Resampling.BOX
            )
            # get_flattened_data replaced getdata in newer Pillow; keep both.
            getter = getattr(gray, "get_flattened_data", None) or gray.getdata
            pixels = list(getter())
    except Exception:  # noqa: BLE001 - corrupt/unsupported image: skip, never crash
        return None

    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()


def visual_pairs(files: list[FileFact]) -> list[tuple[int, int]]:
    """Index pairs of visually near-identical images (cross-format allowed)."""
    candidates: list[tuple[int, int]] = []  # (index into files, hash)
    for i, fact in enumerate(files):
        if fact.ext not in IMAGE_EXTS or fact.size > MAX_IMAGE_BYTES:
            continue
        h = dhash(fact.path)
        if h is not None:
            candidates.append((i, h))

    pairs: list[tuple[int, int]] = []
    for a in range(len(candidates)):
        for b in range(a + 1, len(candidates)):
            if hamming(candidates[a][1], candidates[b][1]) <= NEAR_MIN:
                pairs.append((candidates[a][0], candidates[b][0]))
    return pairs
