"""Live smoke test: hit every tool once, print size and timing.

Not part of CI's required checks — public APIs have bad days and that
should not redden the repo (design.md section 6). Run by hand:

    python -m bio_mcp.selftest

Needs the Census precomputed tables already built (or downloadable — see
sources/census.py) and, for the ORCS tools, ORCS_ACCESS_KEY set.
"""

from __future__ import annotations

import asyncio
import time

from bio_mcp import server, trim


async def _run_one(label: str, coro) -> None:
    t0 = time.time()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 - a selftest reports, never crashes
        print(f"{label}: FAILED ({time.time() - t0:.2f}s) — {exc}")
        return
    chars = len(result)
    tokens = trim.estimate_tokens(result)
    over = " OVER BUDGET" if tokens > trim.BUDGETS["max_tokens_per_tool"] else ""
    print(f"{label}: {time.time() - t0:.2f}s, {chars} chars, ~{tokens} tokens{over}")
    print("-" * 40)
    print(result)
    print("=" * 40)


async def main_async() -> None:
    await _run_one("find_cells(tissue='lung')", server.find_cells(tissue="lung"))
    await _run_one(
        "expression_by_cell_type('MYC', 'lung')",
        server.expression_by_cell_type("MYC", "lung"),
    )
    await _run_one("census_datasets('lung')", server.census_datasets("lung"))
    await _run_one("crispr_screen_hits('MYC')", server.crispr_screen_hits("MYC"))
    await _run_one("screens_in_cell_line('K562')", server.screens_in_cell_line("K562"))
    await _run_one("gene_evidence('MYC', 'lung')", server.gene_evidence("MYC", "lung"))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
