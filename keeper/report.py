"""Render scan results: a colored terminal table (rich) and Markdown files."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from .quarantine import GroupDecision

METHOD_BADGE = {
    "model": "[bold green]model[/]",
    "rule": "[bold yellow]rule[/]",
    "identical": "[bold blue]identical[/]",
}

METHOD_LABEL = {
    "identical": "byte-identical",
    "model": "model",
    "rule": "fallback rule",
}


def render_decisions(groups: list[GroupDecision]) -> None:
    """Print the verdict table: one row per cluster, losers compacted."""
    # On a real terminal, follow its width; piped/captured output gets a wide
    # fixed width so tables stay readable in demos and docs.
    console = Console()
    if not console.is_terminal:
        console = Console(width=140)
    if not groups:
        console.print("No duplicate clusters found. That folder is already tidy.")
        return

    table = Table(
        box=box.SIMPLE, header_style="bold", show_edge=False,
        show_lines=False, pad_edge=False,
    )
    table.add_column("#", style="dim", width=2, no_wrap=True, justify="right")
    table.add_column("Copies", justify="right", width=5, no_wrap=True)
    table.add_column("KEEP", style="green", overflow="fold")
    table.add_column("Decided", width=9, no_wrap=True)
    table.add_column("Why", style="dim", overflow="fold", max_width=40)
    table.add_column("Losers", style="red", overflow="fold")

    for i, gd in enumerate(groups, start=1):
        dec = gd.decision
        loser_names = [Path(f.rel).name for f in gd.losers]
        if len(loser_names) > 3:
            loser_cell = ", ".join(loser_names[:3]) + f" +{len(loser_names) - 3} more"
        else:
            loser_cell = ", ".join(loser_names)
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
