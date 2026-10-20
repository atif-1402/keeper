"""Keeper's command-line interface.

    keeper <folder>               dry run: scan, group, decide, print report
    keeper <folder> --apply       move losers into _keeper_quarantine/
    keeper <folder> --restore     put everything back
    keeper <folder> --model X     use a different Ollama model
    keeper <folder> --report F    also write a Markdown report
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from . import __version__
from .decide import DEFAULT_MODEL, decide, model_available, would_use_model
from .extract import extract_text
from .group import cluster
from .quarantine import QDIR, GroupDecision, apply_quarantine, restore
from .report import markdown_report, render_decisions
from .scan import scan

console = Console()


@contextmanager
def _phase(text: str) -> Iterator[Callable[[str], None]]:
    """A live spinner that replaces itself on a terminal; a one-line note
    when the output is piped (tests, demos, logs)."""
    if console.is_terminal:
        with Live(Spinner("dots", text=text), console=console,
                  transient=True, refresh_per_second=10) as live:
            def update(new_text: str) -> None:
                live.update(Spinner("dots", text=new_text))
            yield update
    else:
        print(f"  {text} ...", flush=True)
        yield lambda _new: None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="keeper",
        description="Find version-clone files in a folder, ask a local "
        "open-weight model which copy to keep, quarantine the rest. "
        "Dry-run by default: nothing moves without --apply.",
    )
    parser.add_argument("path", nargs="?", help="folder to scan")
    parser.add_argument("--apply", action="store_true",
                        help="move losing copies into _keeper_quarantine/")
    parser.add_argument("--restore", action="store_true",
                        help="undo a previous --apply on this folder")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Ollama model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-model", action="store_true",
                        help="skip the model, use only the rule fallback")
    parser.add_argument("--report", metavar="FILE",
                        help="also write a Markdown report to FILE")
    parser.add_argument("--version", action="version", version=f"keeper {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not args.path:
        print("keeper: give me a folder to scan (e.g. `keeper ~/Downloads`)", file=sys.stderr)
        return 2

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"keeper: {root} is not a folder", file=sys.stderr)
        return 2

    if args.restore:
        restored, error = restore(root)
        if error and restored == 0:
            print(f"keeper: {error}", file=sys.stderr)
            return 1
        print(f"Restored {restored} file(s) from {root / QDIR}.")
        if error:
            print(f"note: {error}")
        return 0

    print(f"Scanning {root} ...", flush=True)

    with _phase("hashing files") as set_phase:
        facts = scan(root)
        set_phase("extracting text (txt/md/csv/pdf/docx)")
        for fact in facts:
            fact.text, fact.status = extract_text(fact.path)
        set_phase("clustering duplicates")
        clusters = cluster(facts)

    readable = sum(1 for f in facts if f.readable)
    flagged = sum(1 for f in facts if f.status == "no_text_layer")
    print(f"  {len(facts)} files · {readable} readable · {flagged} no text layer "
          f"· {len(clusters)} duplicate cluster(s)\n", flush=True)

    use_model = not args.no_model
    if use_model and clusters and not model_available():
        print("  Ollama is not running — falling back to the rule engine.")
        print("  (Start it with `ollama serve` to get model verdicts.)\n")
        use_model = False

    worthy = sum(1 for g in clusters if would_use_model(g)) if use_model else 0

    decisions: list[GroupDecision] = []
    asked = 0
    if clusters:
        label = (f"asking {args.model} (0/{worthy})" if worthy
                 else "deciding from metadata")
        with _phase(label) as set_phase:
            for group in clusters:
                if worthy and would_use_model(group):
                    asked += 1
                    head = f"asking {args.model} — {asked}/{worthy}: {group[0].name}"
                    if console.is_terminal:
                        set_phase(head)
                    else:
                        print(f"  {head}", flush=True)
                decisions.append(GroupDecision(
                    files=group,
                    decision=decide(group, model=args.model, use_model=use_model),
                ))
        print(flush=True)

    render_decisions(decisions)

    if args.report:
        Path(args.report).write_text(markdown_report(decisions, str(root)), encoding="utf-8")
        print(f"Report written to {args.report}")

    if not decisions:
        return 0

    if args.apply:
        manifest = apply_quarantine(root, decisions)
        if manifest:
            loser_count = sum(len(g.losers) for g in decisions)
            print(f"Quarantined {loser_count} file(s) -> {root / QDIR}/")
            print(f"  manifest: {manifest}")
            print(f"  undo with: keeper {args.path} --restore   (or {root / QDIR / 'restore.sh'})")
    else:
        loser_count = sum(len(g.losers) for g in decisions)
        print(f"DRY RUN — nothing was moved ({loser_count} loser(s) would be quarantined).")
        print(f"Run again with --apply to quarantine them, or --restore to undo.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
