"""Build the sampled expression cube `expression_by_cell_type` queries.

## Why sampled, not exact

Verified live against cellxgene-census 1.18.0 / census version "2025-11-08"
(158,982,719 human cells, 61,497 human genes):

- Metadata-only reads are fast (see `build_cell_counts.py`), but reading the
  X expression matrix is not: 50,000 contiguous cells x all genes took
  40.7s. Extrapolated to the full corpus, that is 36+ hours and roughly
  1.5TB pulled from S3 — not a "one-time offline job" in any practical
  sense.
- The obvious fix, sampling individual cells at random, is *worse*, not
  better: TileDB-SOMA's tile locality makes scattered reads pay a huge
  per-request penalty. 2,000 scattered cells took longer than the 50,000
  contiguous cells above (>120s, killed before finishing).

So this job reads a bounded number of large *contiguous* blocks, spread
evenly across the full coordinate range, and computes mean expression and
percent-expressing per (gene, tissue_general, cell_type) from that sample.
This is a labeled estimate, not an exact population statistic — every
answer `sources/census.py` renders says so.

## Method

1. Load `census_cell_counts.parquet` (built by `build_cell_counts.py`) to
   get every (tissue_general, cell_type) combination that actually occurs
   in human data — 4,744 of them, verified live.
2. Read `N_BLOCKS` contiguous blocks of `BLOCK_SIZE` cells each, evenly
   spaced across all 158,982,719 human cells, restricted to primary data.
3. Accumulate running sum-of-expression and count-of-expressing-cells per
   (stratum, gene) directly into preallocated arrays sized
   `n_strata x n_genes` (~1.2GB each at float32) via `np.add.at`, so a
   block's sparse matrix is processed and discarded — memory stays bounded
   regardless of how many blocks are read.
4. Drop (stratum, gene) pairs below `MIN_PCT_EXPRESSING` and strata sampled
   below `MIN_CELLS_PER_STRATUM`: both a legitimate noise filter (a gene
   hit in 1 of 900 sampled cells is not a meaningful tissue-level signal)
   and what keeps the shipped table compact (design.md section 3).

Run with: `python -m bio_mcp.precompute.build_expression_cube`
Requires the `precompute` extra (see precompute/__init__.py) and
`census_cell_counts.parquet` already built.
"""

from __future__ import annotations

import gc
import sys
import time

import numpy as np
import pandas as pd

from bio_mcp.precompute.common import (
    CACHE_DIR,
    CELL_COUNTS_FILENAME,
    CELL_TYPE_COLUMN,
    CENSUS_VERSION,
    EXPRESSION_CUBE_FILENAME,
    ORGANISM,
    TISSUE_COLUMN,
)

TOTAL_HUMAN_CELLS = 158_982_719  # verified live; see module docstring
BLOCK_SIZE = 50_000
N_BLOCKS = 140  # ~7,000,000 raw cells sampled; ~95 min at the measured rate

# Noise/compactness filters applied when the cube is written (see module
# docstring point 4).
MIN_CELLS_PER_STRATUM = 20
MIN_PCT_EXPRESSING = 5.0


def _block_starts(total: int, n_blocks: int, block_size: int) -> list[int]:
    usable = max(total - block_size, 0)
    spacing = usable // max(n_blocks - 1, 1)
    return [min(i * spacing, usable) for i in range(n_blocks)]


