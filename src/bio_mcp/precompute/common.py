"""Constants shared between the precompute jobs and `sources/census.py`.

Deliberately dependency-free (no `cellxgene_census`, no `pandas`) so
`sources/census.py` can import it without pulling in anything heavy.
"""

from __future__ import annotations

import os
from pathlib import Path

# Pinned Census release. Verified live 2026-09-01 against cellxgene-census
# 1.18.0: this is the current LTS "stable" build
# (cellxgene_census.get_census_version_directory(lts=True)["stable"]).
# design.md section 3 and scope.md section 6 both require pinning explicitly
# rather than tracking "stable", so a Census schema change upstream can't
# silently change bio-mcp's answers. Bump deliberately, not automatically.
CENSUS_VERSION = "2025-11-08"

ORGANISM = "homo_sapiens"

# bio-mcp binds the tools' `tissue` argument to the Census `tissue_general`
# obs column (the coarse, ~71-value grouping — e.g. "lung", "blood",
# "brain") rather than the finer-grained `tissue` column (hundreds of
# specific anatomical terms). MVP.md's example call,
# `gene_evidence("MYC", tissue="lung")`, matches a tissue_general value
# exactly. This mapping isn't stated explicitly in MVP.md/scope.md; it's a
# judgment call made here, not a verified upstream fact.
TISSUE_COLUMN = "tissue_general"
CELL_TYPE_COLUMN = "cell_type"

# Where precomputed tables and downloads are cached at runtime. Mirrors the
# pattern in http.py.
CACHE_DIR = Path(os.environ.get("BIO_MCP_CACHE_DIR", Path.home() / ".cache" / "bio-mcp"))

CELL_COUNTS_FILENAME = "census_cell_counts.parquet"
DATASETS_FILENAME = "census_datasets.parquet"
EXPRESSION_CUBE_FILENAME = "census_expression_cube.parquet"
