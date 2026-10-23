"""Keeper's command-line interface.

    keeper <folder>               dry run: scan, group, decide, print report
    keeper <folder> --apply       move losers into _keeper_quarantine/
    keeper <folder> --restore     put everything back
    keeper <folder> --model X     use a different Ollama model
    keeper <folder> --report F    also write a Markdown report
    keeper <folder> --json        machine-readable verdicts on stdout

Targeting (scope what is even considered):

    --ext pdf,jpg,docx            only these extensions
    --min-size 10k                skip files below this size
    --include/--exclude GLOB      path globs, both repeatable
    --max-hash-mb N               byte-identical pass cap (0 = no limit)

Deciding (keep control of the verdict):

    --only 1,3,5 / --skip 2       act on a subset of clusters
    --interactive                 walk each cluster and override the keeper
    --fail-on-clutter             exit 3 when clutter is found
    --quiet                       no progress or summary output
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from . import __version__
from .decide import (DEFAULT_MODEL, Decision, decide, model_available,
                     would_use_model)
from .extract import extract_text
from .group import cluster
from .quarantine import QDIR, GroupDecision, apply_quarantine, restore
from .report import fmt_bytes, markdown_report, render_decisions
from .scan import DEFAULT_MAX_HASH_MB, scan

console = Console()

# Exit codes: 0 fine, 1 runtime error, 2 usage error, 3 clutter found
# (the last only when --fail-on-clutter is set, so scripts can tell
# "there was work to do" apart from "something went wrong").
EXIT_CLUTTER = 3


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
                 stream=None, quiet: bool = False) -> None:
        self.text = text
        self.show_time = show_time
        self.stream = stream
        self.quiet = quiet
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
        if self.quiet:
            return
        if self._live is None:  # piped: announce each phase once
            print(f"  {self._label()} ...", file=self.stream, flush=True)

    def bump(self, n: int = 1) -> None:
        self.done += n
        if self.quiet:
            return
        if self._live is None and self.total and self.done < self.total \
                and self.done % 25 == 0:
            print(f"  {self._label()} ...", file=self.stream, flush=True)

    def __enter__(self) -> "_Phase":
        if self.quiet:
            return self
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


_SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3,
}


def parse_size(text: str) -> int:
    """``10k`` -> 10240, ``2.5M`` -> 2621440, ``500`` -> 500 bytes."""
    cleaned = text.strip().lower().replace("_", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", cleaned)
    if not match or match.group(2) not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(
            f"bad size {text!r} — try bytes (500), or 10k, 2M, 1G"
        )
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def parse_exts(text: str) -> set[str]:
    """``pdf,.JPG,docx`` -> ``{'.pdf', '.jpg', '.docx'}``."""
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("empty --ext list")
    return {p if p.startswith(".") else f".{p}" for p in parts}


def parse_clusters(text: str) -> set[int]:
    """``1,3,5-7`` -> ``{1, 3, 5, 6, 7}``. Ranges are inclusive."""
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                first, _, last = part.partition("-")
                out.update(range(int(first), int(last) + 1))
            else:
                out.add(int(part))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"bad cluster list {text!r} — try 1,3,5 or 2-4"
            ) from exc
    if not out or any(n < 1 for n in out):
        raise argparse.ArgumentTypeError(
            f"bad cluster list {text!r} — cluster numbers start at 1"
        )
    return out


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
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="print nothing but errors (and the JSON document "
                             "when --json is set)")

    # Targeting: what gets considered at all.
    target = parser.add_argument_group(
        "targeting", "narrow what Keeper looks at before it hashes or reads")
    target.add_argument("--ext", type=parse_exts, metavar="LIST",
                        help="only consider these extensions, comma-separated "
                             "(e.g. pdf,jpg,docx)")
    target.add_argument("--min-size", type=parse_size, default=0, metavar="SIZE",
                        help="skip files smaller than SIZE (e.g. 10k, 2M, 1G)")
    target.add_argument("--include", action="append", default=[], metavar="GLOB",
                        help="only scan paths matching GLOB; repeatable, "
                             "matched against the path and the file name")
    target.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip paths matching GLOB; repeatable")
    target.add_argument("--max-hash-mb", type=int, default=DEFAULT_MAX_HASH_MB,
                        metavar="N",
                        help="hash files up to N MB when hunting byte-identical "
                             f"copies (default: {DEFAULT_MAX_HASH_MB}; 0 = no limit)")

    # Deciding: who gets the final say.
    decide_group = parser.add_argument_group(
        "deciding", "keep control over which verdicts are acted on")
    decide_group.add_argument("--only", type=parse_clusters, metavar="LIST",
                              help="act only on these cluster numbers "
                                   "(e.g. 1,3,5 or 2-4)")
    decide_group.add_argument("--skip", type=parse_clusters, metavar="LIST",
                              help="act on every cluster except these numbers")
    decide_group.add_argument("--interactive", action="store_true",
                              help="review each cluster in turn and override "
                                   "the keeper (needs a terminal)")
    decide_group.add_argument("--fail-on-clutter", action="store_true",
                              help=f"exit with code {EXIT_CLUTTER} when clutter "
                                   "is found, 0 when the folder is tidy")

    parser.add_argument("--version", action="version", version=f"keeper {__version__}")
    return parser.parse_args(argv)


def _interactive_review(
    numbered: list[tuple[int, GroupDecision]],
) -> list[tuple[int, GroupDecision]]:
    """Walk each cluster, letting the user confirm, re-pick, or drop it.

    Stopping early keeps every verdict reached so far and leaves the rest
    alone — an unreviewed cluster is never acted on. Nothing moves until
    the normal ``--apply`` step, so this is still a dry run while you review.
    """
    accepted: list[tuple[int, GroupDecision]] = []
    total = len(numbered)
    for position, (number, gd) in enumerate(numbered, start=1):
        dec = gd.decision
        print(f"\nCluster {number} of {total} — {len(gd.files)} copies")
        for index, fact in enumerate(gd.files, start=1):
            role = "keep" if fact.path == dec.keep.path else "loser"
            print(f"  {index}. [{role}] {fact.rel}  ({fmt_bytes(fact.size)})")
        print(f"  why: {dec.reason}  [{dec.method}, confidence {dec.confidence:.2f}]")

        while True:
            try:
                raw = input("  [Enter] accept · [number] keep that copy · "
                            "[s] skip · [q] stop > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raw = "q"
            if raw in ("", "y", "yes"):
                accepted.append((number, gd))
                break
            if raw in ("s", "skip"):
                print(f"  cluster {number} skipped — it will be left alone")
                break
            if raw in ("q", "quit", "exit"):
                print(f"\n  Review stopped — {total - position} cluster(s) after "
                      f"this one will not be touched.")
                return accepted
            try:
                choice = int(raw)
            except ValueError:
                print("    type a copy number, s, or q — or press Enter to accept")
                continue
            if not 1 <= choice <= len(gd.files):
                print(f"    pick a number from 1 to {len(gd.files)}")
                continue
            pick = gd.files[choice - 1]
            if pick.path == dec.keep.path:
                print("    that copy is already the keeper — press Enter to accept")
                continue
            gd = GroupDecision(files=gd.files, decision=Decision(
                keep=pick,
                reason=f"you chose copy {choice}",
                confidence=1.0,
                method="manual",
            ))
            print(f"  keeper is now {pick.rel}")
            accepted.append((number, gd))
            break
    return accepted


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    A reader closing the pipe early (`keeper ~/Downloads --json | head`)
    must never dump a traceback — Keeper reports success and shuts up,
    the way every other command-line tool does.
    """
    try:
        return _main(argv)
    except BrokenPipeError:
        try:  # keep the interpreter's shutdown flush from complaining too
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:  # stdout may be a capture object with no fd
            pass
        return 0


