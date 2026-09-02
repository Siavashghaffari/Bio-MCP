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

Run bio-mcp either as a packaged desktop extension or straight from a clone.

The server speaks **stdio** — your MCP client launches it as a subprocess and
exchanges JSON-RPC over stdin/stdout. It depends only on
`pandas`, `pyarrow`, `httpx` and `mcp`: no TileDB, no 30 GB downloads.

Two setup steps apply to both installs:

**Census tables.** The Census tools read precomputed tables from
`~/.cache/bio-mcp/`. Build them once (Linux/macOS — `cellxgene-census` has no
Windows wheels):

```bash
pip install -e ".[precompute]"
python -m bio_mcp.precompute.build_cell_counts      # ~5 minutes, exact
python -m bio_mcp.precompute.build_expression_cube  # ~1.5 hours, sampled
```

See [Performance](#performance) for why they are precomputed rather than
queried live. Set `BIO_MCP_CUBE_BASE_URL` to a host serving these files to
have the server fetch them on first use instead of building them.

**ORCS key.** `crispr_screen_hits` and `screens_in_cell_line` need
`ORCS_ACCESS_KEY`, a free key from
[orcsws.thebiogrid.org](https://orcsws.thebiogrid.org/). Without it those two
tools return a message explaining how to get one; every Census tool,
including the Census half of `gene_evidence`, works regardless.

### Option 1 — Desktop Extension (MCPB / `.dxt`)

A one-click bundle for Claude Desktop and other MCPB-compatible clients, with
a GUI field for the ORCS key. Build it from [`manifest.json`](manifest.json):

```bash
npm install -g @anthropic-ai/mcpb   # the `mcpb` CLI (formerly `dxt`)
pip install --target lib .          # vendor bio_mcp + deps into lib/
mcpb validate manifest.json
mcpb pack . bio-mcp.mcpb
```

Then drag `bio-mcp.mcpb` onto your client and fill in the optional key.

Three properties of the packed bundle, measured:

- **It is platform- and Python-version-specific.** `pip install --target`
  vendors *binary* wheels, so a bundle packed on Windows/CPython 3.10 runs
  only there. Build one per platform you ship to. (`manifest.json` lists all
  three platforms because the *source* supports all three.)
- **It is large**: ~63 MB packed, ~181 MB unpacked — `pandas`, `pyarrow` and
  `mcp`'s Windows `pywin32` dependency dominate.
- **First launch takes ~10 s** while Python imports that tree cold. Later
  launches are faster once `.pyc` files exist.

The bundle runs [`mcpb_entry.py`](mcpb_entry.py) rather than `python -m
bio_mcp` directly. `pip install --target` does not process `.pth` files, so
`PYTHONPATH=lib` is not equivalent to installing — and `mcp` depends on
`pywin32` on Windows, which needs a `.pth` to put `pywintypes` on the path.
The shim restores that; without it the bundled server dies on import.

### Option 2 — from source

```bash
git clone https://github.com/siavashghaffari/bio-mcp
cd bio-mcp
pip install -e .
```

That installs a `bio-mcp` console script. Point your client at either it or
`python -m bio_mcp`:

```json
{
  "mcpServers": {
    "bio-mcp": {
      "command": "bio-mcp",
      "env": { "ORCS_ACCESS_KEY": "your-key-here" }
    }
  }
}
```

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

Building these tables is the one-time setup step under
[Install](#install). See
[`src/bio_mcp/precompute/__init__.py`](src/bio_mcp/precompute/__init__.py)
for the full measurement writeup.

## Example

Every transcript below is captured from a real run. Rows are elided with `...`
where noted; nothing is reconstructed or illustrative.

`find_cells` over the full Census metadata table — 0.23 s and 344 tokens,
against a 3-second and 400-token budget:

```
> find_cells(tissue="lung")
- **total_cells:** 6,167,731
- **filters:** {'tissue': 'lung', 'cell_type': None, 'disease': None, 'assay': None}

| Tissue | Cell type | Cells |
| --- | --- | --- |
| lung | unknown | 1124691 |
| lung | alveolar macrophage | 466169 |
| lung | pulmonary alveolar type 2 cell | 433573 |
| lung | macrophage | 327835 |
| lung | CD4-positive, alpha-beta T cell | 253989 |
| lung | CD8-positive, alpha-beta T cell | 208218 |
| lung | pulmonary alveolar type 1 cell | 205636 |
...
_(242 more rows not shown)_

Datasets: 01209dce-3575-4bed-b1df-129f57fbc031, 093d3bfe-6f0f-4ac0-a7a1-829f94d0a49f, ...
```

`census_datasets` — 0.01 s, 57 tokens:

```
> census_datasets("lung atlas")
| Dataset ID | Title | Collection | Cells |
| --- | --- | --- | --- |
| d8da613f-e681-4c69-b463-e94f5e66847f | A molecular single-cell lung atlas of lethal COVID-19 | A molecular single-cell lung atlas of lethal COVID-19 | 116313 |
```

`gene_evidence` against live BioGRID ORCS, captured on a machine where the
expression cube had not been built. It is the partial-failure path doing its
job: the missing source is named in place, the other half still answers, and
1,534 screens condense to 333 tokens against a 900-token budget.

```
> gene_evidence("MYC", tissue="lung")

# MYC in lung

## Expression
_census unavailable: census_expression_cube.parquet is not in ~/.cache/bio-mcp..._

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
