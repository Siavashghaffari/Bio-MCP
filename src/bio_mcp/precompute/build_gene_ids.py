"""Build the human gene symbol -> Entrez Gene ID table bio-mcp ships.

## Why this exists

Verified live against BioGRID ORCS with a real access key: `/gene/<ID>/`
requires a numeric Entrez Gene ID, and there is no symbol-search
capability anywhere in the API — every parameter name and path form tried
(`geneList`, `geneSymbol`, `searchNames`, `/gene/<symbol>/`,
`identifierType=OFFICIAL_SYMBOL`, ...) either 404s or redirects to a
generic error page. See sources/orcs.py's module docstring for the full
list. Every bio-mcp tool takes a gene symbol, so something has to resolve
`MYC` -> `4609`.

Bundled as package data rather than downloaded on first use (unlike the
Census tables in build_cell_counts.py/build_expression_cube.py): this is
small (~1-2MB), static, human-only reference data. A live third-party
lookup service (e.g. mygene.info) was considered and rejected — scope.md
limits bio-mcp to exactly two sources (Census, ORCS), and this stays
within that boundary by not adding another live dependency.

Source: NCBI's public gene_info reference, filtered to human (tax_id
9606). Output is committed to the repo at
src/bio_mcp/data/human_gene_ids.parquet. Rebuild occasionally with:

    python -m bio_mcp.precompute.build_gene_ids
"""

from __future__ import annotations

import gzip
import io
import sys
from pathlib import Path

import httpx
import pandas as pd

NCBI_URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz"
HUMAN_TAX_ID = "9606"

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "human_gene_ids.parquet"


def build() -> None:
    print(f"downloading {NCBI_URL}...", file=sys.stderr)
    resp = httpx.get(NCBI_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)

    df = pd.read_csv(io.BytesIO(raw), sep="\t", dtype=str, na_values=["-"])
    df = df[df["#tax_id"] == HUMAN_TAX_ID]
    print(f"{len(df):,} human gene records", file=sys.stderr)

    official = df[["Symbol", "GeneID"]].rename(columns={"Symbol": "symbol"})
    official["is_official"] = True

    synonyms = df[["Synonyms", "GeneID"]].dropna(subset=["Synonyms"])
    synonyms = synonyms.assign(symbol=synonyms["Synonyms"].str.split("|")).explode("symbol")
    synonyms = synonyms[["symbol", "GeneID"]]
    synonyms["is_official"] = False

    out = pd.concat([official, synonyms], ignore_index=True)
    out["symbol"] = out["symbol"].str.upper()
    out["gene_id"] = out["GeneID"].astype("int64")
    out = out[["symbol", "gene_id", "is_official"]].drop_duplicates(subset=["symbol", "gene_id"])
    out = out.sort_values(["symbol", "is_official"], ascending=[True, False]).reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUTPUT_PATH, index=False)
    print(
        f"wrote {OUTPUT_PATH} ({len(out):,} symbol->id rows, "
        f"{OUTPUT_PATH.stat().st_size / 1e6:.2f} MB)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    build()
