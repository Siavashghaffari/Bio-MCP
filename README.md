# bio-mcp

An MCP server giving AI agents access to two public biology databases that
currently have no MCP server — [CZ CELLxGENE Census](https://chanzuckerberg.github.io/cellxgene-census/)
and [BioGRID ORCS](https://orcs.thebiogrid.org/) — plus one tool that joins
them.

> Is this gene expressed in my cell type, and does knocking it out do
> anything?

Today that takes a Census notebook, a separate BioGRID search, and manual
reconciliation. `gene_evidence` collapses it into one call.

Human data only. Six tools, stdio transport, Apache-2.0 licensed.

## Why this exists

Every public bio database that's a simple REST lookup already has an MCP
server, several of them official. Census and ORCS don't, because they
can't be queried with a plain HTTP GET — Census needs TileDB-SOMA, ORCS
needs a key and result normalization. That difficulty is the point: it's
what stops this being reproduced in a weekend, and it's also why naive
live queries don't work (see [Performance](#performance) below).

## Install

```bash
pip install bio-mcp
```

Then point your MCP client (Claude Desktop, etc.) at it over stdio:

```json
{
  "mcpServers": {
    "bio-mcp": {
      "command": "bio-mcp"
    }
  }
}
```

The first call to a Census tool downloads a small precomputed data table
(a few MB) and caches it under `~/.cache/bio-mcp/`. No TileDB, no 30GB
downloads — the server depends only on `pandas`/`pyarrow`/`httpx`/`mcp`,
and runs on Linux, macOS, and Windows.

For `crispr_screen_hits` and `screens_in_cell_line`, set `ORCS_ACCESS_KEY`
to a free key from [orcsws.thebiogrid.org](https://orcsws.thebiogrid.org/).
Without it, those two tools return a message explaining how to get one;
every Census tool (including the Census half of `gene_evidence`) works
regardless.

## Tools

| Tool | Source | Arguments | Returns |
|---|---|---|---|
| `find_cells` | Census | `tissue`, `cell_type`, `disease`, `assay` (all optional) | Cell counts by tissue/cell type, matching dataset IDs |
| `expression_by_cell_type` | Census | `gene`, `tissue` | Mean expression and % expressing, per cell type |
| `census_datasets` | Census | free-text `query` | Matching datasets with cell counts |
| `crispr_screen_hits` | ORCS | `gene` | Human screens calling it a hit: cell line, screen type, score |
| `screens_in_cell_line` | ORCS | `cell_line` | Screens available for that line |
| `gene_evidence` | Census + ORCS | `gene`, `tissue` | The join — expression and screen hits in one call |

`tissue` refers to Census's coarse `tissue_general` grouping (e.g. "lung",
"blood", "brain"), not the finer-grained anatomical ontology term.

`gene` on the ORCS tools takes a human gene symbol (e.g. "MYC"). ORCS
itself only accepts numeric Entrez Gene IDs — verified live, there's no
symbol-search endpoint at all — so bio-mcp ships a small bundled
symbol→ID table (NCBI's gene reference, human-only) to resolve it. An
unrecognized symbol returns a clear message rather than a cryptic upstream
error.

## Performance

`expression_by_cell_type` needs to hit a 3-second budget. Live TileDB-SOMA
queries against the full Census (158,982,719 human cells, 61,497 genes)
were measured, not estimated:

| Query | Measured |
|---|---|
| Cell-count query, single filter, census already open | 8–18s |
| Single gene × one tissue, full expression lookup | >120s, killed |
| All genes × full corpus (what a complete precompute would need) | ~36h+, ~1.5TB from S3 |

All three are far over budget, and there's no live-query shortcut — even
restricting to one gene still touches nearly the whole array. So Census
tools read from **precomputed tables** instead:

- `find_cells` / `census_datasets` come from an **exact, full scan** of
  Census metadata (metadata-only reads are fast: the whole human corpus
  scans in about a minute) — no sampling, no approximation.
- `expression_by_cell_type` comes from a **sampled** scan: large
  contiguous blocks of cells spread across the corpus (scattered/random
  single-cell sampling is paradoxically *slower* than a full contiguous
  scan — TileDB's tile locality punishes random access hard). Every
  result states how many cells the estimate is based on.

Rebuild the tables yourself with `pip install bio-mcp[precompute]`
(pulls in `cellxgene-census` — Linux/macOS only, no Windows wheels) and:

```bash
python -m bio_mcp.precompute.build_cell_counts       # ~1 minute, exact
python -m bio_mcp.precompute.build_expression_cube    # ~1.5 hours, sampled
```

See `src/bio_mcp/precompute/__init__.py` for the full measurement writeup.

## Example

<!--
  scope.md's rule: never write example output that wasn't actually
  executed. The gene_evidence transcript below is real but partial — the
  expression cube was still building when this was captured. Will be
  replaced with the full Census+ORCS transcript once it finishes.
-->

```
> gene_evidence("MYC", tissue="lung")

# MYC in lung

## Expression
_census unavailable: census_expression_cube.parquet is not cached locally..._
(the sampled expression cube was still building when this was captured —
 see Performance below)

## CRISPR screen hits
Hit in 943 of 1534 human screens tested.

| Cell line | Screen type | Phenotype |
| --- | --- | --- |
| HCT 116 | Negative Selection | cell proliferation |
| HeLa | Negative Selection | cell proliferation |
| 143B | Negative Selection | cell proliferation |
| hTERT-RPE1 | Negative Selection | cell proliferation |
| DLD-1 | Negative Selection | cell proliferation |
...
_(933 more rows not shown)_
```
333 tokens, well under the 900-token budget for this tool.

Census-only tools, verified live, both under the 3-second budget by two
orders of magnitude:

```
> find_cells(tissue="lung")
- **total_cells:** 6,167,731
- **filters:** {'tissue': 'lung', 'cell_type': None, 'disease': None, 'assay': None}

| Tissue | Cell type | Cells |
| --- | --- | --- |
| lung | unknown | 1124691 |
| lung | alveolar macrophage | 466169 |
| lung | pulmonary alveolar type 2 cell | 433573 |
...
_(242 more rows not shown)_
```
0.08s.

## Development

```bash
git clone https://github.com/siavashghaffari/bio-mcp
cd bio-mcp
pip install -e ".[dev]"
ruff check .
pytest tests/ -v          # offline, no network, this is what CI runs
python -m bio_mcp.selftest  # live smoke test, hits every tool once
```

## Out of scope

Ensembl, UniProt, Open Targets, cBioPortal, GEO, GTEx, DepMap — already
have MCP servers. ARCHS4 and recount3 — need 30GB+ local downloads,
deferred. Mouse or any non-human species. HTTP transport, auth, a hosted
version, a web UI. See `scope.md` for the full boundary list and why.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE) for attribution of the
public data sources bio-mcp draws on (CZ CELLxGENE Census, BioGRID ORCS,
NCBI Gene).
