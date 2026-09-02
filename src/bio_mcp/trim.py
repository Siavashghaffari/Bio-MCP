"""Token budgets and markdown rendering.

Every limit in the project lives here (design.md section 3, "Token budgets
live in one module"). Nothing outside this file should hardcode a row count
or a character limit. Truncation is always visible: a clipped string ends
with a marker, a shortened table says how many rows were hidden. Never
silently drop data — see design.md section 3, "Truncation is always visible".

Token counts are estimated, not exact — bio-mcp has no tokenizer dependency.
The ~4-chars-per-token heuristic is the standard rule of thumb for English
text and is deliberately conservative-adjacent: good enough to keep answers
well clear of the 1,000-token ceiling, not a precise count.
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4

# Every budget in the project. See design.md section 3.
BUDGETS = {
    # hard ceiling from scope.md acceptance criteria #3
    "max_tokens_per_tool": 1000,
    # default markdown table size before rows are hidden
    "table_max_rows": 20,
    # default clip length for free-text fields (abstracts, descriptions)
    "field_clip_tokens": 60,
    # per-tool response budgets, kept comfortably under the 1,000 ceiling
    "find_cells": 400,
    "expression_by_cell_type": 500,
    "census_datasets": 400,
    "crispr_screen_hits": 500,
    "screens_in_cell_line": 400,
    "gene_evidence": 900,
}


def estimate_tokens(text: str) -> int:
    """Rough token count for a rendered string. See module docstring."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def clip(text: str, tokens: int) -> str:
    """Truncate `text` to approximately `tokens` tokens on a word boundary.

    Always marks a cut with a trailing "…" so truncation is visible instead
    of silently dropped (design.md section 3).
    """
    if not text:
        return ""
    char_budget = tokens * CHARS_PER_TOKEN
    if len(text) <= char_budget:
        return text
    cut = text[:char_budget].rsplit(" ", 1)[0].rstrip()
    if not cut:
        cut = text[:char_budget].rstrip()
    return f"{cut}…"


def table(
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
    max_rows: int = BUDGETS["table_max_rows"],
) -> str:
    """Render `rows` as a GitHub-flavored markdown table.

    `columns` is a list of (field, header) pairs, in display order. Missing
    fields render as an empty cell. When there are more than `max_rows`
    rows, only the first `max_rows` are shown and a line beneath the table
    states how many were hidden — never a silent truncation.
    """
    if not rows:
        return "_no results_"

    header = "| " + " | ".join(h for _, h in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    shown = rows[:max_rows]
    body_lines = []
    for row in shown:
        cells = []
        for field, _ in columns:
            value = row.get(field, "")
            cell = "" if value is None else str(value)
            cell = cell.replace("|", "\\|").replace("\n", " ")
            cells.append(cell)
        body_lines.append("| " + " | ".join(cells) + " |")

    lines = [header, sep, *body_lines]
    hidden = len(rows) - len(shown)
    if hidden > 0:
        lines.append(f"\n_({hidden} more row{'s' if hidden != 1 else ''} not shown)_")
    return "\n".join(lines)


def kv(data: dict[str, Any], labels: dict[str, str] | None = None) -> str:
    """Render a dict as a markdown key/value block, dropping empty values.

    `labels` optionally maps a field name to a display label; fields not in
    `labels` use their key verbatim. Order follows `data`'s insertion order.
    """
    labels = labels or {}
    lines = []
    for key, value in data.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        label = labels.get(key, key)
        lines.append(f"- **{label}:** {value}")
    return "\n".join(lines) if lines else "_no data_"
