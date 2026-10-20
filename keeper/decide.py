"""Decide which file in a cluster is the keeper.

Order of preference:
1. Byte-identical copies  -> no model needed, newest timestamp wins.
2. A local model via Ollama (open-weight, offline) reads the metadata and a
   text excerpt of each copy and answers with JSON: keep / reason / confidence.
3. Rule fallback (model down or answer unusable): prefer files Keeper could
   actually read, then newest, then biggest. Safe, boring, explainable.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .scan import FileFact

DEFAULT_MODEL = os.environ.get("KEEPER_MODEL", "gemma3:1b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

SYSTEM_PROMPT = (
    "You are Keeper, a careful file assistant. You are shown several copies of "
    "the same document from one folder. Pick the single file the user should "
    "KEEP. Decision order: (1) a copy containing signatures, approvals or "
    "completed fields beats unsigned drafts; (2) otherwise the most recently "
    "modified copy; (3) otherwise the longest/most complete text. "
    "Reply with ONLY a JSON object, no prose, in exactly this shape: "
    '{"keep": "<exact filename>", "reason": "<one short sentence>", '
    '"confidence": <0.0-1.0>}'
)

_SIGNATURE_WORDS = ("signed", "signature", "signed:", "executed", "approved by")


def is_signed(fact: FileFact) -> bool:
    """Heuristic: does this copy look signed/approved (from name + content)?"""
    haystack = (fact.name + " " + fact.text[:4000]).lower()
    if "unsigned" in haystack:
        return False
    return any(word in haystack for word in _SIGNATURE_WORDS)


def hard_winner(group: list[FileFact]) -> FileFact:
    """The verdict Keeper's metadata signals point at: signed > newest > biggest.

    Used both to fill the prompt's decision table and to sanity-check the
    model's answer — a tiny model that flips must not override hard evidence.
    """
    signed = [f for f in group if is_signed(f)]
    pool = signed or group
    return max(pool, key=lambda f: (f.mtime, f.size))


def _verdict_consistent(pick: FileFact, group: list[FileFact]) -> bool:
    """A model verdict is acceptable if it matches our hard signals: any signed
    copy wins when one exists, otherwise the newest must win."""
    if any(is_signed(f) for f in group):
        return is_signed(pick)
    newest = max(f.mtime for f in group)
    return pick.mtime == newest


@dataclass
class Decision:
    keep: FileFact
    reason: str
    confidence: float
    method: str  # identical | model | rule


def model_available(host: str = OLLAMA_HOST) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _excerpt(text: str, limit: int = 600) -> str:
    """Short excerpt — enough for a tiny model to judge, cheap on prompt tokens."""
    if len(text) <= limit:
        return text.strip()
    head = text[: int(limit * 0.75)].strip()
    tail = text[-int(limit * 0.25):].strip()
    return f"{head}\n...\n{tail}"


def _excerpts_for(group: list[FileFact], cap: int = 3) -> list[FileFact]:
    """Pick at most ``cap`` readable files to show the model: signed first,
    then the newest, then the rest in order. A 15-copy cluster gets the three
    copies that actually matter instead of 15 walls of text."""
    readable = [f for f in group if f.readable]
    newest = max(group, key=lambda f: f.mtime)
    picked: list[FileFact] = []
    for candidate in ([f for f in readable if is_signed(f)]
                      + ([newest] if newest in readable else [])
                      + readable):
        if candidate not in picked:
            picked.append(candidate)
        if len(picked) >= cap:
            break
    return sorted(picked, key=group.index)


def _build_user_prompt(group: list[FileFact]) -> str:
    # Compact decision table FIRST — tiny models scan this reliably. Heavy
    # metadata (dates) in the table rows makes gemma3:1b lose the plot, so we
    # pre-compute the decision signals and keep rows terse. Evidence follows.
    newest = max(group, key=lambda f: f.mtime)
    width = max(len(f.name) for f in group) + 3
    rows = [
        f"{i}. {fact.name.ljust(width)}signed={'YES' if is_signed(fact) else 'no'}  "
        f"newest={'YES' if fact is newest else 'no'}  "
        f"bytes={fact.size:,}"
        for i, fact in enumerate(group, start=1)
    ]

    excerpts = "\n\n".join(
        f"--- {fact.name} ---\n{_excerpt(fact.text)}"
        for fact in _excerpts_for(group)
    )

    return "\n".join(
        [
            "DECISION TABLE (read this first):",
            *rows,
            "Rule: prefer signed=YES, then newest=YES.",
            "Excerpts:",
            excerpts,
            '\n\nReply with ONLY the JSON object: {"keep": "<exact filename>", '
            '"reason": "<one sentence>", "confidence": <0.0-1.0>}',
        ]
    )


def _ask_model(group: list[FileFact], model: str, host: str = OLLAMA_HOST) -> dict:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 80},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(group)},
        ],
    }
    request = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    content = body["message"]["content"]
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"model returned no JSON: {content[:200]!r}")
    return json.loads(match.group(0))


def _rule_decision(group: list[FileFact]) -> Decision:
    """Fallback: signed beats unreadable beats newest beats biggest."""
    signed = [f for f in group if is_signed(f)]
    readable = [f for f in group if f.readable]
    if signed:
        pool, why = signed, "newest signed copy"
    elif readable:
        pool, why = readable, "newest copy with text"
    else:
        pool, why = group, "no readable text; kept the newest copy"
    keep = max(pool, key=lambda f: (f.mtime, f.size))
    return Decision(keep=keep, reason=why, confidence=0.6, method="rule")


def would_use_model(group: list[FileFact]) -> bool:
    """True when this cluster actually warrants a model call: not already
    decided by byte-identity, and at least one copy has text to read."""
    if len({f.sha256 for f in group}) == 1 and group[0].sha256:
        return False
    return any(f.readable for f in group)


def decide(group: list[FileFact], *, model: str = DEFAULT_MODEL,
           use_model: bool = True, host: str = OLLAMA_HOST) -> Decision:
    """Pick the keeper for one cluster."""
    if len({f.sha256 for f in group}) == 1 and group[0].sha256:
        keep = max(group, key=lambda f: (f.mtime, f.size))
        return Decision(
            keep=keep,
            reason="all copies are byte-identical; kept the newest timestamp",
            confidence=1.0,
            method="identical",
        )

    if not any(f.readable for f in group):
        # Nothing readable (zips, images, videos): a model would only guess
        # from filenames — the metadata rule already knows the answer.
        keep = max(group, key=lambda f: (f.mtime, f.size))
        return Decision(
            keep=keep,
            reason="no readable text in this cluster; kept the newest copy",
            confidence=0.5,
            method="rule",
        )

    if not use_model or not model_available(host):
        return _rule_decision(group)

    try:
        answer = _ask_model(group, model, host)
        wanted = str(answer.get("keep", "")).strip().strip('"').lower()
        match = next((f for f in group if f.name.lower() == wanted), None)
        if match is None:
            # Model may have echoed a number or a near-name; take best fuzzy hit.
            match = next(
                (f for f in group if wanted and wanted in f.name.lower()), None
            )
        if match is None:
            return _rule_decision(group)

        # Sanity-check: a tiny model that flips must not override hard
        # evidence (signed / newest flags). Disagreement -> honest fallback.
        if not _verdict_consistent(match, group):
            winner = hard_winner(group)
            signal = "signed" if is_signed(winner) else "newest"
            return Decision(
                keep=winner,
                reason=f"model disagreed; the {signal} copy wins on metadata",
                confidence=0.5,
                method="rule",
            )

        return Decision(
            keep=match,
            reason=str(answer.get("reason", "model picked this one")).strip(),
            confidence=max(0.0, min(1.0, float(answer.get("confidence", 0.5)))),
            method="model",
        )
    except Exception:  # noqa: BLE001 - model trouble must never crash a scan
        return _rule_decision(group)
