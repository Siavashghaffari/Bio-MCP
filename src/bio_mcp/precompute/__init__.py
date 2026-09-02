"""Offline jobs that build the tables `sources/census.py` queries at runtime.

Not part of the installed server's request path — run by hand (or by CI on a
schedule) to produce the parquet files bio-mcp ships. See design.md section 5
for why: naive live TileDB-SOMA queries measured 8s-36h+ against the 3-second
budget in scope.md, so the aggregates these scripts compute are precomputed
once instead.

Two jobs, because they have very different cost profiles (verified live
against cellxgene-census 1.18.0, census version "2025-11-08", 158,982,719
human cells / 61,497 human genes):

- `build_cell_counts.py` — obs metadata only. A full scan is ~4-5 minutes and
  exact, so `find_cells` and `census_datasets` need no sampling at all.
- `build_expression_cube.py` — needs the X expression matrix, where a full
  scan is roughly 36+ hours and ~1.5TB from S3 (measured: 50k contiguous
  cells x all genes = 40.7s; scattered/sampled individual cells are *worse*,
  >120s for 2,000 cells, because random access defeats TileDB's tile
  locality). So this job reads a bounded set of large contiguous blocks
  spread across the corpus and computes mean expression / percent
  expressing from that sample. The result is a labeled estimate, not an
  exact population statistic — see the module docstring there.
"""

from __future__ import annotations