def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    as_json = args.json
    quiet = args.quiet
    # In JSON mode every progress line goes to stderr; stdout carries only
    # the final JSON document (parseable even when piped into jq).
    # --quiet silences both: only errors (and the JSON document) are printed.
    def say(msg: str = "") -> None:
        if quiet:
            return
        print(msg, file=sys.stderr if as_json else sys.stdout, flush=True)
    phase_stream = sys.stderr if as_json else None

    if args.interactive and (as_json or quiet):
        print("keeper: --interactive needs plain terminal output — drop "
              "--json or --quiet", file=sys.stderr)
        return 2
    if args.interactive and not sys.stdin.isatty():
        print("keeper: --interactive needs a terminal (stdin is not one)",
              file=sys.stderr)
        return 2
    if args.max_hash_mb < 0:
        print("keeper: --max-hash-mb cannot be negative (0 means no limit)",
              file=sys.stderr)
        return 2

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
    filters = []
    if args.ext:
        filters.append("ext " + ",".join(sorted(args.ext)))
    if args.min_size:
        filters.append(">= " + fmt_bytes(args.min_size))
    if args.include:
        filters.append("include " + ",".join(args.include))
    if args.exclude:
        filters.append("exclude " + ",".join(args.exclude))
    if args.max_hash_mb != DEFAULT_MAX_HASH_MB:
        filters.append("hash cap "
                       + ("none" if args.max_hash_mb == 0
                          else f"{args.max_hash_mb} MB"))
    say(f"Scanning {root}" + (f"  [{' · '.join(filters)}]" if filters else "")
        + " ...")

    with _Phase("hashing files", show_time=show_time, quiet=quiet,
                stream=phase_stream) as phase:
        facts = scan(
            root,
            max_hash_bytes=args.max_hash_mb * 1024 * 1024,
            exts=args.ext,
            min_size=args.min_size,
            include=args.include,
            exclude=args.exclude,
        )
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
        with _Phase(label, show_time=show_time, quiet=quiet,
                    stream=phase_stream) as phase:
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

    # Keep the cluster numbers the user saw in an earlier run, so --only 3
    # still reports cluster 3 rather than renumbering whatever it lands on.
    numbered: list[tuple[int, GroupDecision]] = list(
        enumerate(decisions, start=1)
    )

    if args.only or args.skip:
        wanted = set(args.only or set()) | set(args.skip or set())
        known = {n for n, _ in numbered}
        missing = sorted(wanted - known)
        if missing:
            print(f"keeper: cluster(s) {', '.join(map(str, missing))} do not "
                  f"exist — this folder has {len(numbered)}",
                  file=sys.stderr)
            return 2
        skip = args.skip or set()
        numbered = [
            (n, gd) for n, gd in numbered
            if (not args.only or n in args.only) and n not in skip
        ]
        say(f"  Selected {len(numbered)} of {len(known)} cluster(s).\n")

    if args.interactive:
        numbered = _interactive_review(numbered)
        say()

    decisions = [gd for _, gd in numbered]
    numbers = [n for n, _ in numbered]

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
                    "cluster": number,
                    "copies": len(g.files),
                    "keep": g.decision.keep.rel,
                    "reason": g.decision.reason,
                    "confidence": round(g.decision.confidence, 2),
                    "method": g.decision.method,
                    "escalated": g.decision.escalated,
                    "losers": [f.rel for f in g.losers],
                    "loser_bytes": sum(f.size for f in g.losers),
                }
                for number, g in numbered
            ],
        }
    elif not quiet:
        if clusters and not decisions:
            say("Nothing selected — --only/--skip excluded every cluster.")
        else:
            render_decisions(decisions, numbers=numbers)

    if args.report:
        Path(args.report).write_text(
            markdown_report(decisions, str(root), numbers=numbers),
            encoding="utf-8")
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

    return EXIT_CLUTTER if (args.fail_on_clutter and loser_total) else 0


if __name__ == "__main__":
    raise SystemExit(main())
