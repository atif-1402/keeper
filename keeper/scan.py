"""Walk a folder and collect file facts (name, size, mtime, hash).

Nothing fancy: Keeper never follows symlinks, never descends into hidden or
noisy directories, and always skips its own quarantine folder.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
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


def scan(root: Path, *, hash_files: bool = True) -> list[FileFact]:
    """Collect FileFacts for every regular file under ``root``."""
    root = root.resolve()
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
                stat = path.stat()
                fact = FileFact(
                    path=path,
                    rel=str(path.relative_to(root)),
                    name=filename,
                    ext=path.suffix.lower(),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
                if hash_files and stat.st_size <= 64 * 1024 * 1024:
                    fact.sha256 = sha256_of(path)
                facts.append(fact)
            except OSError as exc:  # unreadable file: record it, keep going
                facts.append(FileFact(
                    path=path,
                    rel=str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
                    name=filename,
                    ext=path.suffix.lower(),
                    size=0,
                    mtime=0.0,
                    status="error",
                    error=str(exc),
                ))

    return facts