def build() -> None:
    import cellxgene_census

    cell_counts_path = CACHE_DIR / CELL_COUNTS_FILENAME
    if not cell_counts_path.exists():
        raise SystemExit(
            f"{cell_counts_path} not found — run "
            "`python -m bio_mcp.precompute.build_cell_counts` first."
        )

    strata = (
        pd.read_parquet(cell_counts_path, columns=[TISSUE_COLUMN, CELL_TYPE_COLUMN])
        .drop_duplicates()
        .reset_index(drop=True)
    )
    n_strata = len(strata)
    stratum_index: dict[tuple[str, str], int] = {
        (row[TISSUE_COLUMN], row[CELL_TYPE_COLUMN]): i for i, row in strata.iterrows()
    }
    print(f"{n_strata:,} (tissue_general, cell_type) strata", file=sys.stderr)

    t_start = time.time()
    census = cellxgene_census.open_soma(census_version=CENSUS_VERSION)

    gene_names: list[str] | None = None
    n_genes = 0
    sum_expr: np.ndarray | None = None
    n_expressing: np.ndarray | None = None
    n_cells_sampled = np.zeros(n_strata, dtype=np.int64)

    starts = _block_starts(TOTAL_HUMAN_CELLS, N_BLOCKS, BLOCK_SIZE)
    for i, start in enumerate(starts, start=1):
        end = min(start + BLOCK_SIZE, TOTAL_HUMAN_CELLS)
        t0 = time.time()
        adata = cellxgene_census.get_anndata(
            census,
            organism=ORGANISM,
            obs_coords=slice(start, end - 1),
            obs_column_names=[TISSUE_COLUMN, CELL_TYPE_COLUMN, "is_primary_data"],
            var_column_names=["feature_name"],
        )

        if gene_names is None:
            gene_names = adata.var["feature_name"].tolist()
            n_genes = len(gene_names)
            sum_expr = np.zeros((n_strata, n_genes), dtype=np.float32)
            n_expressing = np.zeros((n_strata, n_genes), dtype=np.float32)
            print(f"{n_genes:,} genes (fixed column order from first block)", file=sys.stderr)

        adata = adata[adata.obs["is_primary_data"].to_numpy()]
        if adata.n_obs == 0:
            print(f"  block {i}/{len(starts)}: 0 primary cells, skipped", file=sys.stderr)
            continue

        obs_keys = list(
            zip(
                adata.obs[TISSUE_COLUMN].astype(str),
                adata.obs[CELL_TYPE_COLUMN].astype(str),
                strict=True,
            )
        )
        row_stratum = np.array(
            [stratum_index.get(k, -1) for k in obs_keys],
            dtype=np.int64,
        )
        keep = row_stratum >= 0
        if not keep.all():
            adata = adata[keep]
            row_stratum = row_stratum[keep]
        if adata.n_obs == 0:
            print(f"  block {i}/{len(starts)}: no rows matched a known stratum", file=sys.stderr)
            continue

        coo = adata.X.tocoo()
        block_strata = row_stratum[coo.row]
        flat_idx = block_strata * n_genes + coo.col
        np.add.at(sum_expr.reshape(-1), flat_idx, coo.data)
        np.add.at(n_expressing.reshape(-1), flat_idx, 1)
        n_cells_sampled += np.bincount(row_stratum, minlength=n_strata)

        print(
            f"  block {i}/{len(starts)}: {adata.n_obs:,} primary cells, "
            f"nnz={coo.nnz:,} ({time.time() - t0:.1f}s, "
            f"{time.time() - t_start:.0f}s elapsed)",
            file=sys.stderr,
        )

        # Each block's AnnData carries its own sparse X plus TileDB read
        # buffers; explicit cleanup keeps peak memory bounded across
        # hundreds of blocks instead of relying on GC timing (measured: the
        # WSL2 VM OOM-killed the process without this).
        del adata, coo, block_strata, flat_idx, row_stratum, obs_keys
        gc.collect()

    census.close()
    assert sum_expr is not None and n_expressing is not None and gene_names is not None

    print("aggregating and filtering...", file=sys.stderr)
    valid_strata = np.where(n_cells_sampled >= MIN_CELLS_PER_STRATUM)[0]
    denom = np.where(n_cells_sampled == 0, 1, n_cells_sampled).astype(np.float32)
    mean_expr = sum_expr / denom[:, None]
    pct_expr = n_expressing / denom[:, None] * 100.0

    mask = np.zeros_like(pct_expr, dtype=bool)
    mask[valid_strata, :] = pct_expr[valid_strata, :] >= MIN_PCT_EXPRESSING
    stratum_idx, gene_idx = np.nonzero(mask)
    print(f"{len(stratum_idx):,} (stratum, gene) rows above threshold", file=sys.stderr)

    gene_arr = np.asarray(gene_names)
    out = pd.DataFrame(
        {
            TISSUE_COLUMN: strata[TISSUE_COLUMN].to_numpy()[stratum_idx],
            CELL_TYPE_COLUMN: strata[CELL_TYPE_COLUMN].to_numpy()[stratum_idx],
            "feature_name": gene_arr[gene_idx],
            "mean_expression": mean_expr[stratum_idx, gene_idx].astype("float32"),
            "pct_expressing": pct_expr[stratum_idx, gene_idx].astype("float32"),
            "n_cells_sampled": n_cells_sampled[stratum_idx].astype("int32"),
        }
    )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / EXPRESSION_CUBE_FILENAME
    out.to_parquet(out_path, index=False)
    print(
        f"wrote {out_path} ({len(out):,} rows, {out_path.stat().st_size / 1e6:.1f} MB)",
        file=sys.stderr,
    )
    print(f"done in {time.time() - t_start:.1f}s total", file=sys.stderr)


if __name__ == "__main__":
    build()
