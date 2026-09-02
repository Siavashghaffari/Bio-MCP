"""BioGRID ORCS: CRISPR screen hit calls.

## Scope note — resolving a conflict between scope.md and MVP.md

MVP.md's tool table lists `organism` as an argument to `crispr_screen_hits`.
scope.md section 2 hard-requires "Species: Human only" and section 3
explicitly excludes "Mouse or any non-human species" as a boundary never to
add. Per the precedence both files state ("scope.md wins on boundaries"),
this module is human-only: there is no organism parameter. Every query is
pinned to human (NCBI taxonomy ID 9606). Flagged to the user rather than
silently picking a side.

## Verified live (2026-09-02, with a real ORCS_ACCESS_KEY)

Nothing in this module's request/response handling is a guess anymore —
everything below was confirmed against real responses:

- `/gene/<Entrez ID>/` returns hit/score data across every screen that
  tested the gene. It needs a **numeric Entrez Gene ID**, not a symbol —
  every symbol-based alternative was tried and failed (`geneList`,
  `geneSymbol`, `geneName`, `officialSymbol`, `searchNames`,
  `geneSymbols`, `/gene/<symbol>/`, `/genes/<symbol>/`,
  `identifierType=OFFICIAL_SYMBOL`, `idType=symbol`: every one either
  returns `{"STATUS":"ERROR","MESSAGE":["You must use at least one valid
  gene id or official symbol name in your request"]}`, a 404, or a
  redirect to a generic error page). Real fields: `SCREEN_ID`,
  `IDENTIFIER_ID`, `IDENTIFIER_TYPE`, `OFFICIAL_SYMBOL`, `ALIASES`,
  `ORGANISM_ID`, `ORGANISM_OFFICIAL`, `SCORE.1`..`SCORE.5`, `HIT`
  (`"YES"`/`"NO"`), `SOURCE`. No cell line, screen type, or phenotype —
  that lives on the screen, not the gene-in-screen record.
- `/screens/` (no gene filter — full listing, small dataset: ~1,950 rows
  for human) has the screen-level metadata: `SCREEN_ID`, `CELL_LINE`,
  `CELL_TYPE`, `SCREEN_TYPE`, `PHENOTYPE`, `THROUGHPUT`, `SOURCE_ID` /
  `SOURCE_TYPE` (publication reference), `SCREEN_NAME`, `AUTHOR`, and
  more. So `crispr_screen_hits` joins the two by `SCREEN_ID`.
- JSON responses are plain lists of row dicts (`format=json`), not the
  row-number-keyed object MVP.md's REST conventions might suggest — but
  `_as_records` still defends against that shape too, cheaply.
- Gene symbol -> Entrez ID resolution has no ORCS endpoint at all; see
  `sources/gene_ids.py` for how bio-mcp fills that gap.

`screens_in_cell_line` reuses the same `/screens/` listing and filters
client-side, rather than guessing at a server-side filter parameter that
hasn't been verified.
"""

from __future__ import annotations

import os

from bio_mcp.errors import SourceError
from bio_mcp.http import get_json
from bio_mcp.sources.gene_ids import resolve_entrez_id

BASE_URL = "https://orcsws.thebiogrid.org"
HUMAN_ORGANISM_ID = 9606  # NCBI taxonomy ID for Homo sapiens

ACCESS_KEY_ENV_VAR = "ORCS_ACCESS_KEY"
ACCESS_KEY_HELP = (
    "ORCS_ACCESS_KEY is not set. Register for a free BioGRID ORCS access key at "
    "https://orcsws.thebiogrid.org/ and set it as the ORCS_ACCESS_KEY environment "
    "variable. Census tools are unaffected."
)

# The full screen listing is small (~1,950 human rows) and doesn't change
# often; the default http.py disk cache (1 hour TTL) is deliberately reused
# rather than fetched fresh per call.


def _access_key() -> str:
    key = os.environ.get(ACCESS_KEY_ENV_VAR)
    if not key:
        raise SourceError("orcs", ACCESS_KEY_HELP)
    return key


def _base_params() -> dict[str, str]:
    return {"accessKey": _access_key(), "format": "json", "header": "no"}


