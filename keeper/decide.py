"""Decide which file in a cluster is the keeper.

Order of preference:
1. Byte-identical copies  -> no model needed, newest timestamp wins.
2. A local model via Ollama (open-weight, offline) reads the metadata and a
   text excerpt of each copy and answers with a schema-enforced JSON verdict:
   keep / reason / confidence. A verdict the model isn't confident about
   gets one richer second look; still unsure, Keeper falls back honestly.
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

# Server-side JSON schema for verdicts (Ollama structured outputs): the model
# physically cannot answer in any other shape. Old Ollama (< 0.5) rejects
# schema objects in `format` — _ask_model degrades to the loose "json" form
# on a 400, and to the rule engine if even that fails.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["keep", "reason", "confidence"],
}

# A verdict below this confidence gets one richer second look; still unsure
# after that, and Keeper says so instead of bluffing.
ESCALATE_BELOW = 0.6
RICH_EXCERPT_CHARS = 900   # richer pass shows more text per copy...
RICH_EXCERPT_CAP = 5       # ...and more copies

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
    escalated: bool = False  # model was unsure; a richer second look happened


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


def _build_user_prompt(group: list[FileFact], *, excerpt_chars: int = 600,
                       excerpt_cap: int = 3, revisit: bool = False) -> str:
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
        f"--- {fact.name} ---\n{_excerpt(fact.text, excerpt_chars)}"
        for fact in _excerpts_for(group, excerpt_cap)
    )

    lines = [
        "DECISION TABLE (read this first):",
        *rows,
        "Rule: prefer signed=YES, then newest=YES.",
        "Excerpts:",
        excerpts,
    ]
    if revisit:
        lines.append(
            "\nYour previous answer had LOW confidence. Look again carefully "
            "at the evidence above before deciding."
        )
    lines.append(
        '\n\nReply with ONLY the JSON object: {"keep": "<exact filename>", '
        '"reason": "<one sentence>", "confidence": <0.0-1.0>}'
    )
    return "\n".join(lines)


def _ask_model(group: list[FileFact], model: str, host: str = OLLAMA_HOST,
               *, revisit: bool = False, schema: bool = True) -> dict:
    payload = {
        "model": model,
        "stream": False,
        # Schema-enforced verdict: structurally cannot freestyle. Old Ollama
        # (< 0.5) rejects schema objects with a 400 — retry once with the
        # loose "json" string form before giving up to the rules.
        "format": VERDICT_SCHEMA if schema else "json",
        "options": {"temperature": 0.0, "num_predict": 120 if revisit else 80},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(
                group,
                excerpt_chars=RICH_EXCERPT_CHARS if revisit else 600,
                excerpt_cap=RICH_EXCERPT_CAP if revisit else 3,
                revisit=revisit,
            )},
        ],
    }
    request = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and schema:
            return _ask_model(group, model, host, revisit=revisit, schema=False)
        raise
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


def _unsure_rule_decision(group: list[FileFact]) -> Decision:
    """The model had its second look and still wasn't sure — say so, then
    let the transparent rules decide instead of trusting a coin flip."""
    dec = _rule_decision(group)
    dec.reason = f"the model was unsure after two looks; {dec.reason}"
    dec.confidence = 0.5
    dec.escalated = True
    return dec


def _pick_from_answer(group: list[FileFact], answer: dict) -> FileFact | None:
    """Match the model's `keep` field to a real file (exact, then fuzzy)."""
    wanted = str(answer.get("keep", "")).strip().strip('"').lower()
    match = next((f for f in group if f.name.lower() == wanted), None)
    if match is None:
        # Model may have echoed a number or a near-name; take best fuzzy hit.
        match = next(
            (f for f in group if wanted and wanted in f.name.lower()), None
        )
    return match


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
        newest = max(group, key=lambda f: (f.mtime, f.size))
        if any(f.visual for f in group):
            # Clustered by perceptual hash: these really are the same picture.
            return Decision(
                keep=newest,
                reason="copies are visually near-identical (perceptual hash); "
                       "kept the newest",
                confidence=0.7,
                method="rule",
            )
        return Decision(
            keep=newest,
            reason="no readable text in this cluster; kept the newest copy",
            confidence=0.5,
            method="rule",
        )

    if not use_model or not model_available(host):
        return _rule_decision(group)

    try:
        answer = _ask_model(group, model, host)
        match = _pick_from_answer(group, answer)
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

        confidence = max(0.0, min(1.0, float(answer.get("confidence", 0.5))))
        if confidence >= ESCALATE_BELOW:
            return Decision(
                keep=match,
                reason=str(answer.get("reason", "model picked this one")).strip(),
                confidence=confidence,
                method="model",
            )

        # Low confidence: one richer second look — more copies, longer
        # excerpts, explicit "you were unsure" nudge. Confident now? Take it.
        answer2 = _ask_model(group, model, host, revisit=True)
        match2 = _pick_from_answer(group, answer2)
        if match2 is not None and _verdict_consistent(match2, group):
            confidence2 = max(0.0, min(1.0, float(answer2.get("confidence", 0.5))))
            if confidence2 >= ESCALATE_BELOW:
                return Decision(
                    keep=match2,
                    reason=str(answer2.get("reason", "second look")).strip(),
                    confidence=confidence2,
                    method="model",
                    escalated=True,
                )
        return _unsure_rule_decision(group)
    except Exception:  # noqa: BLE001 - model trouble must never crash a scan
        return _rule_decision(group)
