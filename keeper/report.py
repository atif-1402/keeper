"""Render scan results as terminal text and Markdown."""

from __future__ import annotations

from .quarantine import GroupDecision

METHOD_LABEL = {
    "identical": "byte-identical",
    "model": "model",
    "rule": "fallback rule",
}


def terminal_summary(groups: list[GroupDecision], root: str) -> str:
    if not groups:
        return "No duplicate clusters found. That folder is already tidy."

    lines = [""]
    for i, gd in enumerate(groups, start=1):
        dec = gd.decision
        label = METHOD_LABEL.get(dec.method, dec.method)
        lines.append(f"[{i}] {len(gd.files)} copies of the same thing  (decided by {label})")
        lines.append(f"    KEEP   {gd.decision.keep.rel}")
        lines.append(f"    WHY    {dec.reason}")
        for fact in gd.losers:
            lines.append(f"    loser  {fact.rel}")
        lines.append("")
    return "\n".join(lines)


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