def _as_records(data: object) -> list[dict]:
    """Normalize ORCS JSON into a list of row dicts.

    Verified live: successful responses are plain JSON lists. This also
    defends against a row-number-keyed object (`{"1": {...}, "2": {...}}`,
    seen elsewhere in BioGRID's classic REST APIs) in case a different
    endpoint or a future response ever uses it — cheap insurance, not a
    guess this module relies on.
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if "STATUS" in data and "MESSAGE" in data:
            message = data["MESSAGE"]
            text = "; ".join(message) if isinstance(message, list) else str(message)
            raise SourceError("orcs", text)
        return [v for v in data.values() if isinstance(v, dict)]
    raise SourceError("orcs", f"unexpected response shape: {type(data).__name__}")


async def _all_human_screens() -> dict[str, dict]:
    """Every human screen's metadata, keyed by SCREEN_ID. Verified live shape."""
    params = {**_base_params(), "organismID": HUMAN_ORGANISM_ID}
    data = await get_json("orcs", f"{BASE_URL}/screens/", params=params)
    return {r["SCREEN_ID"]: r for r in _as_records(data)}


def _normalize_screen_hit(gene_row: dict, screen: dict | None) -> dict:
    """Condense one gene-in-screen record (joined with its screen metadata,
    when available) to the fields bio-mcp shows. All field names verified
    live — see module docstring.
    """
    screen = screen or {}
    return {
        "gene_symbol": gene_row.get("OFFICIAL_SYMBOL"),
        "screen_id": gene_row.get("SCREEN_ID"),
        "cell_line": screen.get("CELL_LINE"),
        "cell_type": screen.get("CELL_TYPE"),
        "screen_type": screen.get("SCREEN_TYPE"),
        "score": gene_row.get("SCORE.1"),
        "phenotype": screen.get("PHENOTYPE"),
        "hit": str(gene_row.get("HIT", "")).strip().upper() == "YES",
        "publication": screen.get("SOURCE_ID") if screen.get("SOURCE_TYPE") == "pubmed" else None,
        "throughput": screen.get("THROUGHPUT"),
    }


async def crispr_screen_hits(gene: str) -> dict:
    """Human CRISPR screens that scored `gene`, from BioGRID ORCS.

    Returns a dict with `gene`, `hits` (list of normalized screen results,
    see `_normalize_screen_hit`), and `total_screens_tested`. Raises
    `SourceError` if `ORCS_ACCESS_KEY` is missing, `gene` isn't a
    recognized human gene symbol, or the request fails.
    """
    entrez_id = resolve_entrez_id(gene)
    params = {**_base_params(), "organismID": HUMAN_ORGANISM_ID}
    data = await get_json("orcs", f"{BASE_URL}/gene/{entrez_id}/", params=params)
    records = _as_records(data)

    screens = await _all_human_screens()
    hits = [_normalize_screen_hit(r, screens.get(r.get("SCREEN_ID"))) for r in records]
    return {
        "gene": gene,
        "hits": hits,
        "total_screens_tested": len(records),
    }


def _normalize_cell_line(name: str) -> str:
    """Loosen cell line names for matching, e.g. "K562" == "K-562".

    Verified live: ORCS stores the common leukemia line as "K-562", not
    "K562" — an easy real-world mismatch. Stripping non-alphanumerics
    before comparing absorbs hyphen/space inconsistencies like this one.
    """
    return "".join(ch for ch in name.upper() if ch.isalnum())


async def screens_in_cell_line(cell_line: str) -> dict:
    """Human CRISPR screens run in `cell_line`, from BioGRID ORCS.

    Matches `cell_line` against the ORCS `CELL_LINE` field, ignoring case
    and punctuation (ORCS's own naming isn't fully consistent — e.g.
    "K-562" for what's commonly written "K562"). Returns a dict with
    `cell_line` and `screens` (screen_id, screen_type, title, publication).
    Raises `SourceError` if `ORCS_ACCESS_KEY` is missing or the request
    fails.
    """
    screens = await _all_human_screens()
    needle = _normalize_cell_line(cell_line)
    matched = [
        r for r in screens.values() if _normalize_cell_line(r.get("CELL_LINE") or "") == needle
    ]

    result = [
        {
            "screen_id": r.get("SCREEN_ID"),
            "screen_type": r.get("SCREEN_TYPE"),
            "title": r.get("SCREEN_NAME"),
            "publication": r.get("SOURCE_ID") if r.get("SOURCE_TYPE") == "pubmed" else None,
        }
        for r in matched
    ]
    return {"cell_line": cell_line, "screens": result}
