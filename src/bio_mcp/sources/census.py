"""CZ CELLxGENE Census — served from precomputed tables, not live queries.

## Why this module never imports cellxgene_census

Naive live TileDB-SOMA queries were measured (see precompute/__init__.py)
at 8 seconds to 36+ hours against a 3-second budget — the exact failure
design.md section 5 anticipated. `find_cells`, `expression_by_cell_type`,
and `census_datasets` all read parquet tables built offline by
`bio_mcp.precompute.build_cell_counts` and `.build_expression_cube`
instead. This also means the *server* never depends on `tiledbsoma`, which
ships no Windows wheels — only the precompute jobs do (the `precompute`
extra in pyproject.toml).

`find_cells` and `census_datasets` come from an exact full scan of Census
metadata and are precise. `expression_by_cell_type` comes from a sampled
scan of the expression matrix (contiguous blocks, not the full ~159M-cell
corpus — full-population reads of X were the ~36h+ case above) and is a
statistical estimate; every result says so and reports how many cells the
estimate is based on.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pandas as pd

from bio_mcp.errors import SourceError
from bio_mcp.precompute.common import (
    CACHE_DIR,
    CELL_COUNTS_FILENAME,
    CELL_TYPE_COLUMN,
    DATASETS_FILENAME,
    EXPRESSION_CUBE_FILENAME,
    TISSUE_COLUMN,
)

# Precomputed tables are built locally by the jobs in `bio_mcp.precompute`.
# Set BIO_MCP_CUBE_BASE_URL to a host serving them (a release page, an S3
# bucket) and the server will fetch them into CACHE_DIR on first use instead.
# There is no default host: pointing at one that does not serve the files
# would make every Census call pay for a doomed request before reporting the
# same thing the message below says immediately.
CUBE_BASE_URL_ENV_VAR = "BIO_MCP_CUBE_BASE_URL"

BUILD_TABLES_HELP = (
    "Build it with `pip install -e \".[precompute]\"` then "
    "`python -m bio_mcp.precompute.build_cell_counts` and "
    "`python -m bio_mcp.precompute.build_expression_cube`, or set "
    f"{CUBE_BASE_URL_ENV_VAR} to a host serving the precomputed tables."
)

_TABLES: dict[str, pd.DataFrame] = {}


def _cube_base_url() -> str | None:
    return os.environ.get(CUBE_BASE_URL_ENV_VAR) or None


async def _ensure_downloaded(filename: str) -> Path:
    path = CACHE_DIR / filename
    if path.exists():
        return path

    base_url = _cube_base_url()
    if base_url is None:
        raise SourceError("census", f"{filename} is not in {CACHE_DIR}. {BUILD_TABLES_HELP}")

    url = f"{base_url.rstrip('/')}/{filename}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(
            "census",
            f"{filename} is not in {CACHE_DIR} and could not be downloaded from "
            f"{url} ({exc}). {BUILD_TABLES_HELP}",
        ) from exc

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path


async def _load_table(filename: str) -> pd.DataFrame:
    if filename not in _TABLES:
        path = await _ensure_downloaded(filename)
        try:
            _TABLES[filename] = pd.read_parquet(path)
        except Exception as exc:
            raise SourceError("census", f"{filename} is corrupt: {exc}") from exc
    return _TABLES[filename]


def _reset_cache_for_tests() -> None:
    """Test-only: drop the in-memory table cache so a test's own fixture wins."""
    _TABLES.clear()


MAX_DATASET_IDS_RETURNED = 10


async def find_cells(
    tissue: str | None = None,
    cell_type: str | None = None,
    disease: str | None = None,
    assay: str | None = None,
) -> dict:
    """Cell counts by (tissue, cell_type), from an exact scan of Census metadata.

    `tissue` matches the Census `tissue_general` field (the coarse grouping,
    e.g. "lung" — see precompute/common.py for why). All arguments are
    optional filters; omitted ones are aggregated over.
    """
    df = await _load_table(CELL_COUNTS_FILENAME)
    mask = pd.Series(True, index=df.index)
    if tissue is not None:
        mask &= df[TISSUE_COLUMN].str.lower() == tissue.lower()
    if cell_type is not None:
        mask &= df[CELL_TYPE_COLUMN].str.lower() == cell_type.lower()
    if disease is not None:
        mask &= df["disease"].str.lower() == disease.lower()
    if assay is not None:
        mask &= df["assay"].str.lower() == assay.lower()

    matched = df[mask]
    filters = {"tissue": tissue, "cell_type": cell_type, "disease": disease, "assay": assay}
    if matched.empty:
        return {"filters": filters, "total_cells": 0, "rows": [], "dataset_ids": []}

    grouped = (
        matched.groupby([TISSUE_COLUMN, CELL_TYPE_COLUMN], observed=True)["cell_count"]
        .sum()
        .reset_index()
        .sort_values("cell_count", ascending=False)
    )
    dataset_ids: set[str] = set()
    for ids in matched["dataset_ids"]:
        dataset_ids.update(ids)

    return {
        "filters": filters,
        "total_cells": int(matched["cell_count"].sum()),
        "rows": grouped.rename(columns={TISSUE_COLUMN: "tissue"}).to_dict(orient="records"),
        "dataset_ids": sorted(dataset_ids)[:MAX_DATASET_IDS_RETURNED],
    }


async def expression_by_cell_type(gene: str, tissue: str) -> dict:
    """Mean expression and percent expressing per cell type, for `gene` in `tissue`.

    From the sampled expression cube (see module docstring): a statistical
    estimate over a large contiguous sample of Census cells, not the full
    population. Cell types where `gene` didn't clear the cube's detection
    threshold (5% of sampled cells, see build_expression_cube.py) aren't
    necessarily silent — the summary is explicit about which cell types
    were checked.
    """
    df = await _load_table(EXPRESSION_CUBE_FILENAME)
    mask = (df[TISSUE_COLUMN].str.lower() == tissue.lower()) & (
        df["feature_name"].str.lower() == gene.lower()
    )
    matched = df[mask].sort_values("mean_expression", ascending=False)

    checked_cell_types = await find_cells(tissue=tissue)
    n_cell_types_in_tissue = len(checked_cell_types["rows"])

    return {
        "gene": gene,
        "tissue": tissue,
        "rows": matched.rename(columns={TISSUE_COLUMN: "tissue"}).to_dict(orient="records"),
        "n_cell_types_with_signal": len(matched),
        "n_cell_types_in_tissue": n_cell_types_in_tissue,
        "method": "sampled estimate (contiguous block sample of Census, not full population)",
    }


async def census_datasets(query: str) -> dict:
    """Human Census datasets whose title or collection name matches `query`."""
    df = await _load_table(DATASETS_FILENAME)
    needle = query.lower()
    mask = df["dataset_title"].str.lower().str.contains(needle, na=False) | df[
        "collection_name"
    ].str.lower().str.contains(needle, na=False)
    matched = df[mask].sort_values("dataset_total_cell_count", ascending=False)

    rows = matched[
        ["dataset_id", "dataset_title", "collection_name", "dataset_total_cell_count"]
    ].to_dict(orient="records")
    return {"query": query, "rows": rows, "total_matches": len(rows)}
