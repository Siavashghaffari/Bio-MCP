"""Tool definitions. No source logic lives here — see design.md section 1.

Each tool calls exactly one (or, for `gene_evidence`, two in parallel)
`sources/*.py` functions, renders the resulting dict through `trim`, and
returns markdown. Docstrings are the tool descriptions an agent reads when
deciding whether to call the tool, not documentation for a human reading
the source.

## A note on the SDK

MVP.md/design.md specify "mcp SDK, FastMCP". The installed `mcp` package is
2.x, where FastMCP was renamed to `MCPServer` with a slightly different API
(`mcp.server.mcpserver.MCPServer`, not `mcp.server.fastmcp.FastMCP`) — a
mechanical rename, not a behavior change, so this module follows the
current SDK rather than the old class name. Flagged rather than silently
adapted without mention.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.mcpserver import MCPServer

from bio_mcp import __version__, trim
from bio_mcp.errors import SourceError
from bio_mcp.sources import census, orcs

# stdout is the MCP transport; anything written there corrupts the
# protocol (design.md section 7). Logs go to stderr only.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("bio_mcp.server")

server = MCPServer(
    name="bio-mcp",
    # MCPServer defaults version to "", which reports an empty version in the
    # initialize handshake; clients show that as an unversioned server.
    version=__version__,
    website_url="https://github.com/Siavashghaffari/Bio-MCP",
    instructions=(
        "Human-only single-cell expression (CZ CELLxGENE Census) and CRISPR "
        "screen hit calls (BioGRID ORCS). Prefer gene_evidence when the "
        "question is whether a gene matters in a given tissue/cell type — "
        "it answers expression and screen evidence in one call."
    ),
)


def _degraded(source: str, exc: BaseException) -> str:
    message = exc.message if isinstance(exc, SourceError) else str(exc)
    logger.warning("%s failed: %s", source, message)
    return f"_{source} unavailable: {message}_"


# ---------------------------------------------------------------------------
# Census tools
# ---------------------------------------------------------------------------


@server.tool()
async def find_cells(
    tissue: str | None = None,
    cell_type: str | None = None,
    disease: str | None = None,
    assay: str | None = None,
) -> str:
    """Cell counts in the CZ CELLxGENE Census, human data only.

    Filter by any combination of tissue (the coarse grouping, e.g. "lung",
    "blood", "brain"), cell_type, disease (e.g. "normal" for healthy
    tissue), and assay. All arguments are optional; omitted ones are
    aggregated over. Returns cell counts broken down by tissue and cell
    type, plus matching dataset IDs. Use this to discover what cell types
    and how much data exist before calling expression_by_cell_type.
    """
    try:
        result = await census.find_cells(
            tissue=tissue, cell_type=cell_type, disease=disease, assay=assay
        )
    except SourceError as exc:
        return _degraded("census", exc)

    if result["total_cells"] == 0:
        return "No matching cells found in Census for those filters."

    lines = [
        trim.kv(
            {"total_cells": f"{result['total_cells']:,}", "filters": result["filters"]},
        ),
        "",
        trim.table(
            result["rows"],
            [("tissue", "Tissue"), ("cell_type", "Cell type"), ("cell_count", "Cells")],
        ),
    ]
    if result["dataset_ids"]:
        lines += ["", f"Datasets: {', '.join(result['dataset_ids'])}"]
    return trim.clip("\n".join(lines), trim.BUDGETS["find_cells"])


@server.tool()
async def expression_by_cell_type(gene: str, tissue: str) -> str:
    """Mean expression and percent of cells expressing `gene`, by cell type.

    `tissue` is the coarse Census grouping (e.g. "lung", "blood", "brain").
    Values come from a large sample of Census human cells, not the full
    population — the response states how many cells the estimate is based
    on. Cell types not listed either weren't sampled or didn't clear the
    detection threshold; it is not proof the gene is silent there.
    """
    try:
        result = await census.expression_by_cell_type(gene, tissue)
    except SourceError as exc:
        return _degraded("census", exc)

    if not result["rows"]:
        return (
            f"No expression signal for {gene} in {tissue} above the reporting "
            f"threshold (checked {result['n_cell_types_in_tissue']} cell types)."
        )

    lines = [
        f"{gene} in {tissue} — {result['method']}",
        "",
        trim.table(
            result["rows"],
            [
                ("cell_type", "Cell type"),
                ("mean_expression", "Mean expr"),
                ("pct_expressing", "% expressing"),
                ("n_cells_sampled", "Cells sampled"),
            ],
        ),
    ]
    return trim.clip("\n".join(lines), trim.BUDGETS["expression_by_cell_type"])


@server.tool()
async def census_datasets(query: str) -> str:
    """Human Census datasets matching a free-text query.

    Matches against dataset title and collection name. Returns dataset ID,
    title, collection, and total cell count.
    """
    try:
        result = await census.census_datasets(query)
    except SourceError as exc:
        return _degraded("census", exc)

    if not result["rows"]:
        return f"No Census datasets matched '{query}'."

    lines = [
        trim.table(
            result["rows"],
            [
                ("dataset_id", "Dataset ID"),
                ("dataset_title", "Title"),
                ("collection_name", "Collection"),
                ("dataset_total_cell_count", "Cells"),
            ],
        )
    ]
    return trim.clip("\n".join(lines), trim.BUDGETS["census_datasets"])


# ---------------------------------------------------------------------------
# ORCS tools
# ---------------------------------------------------------------------------


@server.tool()
async def crispr_screen_hits(gene: str) -> str:
    """Human CRISPR screens (BioGRID ORCS) that called `gene` a hit.

    Returns cell line, screen type, score, and phenotype for each screen
    where the gene scored as a hit. Requires the ORCS_ACCESS_KEY
    environment variable; if missing, explains how to get a free key.
    """
    try:
        result = await orcs.crispr_screen_hits(gene)
    except SourceError as exc:
        return _degraded("orcs", exc)

    hits = [h for h in result["hits"] if h["hit"]]
    if not hits:
        tested = result["total_screens_tested"]
        return f"No hit calls for {gene} across {tested} human screens tested."

    lines = [
        f"{gene}: hit in {len(hits)} of {result['total_screens_tested']} human screens tested",
        "",
        trim.table(
            hits,
            [
                ("cell_line", "Cell line"),
                ("screen_type", "Screen type"),
                ("score", "Score"),
                ("phenotype", "Phenotype"),
            ],
        ),
    ]
    return trim.clip("\n".join(lines), trim.BUDGETS["crispr_screen_hits"])


@server.tool()
async def screens_in_cell_line(cell_line: str) -> str:
    """Human CRISPR screens (BioGRID ORCS) run in `cell_line`.

    Returns screen ID, type, and title for every screen using that cell
    line. Requires ORCS_ACCESS_KEY; if missing, explains how to get a free
    key.
    """
    try:
        result = await orcs.screens_in_cell_line(cell_line)
    except SourceError as exc:
        return _degraded("orcs", exc)

    if not result["screens"]:
        return f"No screens found for cell line '{cell_line}'."

    lines = [
        trim.table(
            result["screens"],
            [("screen_id", "Screen ID"), ("screen_type", "Type"), ("title", "Title")],
        )
    ]
    return trim.clip("\n".join(lines), trim.BUDGETS["screens_in_cell_line"])


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


@server.tool()
async def gene_evidence(gene: str, tissue: str) -> str:
    """Is `gene` expressed in `tissue`, and does knocking it out do anything?

    The reason bio-mcp exists: one call joining CZ CELLxGENE Census
    (expression by cell type) and BioGRID ORCS (CRISPR screen hit calls),
    run in parallel. If one source fails, the other's half is still
    returned with a line naming what failed — never a hard error. Prefer
    this over calling expression_by_cell_type and crispr_screen_hits
    separately.
    """
    census_result, orcs_result = await asyncio.gather(
        census.expression_by_cell_type(gene, tissue),
        orcs.crispr_screen_hits(gene),
        return_exceptions=True,
    )

    lines = [f"# {gene} in {tissue}", ""]

    lines.append("## Expression")
    if isinstance(census_result, BaseException):
        lines.append(_degraded("census", census_result))
    elif not census_result["rows"]:
        lines.append(
            f"No expression signal above the reporting threshold "
            f"(checked {census_result['n_cell_types_in_tissue']} cell types)."
        )
    else:
        lines.append(f"_{census_result['method']}_")
        lines.append("")
        lines.append(
            trim.table(
                census_result["rows"],
                [
                    ("cell_type", "Cell type"),
                    ("mean_expression", "Mean expr"),
                    ("pct_expressing", "% expressing"),
                ],
                max_rows=10,
            )
        )

    lines.append("")
    lines.append("## CRISPR screen hits")
    if isinstance(orcs_result, BaseException):
        lines.append(_degraded("orcs", orcs_result))
    else:
        hits = [h for h in orcs_result["hits"] if h["hit"]]
        if not hits:
            lines.append(
                f"No hit calls across {orcs_result['total_screens_tested']} human screens tested."
            )
        else:
            lines.append(
                f"Hit in {len(hits)} of {orcs_result['total_screens_tested']} human screens tested."
            )
            lines.append("")
            lines.append(
                trim.table(
                    hits,
                    [
                        ("cell_line", "Cell line"),
                        ("screen_type", "Screen type"),
                        ("phenotype", "Phenotype"),
                    ],
                    max_rows=10,
                )
            )

    return trim.clip("\n".join(lines), trim.BUDGETS["gene_evidence"])


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
