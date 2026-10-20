"""Render scan results: a colored terminal table (rich) and Markdown files."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from .quarantine import GroupDecision

METHOD_BADGE = {
    "model": "[bold green]● model[/]",
    "rule": "[bold yellow]◐ rule[/]",
    "identical": "[bold blue]◆ identical[/]",
}

METHOD_LABEL = {
    "identical": "byte-identical",
    "model": "model",
    "rule": "fallback rule",
}


def render_decisions(groups: list[GroupDecision]) -> None:
    """Print the verdict table: colored, one row per cluster.

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

    # Width budget: ~19 cols of fixed columns + ~18 of padding/borders; the
    # rest splits KEEP 34% / Why 30% / Losers gets the remainder.
    width = console.width or 180
    keep_w = max(24, int(width * 0.34))
    why_w = max(30, int(width * 0.30))
    losers_w = max(24, width - (2 + 6 + 11 + keep_w + why_w + 18))

    table = Table(
        box=box.ROUNDED, header_style="bold bright_cyan", show_edge=False,
        show_lines=False, pad_edge=False,
    )
    table.add_column("#", style="dim", width=2, no_wrap=True, justify="right")
    table.add_column("Copies", justify="right", width=6, no_wrap=True)
    table.add_column("KEEP", style="bold green", width=keep_w, overflow="fold")
    table.add_column("Decided", width=11, no_wrap=True)
    table.add_column("Why", style="dim", width=why_w, overflow="fold")
    table.add_column("Losers", style="red", width=losers_w, overflow="fold")

    for i, gd in enumerate(groups, start=1):
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
            str(i),
            str(len(gd.files)),
            Path(dec.keep.rel).name,
            METHOD_BADGE.get(dec.method, dec.method),
            dec.reason,
            loser_cell,
        )
    console.print(table)


def markdown_report(groups: list[GroupDecision], root: str) -> str:
    lines = [
        "# Keeper report",
        "",
        f"Folder: `{root}`",
        "",
        f"Found **{len(groups)}** duplicate cluster(s).",
        "",
    ]
    if not groups:
        lines.append("Nothing to do — no duplicate clusters found.")
        return "\n".join(lines) + "\n"

    lines += ["| # | Copies | Keeper | Decided by | Why |", "|---|--------|--------|------------|-----|"]
    for i, gd in enumerate(groups, start=1):
        dec = gd.decision
        label = METHOD_LABEL.get(dec.method, dec.method)
        reason = dec.reason.replace("|", "\\|")
        lines.append(
            f"| {i} | {len(gd.files)} | `{gd.decision.keep.rel}` | {label} | {reason} |"
        )

    lines += ["", "## Losers", ""]
    for i, gd in enumerate(groups, start=1):
        lines.append(f"**Cluster {i}** — keeper: `{gd.decision.keep.rel}`")
        lines.append("")
        for fact in gd.losers:
            lines.append(f"- `{fact.rel}`")
        lines.append("")
    return "\n".join(lines)
