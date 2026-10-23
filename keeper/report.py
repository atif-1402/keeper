"""Render scan results: a colored terminal table (rich) and Markdown files."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from .quarantine import GroupDecision

METHOD_BADGE = {
    "model": "[bold green]● model[/]",
    "rule": "[bold yellow]◐ rule[/]",
    "identical": "[bold blue]◆ identical[/]",
    "manual": "[bold magenta]◆ you[/]",
}

METHOD_LABEL = {
    "identical": "byte-identical",
    "model": "model",
    "rule": "fallback rule",
    "manual": "your pick",
}


def fmt_bytes(n: int) -> str:
    """Human byte count: 912 B, 4.2 KB, 1.3 MB, 2.3 GB."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

# The house style: light outer frame, heavy header rule (╞═╪╡), solid grid.
KEEPER_BOX = box.Box("""\
┌─┬┐
│ ││
╞═╪╡
│ ││
├─┼┤
├─┼┤
│ ││
└─┴┘
""")


def render_decisions(groups: list[GroupDecision], *,
                     numbers: Sequence[int] | None = None) -> None:
    """Print the verdict table: colored, one row per cluster.

    ``numbers`` overrides the displayed cluster numbers — used when the user
    filtered with ``--only``/``--skip`` so cluster 7 still reads as 7 instead
    of being renumbered to whatever slot it landed in.

    Column widths are proportional to the terminal, and every text column
    *wraps* — nothing is ever cut off with an ellipsis, no matter how small
    the window is.
    """
    # On a real terminal, follow its width; piped/captured output gets a wide
    # fixed width so tables stay readable in demos and docs.
    console = Console()
    if not console.is_terminal:
        console = Console(width=180)
    if not groups:
        console.print("No duplicate clusters found. That folder is already tidy.")
        return
    labels = list(numbers) if numbers is not None else list(range(1, len(groups) + 1))

    # Width budget: # / Copies / Decided are fixed (~19 cols), overhead for
    # padding and borders ~24; the remaining pool splits KEEP 40% / Why 32% /
    # Losers the rest — so the table always fits the terminal exactly.
    width = console.width or 180
    avail = max(40, width - 19 - 24)
    if avail >= 72:
        keep_w = max(20, int(avail * 0.40))
        why_w = max(26, int(avail * 0.32))
        losers_w = max(20, avail - keep_w - why_w)
    else:  # very narrow terminal: split what exists, no minimums
        keep_w = int(avail * 0.42)
        why_w = int(avail * 0.32)
        losers_w = avail - keep_w - why_w

    table = Table(
        box=KEEPER_BOX, header_style="bold bright_cyan", show_edge=True,
        show_lines=True, pad_edge=False,
    )
    table.add_column("#", style="dim", width=2, no_wrap=True, justify="right")
    table.add_column("Copies", justify="right", width=6, no_wrap=True)
    table.add_column("KEEP", style="bold green", width=keep_w, overflow="fold")
    table.add_column("Decided", width=11, no_wrap=True)
    table.add_column("Why", style="dim", width=why_w, overflow="fold")
    table.add_column("Losers", style="red", width=losers_w, overflow="fold")

    for label, gd in zip(labels, groups):
        dec = gd.decision
        loser_names = [Path(f.rel).name for f in gd.losers]
        # As many loser names as comfortably fit; the full list lives in the
        # Markdown report.
        loser_cell = loser_names[0]
        for take in (1, 2, 3):
            if take > len(loser_names):
                break
            cell = ", ".join(loser_names[:take])
            if len(loser_names) > take:
                cell += f" +{len(loser_names) - take} more"
            if len(cell) <= losers_w:
                loser_cell = cell
        table.add_row(
            str(label),
            str(len(gd.files)),
            Path(dec.keep.rel).name,
            METHOD_BADGE.get(dec.method, dec.method),
            dec.reason,
            loser_cell,
        )
    console.print(table)


def markdown_report(groups: list[GroupDecision], root: str, *,
                    numbers: Sequence[int] | None = None) -> str:
    clutter = sum(f.size for g in groups for f in g.losers)
    labels = list(numbers) if numbers is not None else list(range(1, len(groups) + 1))
    lines = [
        "# Keeper report",
        "",
        f"Folder: `{root}`",
        "",
        f"Found **{len(groups)}** duplicate cluster(s) · "
        f"**{fmt_bytes(clutter)}** of duplicate clutter identified.",
        "",
    ]
    if not groups:
        lines.append("Nothing to do — no duplicate clusters found.")
        return "\n".join(lines) + "\n"

    lines += ["| # | Copies | Keeper | Decided by | Why |", "|---|--------|--------|------------|-----|"]
    for label, gd in zip(labels, groups):
        dec = gd.decision
        label_text = METHOD_LABEL.get(dec.method, dec.method)
        reason = dec.reason.replace("|", "\\|")
        lines.append(
            f"| {label} | {len(gd.files)} | `{gd.decision.keep.rel}` | {label_text} | {reason} |"
        )

    lines += ["", "## Losers", ""]
    for label, gd in zip(labels, groups):
        lines.append(f"**Cluster {label}** — keeper: `{gd.decision.keep.rel}`")
        lines.append("")
        for fact in gd.losers:
            lines.append(f"- `{fact.rel}`")
        lines.append("")
    return "\n".join(lines)
