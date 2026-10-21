"""Keeper's command-line interface.

    keeper <folder>               dry run: scan, group, decide, print report
    keeper <folder> --apply       move losers into _keeper_quarantine/
    keeper <folder> --restore     put everything back
    keeper <folder> --model X     use a different Ollama model
    keeper <folder> --report F    also write a Markdown report
    keeper <folder> --json        machine-readable verdicts on stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from . import __version__
from .decide import DEFAULT_MODEL, decide, model_available, would_use_model
from .extract import extract_text
from .group import cluster
from .quarantine import QDIR, GroupDecision, apply_quarantine, restore
from .report import fmt_bytes, markdown_report, render_decisions
from .scan import scan

console = Console()


def _fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class _Phase:
    """One live spinner line: current phase, optional [done/total] counter,
    and — unless disabled — a running clock plus a rough countdown derived
    from measured throughput (so a slow machine honestly shows minutes and a
    fast one shows seconds).

    On a terminal it animates in place and erases itself when done; when the
    output is piped (tests, demos, logs) each phase prints one plain line.
    ``stream`` redirects every phase line (spinner included) — used by
    ``--json`` so stdout carries only the JSON document.
    """

    def __init__(self, text: str, *, show_time: bool = True,
                 stream=None) -> None:
        self.text = text
        self.show_time = show_time
        self.stream = stream
        self.total: int | None = None
        self.done = 0
        self.started = time.monotonic()
        self._live: Live | None = None
        self._spinner: Spinner | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def _label(self) -> str:
        elapsed = time.monotonic() - self.started
        label = self.text
        if self.total and self.done:
            label += f" [{self.done}/{self.total}]"
        if self.show_time and elapsed >= 1:
            if self.total and self.done and self.done < self.total:
                left = elapsed / self.done * (self.total - self.done)
                label += f" · about {_fmt_duration(left)} left"
            else:
                label += f" · {_fmt_duration(elapsed)}"
        return label

    def set(self, text: str, *, total: int | None = None, done: int = 0) -> None:
        self.text, self.total, self.done = text, total, done
        if self._live is None:  # piped: announce each phase once
            print(f"  {self._label()} ...", file=self.stream, flush=True)

    def bump(self, n: int = 1) -> None:
        self.done += n
        if self._live is None and self.total and self.done < self.total \
                and self.done % 25 == 0:
            print(f"  {self._label()} ...", file=self.stream, flush=True)

    def __enter__(self) -> "_Phase":
        term = console.is_terminal or (self.stream is not None and
                                       getattr(self.stream, "isatty", lambda: False)())
        if term:
            # One Spinner instance for the whole phase: its animation clock
            # keeps running while only the text changes, so the frames turn
            # smoothly instead of resetting every tick.
            live_console = (console if self.stream is None
                            else Console(file=self.stream))
            self._spinner = Spinner("dots", self._label())
            self._live = Live(self._spinner, console=live_console, transient=True,
                              refresh_per_second=10)
            self._live.start()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        else:
            print(f"  {self._label()} ...", file=self.stream, flush=True)
        return self

    def _tick(self) -> None:
        while not self._stop.wait(0.3):
            self._spinner.text = self._label()
            self._live.update(self._spinner)

    def __exit__(self, *_exc: object) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)  # no updates after the line is gone
        if self._live is not None:
            self._live.stop()


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
                        help="rule engine only — never call the model")
    parser.add_argument("--model-only", action="store_true",
                        help="require the model — fail if Ollama is not running "
                             "(default: model when available, rules otherwise)")
    parser.add_argument("--no-eta", action="store_true",
                        help="hide the elapsed/remaining time in the spinner")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output: one JSON document on "
                             "stdout (progress goes to stderr)")
    parser.add_argument("--report", metavar="FILE",
                        help="also write a Markdown report to FILE")
    parser.add_argument("--version", action="version", version=f"keeper {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    as_json = args.json
    # In JSON mode every progress line goes to stderr; stdout carries only
    # the final JSON document (parseable even when piped into jq).
    def say(msg: str = "") -> None:
        print(msg, file=sys.stderr if as_json else sys.stdout, flush=True)
    phase_stream = sys.stderr if as_json else None

    if not args.path:
        print("keeper: give me a folder to scan (e.g. `keeper ~/Downloads`)", file=sys.stderr)
        return 2

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"keeper: {root} is not a folder", file=sys.stderr)
        return 2

    if args.restore:
        restored, error = restore(root)
        if as_json:
            print(json.dumps({
                "restored": restored,
                "error": error,
            }, indent=2))
            return 1 if (error and restored == 0) else 0
        if error and restored == 0:
            print(f"keeper: {error}", file=sys.stderr)
            return 1
        print(f"Restored {restored} file(s) from {root / QDIR}.")
        if error:
            print(f"note: {error}")
        return 0

    if args.no_model and args.model_only:
        print("keeper: --no-model and --model-only contradict each other",
              file=sys.stderr)
        return 2

    show_time = not args.no_eta
    say(f"Scanning {root} ...")

    with _Phase("hashing files", show_time=show_time,
                stream=phase_stream) as phase:
        facts = scan(root)
        phase.set("extracting text (txt/md/csv/pdf/docx)",
                  total=len(facts), done=0)
        for fact in facts:
            fact.text, fact.status = extract_text(fact.path)
            phase.bump()
        phase.set("clustering duplicates")
        clusters = cluster(facts)

    readable = sum(1 for f in facts if f.readable)
    flagged = sum(1 for f in facts if f.status == "no_text_layer")
    say(f"  {len(facts)} files · {readable} readable · {flagged} no text layer "
        f"· {len(clusters)} duplicate cluster(s)\n")

    use_model = not args.no_model
    worthy = sum(1 for g in clusters if would_use_model(g)) if use_model else 0
    if use_model and worthy and not model_available():
        if args.model_only:
            print("keeper: --model-only, but Ollama is not running — "
                  "start it with `ollama serve`", file=sys.stderr)
            return 1
        say("  Ollama is not running — falling back to the rule engine.")
        say("  (Start it with `ollama serve` to get model verdicts.)\n")
        use_model, worthy = False, 0

    decisions: list[GroupDecision] = []
    asked = 0
    if clusters:
        label = (f"asking {args.model}" if worthy else "deciding from metadata")
        with _Phase(label, show_time=show_time, stream=phase_stream) as phase:
            phase.total = worthy or None
            for group in clusters:
                needs_ask = worthy and would_use_model(group)
                if needs_ask:
                    asked += 1
                    # total=phase.total (not worthy): an escalation may have
                    # grown the countdown already — never shrink it back.
                    phase.set(f"asking {args.model} — {group[0].name}",
                              total=phase.total or worthy, done=asked - 1)
                group_decision = decide(group, model=args.model,
                                        use_model=use_model)
                if group_decision.escalated:
                    # A second, richer model look happened: the honest
                    # countdown grows by one so the ETA stays truthful.
                    phase.total = (phase.total or worthy) + 1
                decisions.append(GroupDecision(files=group,
                                               decision=group_decision))
                if needs_ask:
                    phase.bump()
        say()

    clutter = sum(f.size for g in decisions for f in g.losers)
    loser_total = sum(len(g.losers) for g in decisions)

    if as_json:
        payload = {
            "keeper": __version__,
            "root": str(root),
            "model": {"requested": args.model,
                      "used": bool(use_model and asked),
                      "calls": asked},
            "files": len(facts),
            "readable": readable,
            "no_text_layer": flagged,
            "clusters": len(clusters),
            "losers": loser_total,
            "clutter_bytes": clutter,
            "clutter": fmt_bytes(clutter),
            "applied": False,
            "decisions": [
                {
                    "cluster": i,
                    "copies": len(g.files),
                    "keep": g.decision.keep.rel,
                    "reason": g.decision.reason,
                    "confidence": round(g.decision.confidence, 2),
                    "method": g.decision.method,
                    "escalated": g.decision.escalated,
                    "losers": [f.rel for f in g.losers],
                    "loser_bytes": sum(f.size for f in g.losers),
                }
                for i, g in enumerate(decisions, start=1)
            ],
        }
    else:
        render_decisions(decisions)

    if args.report:
        Path(args.report).write_text(markdown_report(decisions, str(root)), encoding="utf-8")
        say(f"Report written to {args.report}")

    if not decisions:
        if as_json:
            print(json.dumps(payload, indent=2))
        return 0

    if args.apply:
        manifest = apply_quarantine(root, decisions)
        if manifest:
            if as_json:
                payload["applied"] = True
                payload["quarantine"] = str(root / QDIR)
            else:
                print(f"Quarantined {loser_total} file(s) -> {root / QDIR}/")
                print(f"  manifest: {manifest}")
                print(f"  undo with: keeper {args.path} --restore   (or {root / QDIR / 'restore.sh'})")
    elif not as_json:
        say(f"DRY RUN — nothing was moved ({loser_total} loser(s) would be "
            f"quarantined · {fmt_bytes(clutter)} of duplicate clutter).")
        say("Run again with --apply to quarantine them, or --restore to undo.")

    if as_json:
        print(json.dumps(payload, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
