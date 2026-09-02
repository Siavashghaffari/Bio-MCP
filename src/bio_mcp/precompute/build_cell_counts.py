"""Build the exact cell-count and dataset tables `find_cells` and
`census_datasets` query at runtime.

Unlike the expression cube (`build_expression_cube.py`), this needs only
obs/dataset *metadata*, never the X expression matrix, so a full scan of
every human cell is fast and exact — no sampling. Verified live: reading
500,000 cells' metadata via a coordinate slice took 0.77s, so the full
158,982,719-cell human corpus scans in a few minutes.

Run with: `python -m bio_mcp.precompute.build_cell_counts`
Requires the `precompute` extra: `pip install -e ".[precompute]"`
(needs cellxgene-census, which has no Windows wheels — run this under
Linux/macOS or WSL. See precompute/__init__.py.)
"""

from __future__ import annotations

import sys
import time

import pandas as pd

from bio_mcp.precompute.common import (
    CACHE_DIR,
    CELL_COUNTS_FILENAME,
    CELL_TYPE_COLUMN,
    CENSUS_VERSION,
    DATASETS_FILENAME,
    ORGANISM,
    TISSUE_COLUMN,
)

# obs columns pulled for every cell. is_primary_data excludes cells that are
# duplicated across integrated datasets, per Census's own recommendation for
# any cell-counting or aggregate statistic.
OBS_COLUMNS = [
    TISSUE_COLUMN,
    "tissue",
    CELL_TYPE_COLUMN,
    "disease",
    "assay",
    "dataset_id",
    "is_primary_data",
]

# Chunk size for the metadata-only scan. Coordinate-based (contiguous)
# reads are fast for metadata; this is sized to keep each chunk's memory
# footprint small while keeping the chunk count (and per-call overhead) low.
CHUNK_SIZE = 5_000_000

GROUP_COLUMNS = [TISSUE_COLUMN, "tissue", CELL_TYPE_COLUMN, "disease", "assay"]

# Cap on how many dataset IDs we keep per group row, so one very common
# combination can't make the table unbounded. Rendering-time trimming still
# happens in trim.py; this is just a sanity bound on the source table.
MAX_DATASET_IDS_PER_GROUP = 25


def _fetch_chunk(census, start: int, end: int) -> pd.DataFrame:
    import cellxgene_census

    return cellxgene_census.get_obs(
        census,
        ORGANISM,
        column_names=OBS_COLUMNS,
        coords=slice(start, end - 1),
    )


def build() -> None:
    import cellxgene_census

    t_start = time.time()
    print(f"opening census {CENSUS_VERSION}...", file=sys.stderr)
    census = cellxgene_census.open_soma(census_version=CENSUS_VERSION)
    human = census["census_data"][ORGANISM]
    total = human.obs.count
    print(f"human obs count: {total:,}", file=sys.stderr)

    # Running accumulator: one row per distinct combination of
    # GROUP_COLUMNS seen so far, with a running cell count and a running
    # set of dataset IDs. Merged chunk-by-chunk rather than holding all
    # 159M raw rows in memory at once.
    accumulator: pd.DataFrame | None = None
    seen_dataset_ids: set[str] = set()

    n_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    for i, start in enumerate(range(0, total, CHUNK_SIZE), start=1):
        end = min(start + CHUNK_SIZE, total)
        t0 = time.time()
        chunk = _fetch_chunk(census, start, end)
        chunk = chunk[chunk["is_primary_data"]]
        seen_dataset_ids.update(chunk["dataset_id"].unique().tolist())

        partial = (
            chunk.groupby(GROUP_COLUMNS, observed=True)
            .size()
            .rename("cell_count")
            .reset_index()
        )
        # dataset_ids per group, for "matching dataset IDs" in find_cells.
        ds_ids = (
            chunk.groupby(GROUP_COLUMNS, observed=True)["dataset_id"]
            .agg(lambda s: set(s.unique()))
            .rename("dataset_ids")
            .reset_index()
        )
        partial = partial.merge(ds_ids, on=GROUP_COLUMNS, how="left")

        if accumulator is None:
            accumulator = partial
        else:
            merged = accumulator.merge(
                partial, on=GROUP_COLUMNS, how="outer", suffixes=("_old", "_new")
            )
            merged["cell_count"] = merged["cell_count_old"].fillna(0) + merged[
                "cell_count_new"
            ].fillna(0)
            def _as_set(v: object) -> set:
                # An outer merge leaves a float NaN (not None) on the side
                # that had no match — and NaN is truthy in Python, so a
                # plain `v or set()` returns the NaN itself, not the
                # fallback. Check explicitly instead.
                return v if isinstance(v, set) else set()

            merged["dataset_ids"] = merged.apply(
                lambda r: _as_set(r["dataset_ids_old"]) | _as_set(r["dataset_ids_new"]),
                axis=1,
            )
            accumulator = merged[[*GROUP_COLUMNS, "cell_count", "dataset_ids"]].copy()

        print(
            f"  chunk {i}/{n_chunks} rows={len(chunk):,} "
            f"groups_so_far={len(accumulator):,} ({time.time() - t0:.1f}s)",
            file=sys.stderr,
        )

    assert accumulator is not None
    accumulator["cell_count"] = accumulator["cell_count"].astype("int64")
    accumulator["dataset_ids"] = accumulator["dataset_ids"].apply(
        lambda s: sorted(s)[:MAX_DATASET_IDS_PER_GROUP]
    )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / CELL_COUNTS_FILENAME
    accumulator.to_parquet(out_path, index=False)
    print(
        f"wrote {out_path} ({len(accumulator):,} rows, "
        f"{out_path.stat().st_size / 1e6:.1f} MB)",
        file=sys.stderr,
    )

    # Dataset metadata, restricted to the datasets that actually appear in
    # human data (census_info.datasets covers every organism).
    print("reading census_info.datasets...", file=sys.stderr)
    datasets = census["census_info"]["datasets"].read().concat().to_pandas()
    datasets = datasets[datasets["dataset_id"].isin(seen_dataset_ids)].reset_index(drop=True)
    ds_out_path = CACHE_DIR / DATASETS_FILENAME
    datasets.to_parquet(ds_out_path, index=False)
    print(
        f"wrote {ds_out_path} ({len(datasets):,} human datasets, "
        f"{ds_out_path.stat().st_size / 1e6:.1f} MB)",
        file=sys.stderr,
    )

    census.close()
    print(f"done in {time.time() - t_start:.1f}s total", file=sys.stderr)


if __name__ == "__main__":
    build()
