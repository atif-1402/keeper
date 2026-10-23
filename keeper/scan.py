"""Walk a folder and collect file facts (name, size, mtime, hash).

Nothing fancy: Keeper never follows symlinks, never descends into hidden or
noisy directories, and always skips its own quarantine folder.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

# Directories Keeper refuses to walk into.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "_keeper_quarantine", "node_modules",
    "__pycache__", ".venv", "venv", ".cache", ".local", ".config",
    ".npm", ".cargo", ".rustup",
}

# Cap on how much extracted text we keep per file (enough for similarity +
# a meaty model prompt, small enough to stay fast on big folders).
MAX_TEXT_CHARS = 200_000

# How big a file may be before we give up hashing it for the byte-identical
# pass. Old default was 64 MB, which silently hid the usual Downloads
# offenders — a re-downloaded 100 MB installer, two copies of the same
# video. Hashing reads sequentially and costs well under a second per
# 500 MB on any drive made this decade, so the cap only exists to stop a
# folder of multi-gigabyte ISOs from stalling the scan.
DEFAULT_MAX_HASH_MB = 512


@dataclass
class FileFact:
    """Everything Keeper knows about one file."""

    path: Path
    rel: str          # path relative to the scanned root
    name: str
    ext: str
    size: int
    mtime: float
    sha256: str | None = None
    text: str = ""
    status: str = "ok"  # ok | binary | no_text_layer | error
    error: str | None = None
    visual: bool = False  # grouped via perceptual image hash (see visual.py)

    @property
    def readable(self) -> bool:
        return self.status == "ok" and len(self.text.strip()) >= 40


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _hash_cap_ok(size: int, cap: int) -> bool:
    """Should this file be hashed? ``cap <= 0`` means no limit."""
    return cap <= 0 or size <= cap


def _glob_match(posix_rel: str, name: str, pattern: str) -> bool:
    """Match a user glob against the path relative to the root, or the name.

    Patterns are matched as given, so ``*.pdf`` catches a top-level PDF and
    ``reports/*.pdf`` narrows to a subfolder. Matching the bare name too
    means ``--exclude node_modules`` works even when written without a glob.
    """
    return fnmatch(posix_rel, pattern) or fnmatch(name, pattern)


def _keep_file(path: Path, rel: str, size: int, *,
               exts: set[str] | None = None, min_size: int = 0,
               include: Sequence[str] = (), exclude: Sequence[str] = ()) -> bool:
    """Apply the user's targeting filters to one candidate file."""
    if exts and path.suffix.lower() not in exts:
        return False
    if size < min_size:
        return False
    posix = rel.replace(os.sep, "/")
    if include and not any(_glob_match(posix, path.name, p) for p in include):
        return False
    if any(_glob_match(posix, path.name, p) for p in exclude):
        return False
    return True


def scan(root: Path, *, hash_files: bool = True,
         max_hash_bytes: int | None = None,
         exts: set[str] | None = None, min_size: int = 0,
         include: Sequence[str] = (), exclude: Sequence[str] = ()) -> list[FileFact]:
    """Collect FileFacts for every regular file under ``root``.

    ``max_hash_bytes`` caps the byte-identical pass (see
    :data:`DEFAULT_MAX_HASH_MB`); pass ``0`` to hash everything.
    ``exts``/``min_size``/``include``/``exclude`` narrow which files are
    considered at all — they are applied during the walk so nothing that
    will be ignored is ever hashed or read.
    """
    root = root.resolve()
    if max_hash_bytes is None:
        max_hash_bytes = DEFAULT_MAX_HASH_MB * 1024 * 1024
    facts: list[FileFact] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in-place so os.walk doesn't descend into skipped/hidden dirs.
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if filename.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:  # not under root (symlinked dir): keep going
                rel = str(path)
            try:
                stat = path.stat()
            except OSError as exc:  # unreadable file: record it, keep going
                facts.append(FileFact(
                    path=path, rel=rel, name=filename, ext=path.suffix.lower(),
                    size=0, mtime=0.0, status="error", error=str(exc),
                ))
                continue

            if not _keep_file(path, rel, stat.st_size, exts=exts,
                              min_size=min_size, include=include, exclude=exclude):
                continue

            sha: str | None = None
            try:
                if hash_files and _hash_cap_ok(stat.st_size, max_hash_bytes):
                    sha = sha256_of(path)
            except OSError as exc:  # vanished or unreadable mid-hash
                facts.append(FileFact(
                    path=path, rel=rel, name=filename, ext=path.suffix.lower(),
                    size=0, mtime=0.0, status="error", error=str(exc),
                ))
                continue

            facts.append(FileFact(
                path=path,
                rel=rel,
                name=filename,
                ext=path.suffix.lower(),
                size=stat.st_size,
                mtime=stat.st_mtime,
                sha256=sha,
            ))

    return facts
