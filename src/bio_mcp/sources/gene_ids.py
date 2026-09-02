"""Human gene symbol -> Entrez Gene ID resolution, for BioGRID ORCS.

See precompute/build_gene_ids.py for why this exists (verified live: ORCS
has no symbol-search capability, only numeric Entrez IDs) and how the
table is built. Loaded once from bundled package data — no network call,
no cache directory, just ships with the package.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache

import pandas as pd

from bio_mcp.errors import SourceError


@lru_cache(maxsize=1)
def _table() -> pd.DataFrame:
    ref = importlib.resources.files("bio_mcp.data").joinpath("human_gene_ids.parquet")
    with importlib.resources.as_file(ref) as path:
        return pd.read_parquet(path)


def resolve_entrez_id(symbol: str) -> int:
    """Look up the Entrez Gene ID for a human gene symbol.

    Matches case-insensitively against both official symbols and known
    synonyms (NCBI gene_info), preferring an official-symbol match when a
    symbol is ambiguous. Raises SourceError if nothing matches — most
    likely a typo, or a non-human/deprecated symbol.
    """
    matches = _table()[_table()["symbol"] == symbol.upper()]
    if matches.empty:
        raise SourceError(
            "orcs",
            f"'{symbol}' is not a recognized human gene symbol (checked against NCBI's "
            "gene reference). Check spelling — it may also be a deprecated or "
            "non-human symbol.",
        )
    # Sorted with official symbols first at build time (see
    # build_gene_ids.py), so the first match is the best available.
    return int(matches.iloc[0]["gene_id"])
