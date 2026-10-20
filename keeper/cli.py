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
from pathlib import Path

from . import __version__
from .decide import DEFAULT_MODEL, decide, model_available
from .extract import extract_text
from .group import cluster
from .quarantine import QDIR, GroupDecision, apply_quarantine, restore
from .report import markdown_report, terminal_summary
from .scan import scan


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

    print(f"Keeper v{__version__} — scanning {root} ...")

    facts = scan(root)
    print(f"  {len(facts)} files found")

    for fact in facts:
        fact.text, fact.status = extract_text(fact.path)
    readable = sum(1 for f in facts if f.readable)
    flagged = sum(1 for f in facts if f.status == "no_text_layer")
    print(f"  {readable} readable as text, {flagged} with no text layer (scans/images)")

    clusters = cluster(facts)
    print(f"  {len(clusters)} duplicate cluster(s)\n")

    use_model = not args.no_model
    if use_model and clusters and not model_available():
        print("  Ollama is not running — falling back to the rule engine for all groups.")
        print("  (Start it with `ollama serve` to get model verdicts.)\n")
        use_model = False

    decisions: list[GroupDecision] = []
    for group in clusters:
        dec = decide(group, model=args.model, use_model=use_model)
        decisions.append(GroupDecision(files=group, decision=dec))

    print(terminal_summary(decisions, str(root)))

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
