"""Group files that are versions of the same thing.

Two passes:

1. **Identical copies** — same sha256 = same bytes. Trivial cluster.
2. **Near-duplicates** — same *thing*, different content: filename-token
   overlap ("rental_agreement_final_v2(1).pdf" vs "rental_agreement.pdf")
   or content similarity (TF-IDF cosine over extracted text).

Union-find glues everything into clusters. Files Keeper couldn't read
(scans, binaries with no name twin) are never content-compared — they only
cluster by filename, so we never confidently merge things we can't see.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from itertools import combinations

from .scan import FileFact

# Tokens that describe *file management*, not the document itself.
# Stripping them gives the "same stem" signal: rental_agreement_final_v2(1)
# and rental_agreement_scan both normalise to "rental agreement".
VERSION_TOKENS = {
    "final", "copy", "new", "old", "latest", "draft", "revised", "rev",
    "signed", "unsigned", "scan", "scanned", "original", "edited", "edit",
    "v", "version", "doc", "document", "finalized", "approved", "sent",
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_V_TOKEN_RE = re.compile(r"^v\d+$")  # v2, v3, ... — pure file-version noise

# Thresholds.
NAME_JACCARD_MIN = 0.6    # filename token overlap to call two files related
CONTENT_COSINE_MIN = 0.55  # text similarity to call two files related


def tokens_of(name: str) -> set[str]:
    """All word tokens in a filename, lowercased (keeps version digits)."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return set(_WORD_RE.findall(stem.lower()))


def norm_stem(name: str) -> str:
    """Filename stem with the file-management words removed."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    words = _WORD_RE.findall(stem.lower())
    kept = [w for w in words if w not in VERSION_TOKENS and not _V_TOKEN_RE.match(w)]
    return " ".join(kept or words)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _tfidf_vectors(texts: list[str]) -> tuple[list[dict[str, float]], dict[str, float]]:
    """Tiny TF-IDF over a small set of documents (pure Python, no deps)."""
    doc_tfs: list[dict[str, int]] = []
    df: dict[str, int] = defaultdict(int)
    for text in texts:
        counts: dict[str, int] = defaultdict(int)
        for word in _WORD_RE.findall(text.lower()):
            counts[word] += 1
        doc_tfs.append(counts)
        for word in counts:
            df[word] += 1

    n = max(len(texts), 1)
    idf = {w: math.log((n + 1) / (c + 1)) + 1.0 for w, c in df.items()}

    vectors: list[dict[str, float]] = []
    for counts in doc_tfs:
        vec = {w: tf * idf[w] for w, tf in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({w: v / norm for w, v in vec.items()})
    return vectors, idf


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(w, 0.0) for w, v in a.items())


def cluster(files: list[FileFact]) -> list[list[FileFact]]:
    """Return clusters of 2+ files that are versions of the same thing."""
    uf = _UnionFind(len(files))

    # Pass 1: byte-identical copies.
    by_hash: dict[str, list[int]] = defaultdict(list)
    for i, fact in enumerate(files):
        if fact.sha256:
            by_hash[fact.sha256].append(i)
    for indices in by_hash.values():
        for i in indices[1:]:
            uf.union(indices[0], i)

    # Pass 2: near-duplicates (name + content signals).
    tok_sets = [tokens_of(f.name) for f in files]
    norm_stems = [norm_stem(f.name) for f in files]

    # Candidate pairs: same normalised stem always; otherwise compare only
    # within the same parent folder (perf guard for big directories) unless
    # the folder is small enough for an all-pairs pass.
    by_stem: dict[str, list[int]] = defaultdict(list)
    for i, stem in enumerate(norm_stems):
        if stem:
            by_stem[stem].append(i)

    candidate_pairs: set[tuple[int, int]] = set()
    for indices in by_stem.values():
        for a, b in combinations(indices, 2):
            candidate_pairs.add((a, b))

    small_folder = len(files) <= 400
    for a, b in combinations(range(len(files)), 2):
        if (a, b) in candidate_pairs:
            continue
        if not small_folder and files[a].path.parent != files[b].path.parent:
            continue
        candidate_pairs.add((a, b))

    # Content vectors for readable files (one pass over the texts).
    readable_idx = [i for i, f in enumerate(files) if f.readable]
    vectors: dict[int, dict[str, float]] = {}
    if readable_idx:
        vec_list, _idf = _tfidf_vectors([files[i].text[:8000] for i in readable_idx])
        vectors = dict(zip(readable_idx, vec_list))

    for a, b in candidate_pairs:
        if uf.find(a) == uf.find(b):
            continue
        # Name signal.
        name_score = (
            len(tok_sets[a] & tok_sets[b]) / len(tok_sets[a] | tok_sets[b])
            if tok_sets[a] and tok_sets[b] else 0.0
        )
        matched = name_score >= NAME_JACCARD_MIN or (
            norm_stems[a] and norm_stems[a] == norm_stems[b]
        )
        # Content signal.
        if not matched and a in vectors and b in vectors:
            matched = _cosine(vectors[a], vectors[b]) >= CONTENT_COSINE_MIN
        if matched:
            uf.union(a, b)

    groups: dict[int, list[FileFact]] = defaultdict(list)
    for i, fact in enumerate(files):
        groups[uf.find(i)].append(fact)

    clusters = [members for members in groups.values() if len(members) >= 2]
    clusters.sort(key=lambda members: (-len(members), members[0].rel))
    return clusters
