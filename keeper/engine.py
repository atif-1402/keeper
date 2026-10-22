"""Shared pipeline runner — one call that runs scan → extract → cluster → decide.

The CLI and the GUI are both thin shells over this. It deliberately carries no
rich/spinner and no tkinter: progress is reported through an optional callback
``progress(phase_text, done, total)`` where ``total <= 0`` means "indeterminate
for this phase". That keeps the engine headless-testable and lets each front
end render progress however it likes (a rich spinner, a tk progressbar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .decide import DEFAULT_MODEL, decide, model_available, would_use_model
from .extract import extract_text
from .group import cluster
from .quarantine import GroupDecision, apply_quarantine, restore
from .scan import scan

ProgressFn = Callable[[str, int, int], None]

# The three modes the GUI exposes, mapped onto the CLI's flags:
#   default -> model when available, honest rules fallback otherwise
#   rules   -> never call the model (--no-model)
#   model   -> require the model; if Ollama is down we still show rule
#              decisions but flag the failure so the caller can surface it
MODE_DEFAULT = "default"
MODE_RULES = "rules"
MODE_MODEL = "model"


@dataclass
class PipelineResult:
    root: Path
    facts: list = field(default_factory=list)
    decisions: list = field(default_factory=list)  # list[GroupDecision]
    files: int = 0
    readable: int = 0
    no_text_layer: int = 0
    clusters: int = 0
    losers: int = 0
    clutter_bytes: int = 0
    use_model: bool = False
    asked: int = 0
    model: str = DEFAULT_MODEL
    mode: str = MODE_DEFAULT
    # Human-readable note when the model could not be used (Ollama down in
    # model mode, or silently fell back in default mode). None when clean.
    model_error: str | None = None


def run_pipeline(root: Path, *, model: str = DEFAULT_MODEL,
                 mode: str = MODE_DEFAULT,
                 progress: ProgressFn | None = None) -> PipelineResult:
    """Run the full scan/group/decide pipeline. Never touches files on disk."""
    root = Path(root)
    use_model = mode != MODE_RULES
    model_only = mode == MODE_MODEL

    def report(text: str, done: int = 0, total: int = 0) -> None:
        if progress is not None:
            progress(text, done, total)

    report("Hashing files", 0, 0)
    facts = scan(root)
    total = len(facts)

    report("Extracting text (txt/md/csv/pdf/docx)", 0, total)
    for i, fact in enumerate(facts, start=1):
        fact.text, fact.status = extract_text(fact.path)
        report("Extracting text (txt/md/csv/pdf/docx)", i, total)

    report("Clustering duplicates")
    clusters = cluster(facts)

    readable = sum(1 for f in facts if f.readable)
    flagged = sum(1 for f in facts if f.status == "no_text_layer")

    use_model_now = use_model
    worthy = sum(1 for g in clusters if would_use_model(g)) if use_model else 0
    model_error: str | None = None
    if use_model and worthy and not model_available():
        if model_only:
            model_error = ("Model mode needs Ollama, but it is not running. "
                           "Showing rule-based decisions instead — start it "
                           "with `ollama serve` for model verdicts.")
        else:
            model_error = ("Ollama is not running — fell back to the rule "
                           "engine. Start it with `ollama serve` to get "
                           "model verdicts.")
        use_model_now = False
        worthy = 0

    decisions: list[GroupDecision] = []
    asked = 0
    for group in clusters:
        needs_ask = worthy and would_use_model(group)
        if needs_ask:
            asked += 1
            report(f"Asking {model} — {group[0].name}", asked, worthy)
        group_decision = decide(group, model=model, use_model=use_model_now)
        decisions.append(GroupDecision(files=group, decision=group_decision))
        if needs_ask:
            report(f"Asking {model} — {group[0].name}", asked, worthy)

    clutter = sum(f.size for g in decisions for f in g.losers)
    losers = sum(len(g.losers) for g in decisions)

    return PipelineResult(
        root=root,
        facts=facts,
        decisions=decisions,
        files=len(facts),
        readable=readable,
        no_text_layer=flagged,
        clusters=len(clusters),
        losers=losers,
        clutter_bytes=clutter,
        use_model=use_model_now,
        asked=asked,
        model=model,
        mode=mode,
        model_error=model_error,
    )
