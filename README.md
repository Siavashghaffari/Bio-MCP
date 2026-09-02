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

> **Release status.** Two things haven't shipped yet (Phase 4 in
> [`scope.md`](scope.md)): the `bio-mcp` package isn't on PyPI, and the
> precomputed Census tables aren't hosted for download.
>
> So **Option 3 (pip/uvx) cannot work yet** — there is nothing on PyPI to
> install — and **Option 2 (Smithery)** has nothing to resolve either.
> **Options 1, 4 and 5** build and run from this repo today. Whichever you
> pick, the Census tools need the precomputed tables in `~/.cache/bio-mcp/`
> first (see [Performance](#performance)); the two ORCS tools work
> everywhere once you set a key.

The server speaks **stdio**: your MCP client launches it as a subprocess and
exchanges JSON-RPC over stdin/stdout. It depends only on
`pandas`/`pyarrow`/`httpx`/`mcp` — no TileDB, no 30GB downloads — and runs on
Linux, macOS, and Windows. The first Census call downloads a small
precomputed table (a few MB) into `~/.cache/bio-mcp/`.

For `crispr_screen_hits` and `screens_in_cell_line`, set `ORCS_ACCESS_KEY`
to a free key from [orcsws.thebiogrid.org](https://orcsws.thebiogrid.org/).
Without it, those two tools return a message explaining how to get one;
every Census tool (including the Census half of `gene_evidence`) works
regardless.

### Option 1 — Desktop Extension (MCPB / `.dxt`)

A one-click bundle for Claude Desktop and other MCPB-compatible clients, with
a GUI field for the ORCS key. Built from [`manifest.json`](manifest.json):

```bash
npm install -g @anthropic-ai/mcpb   # the `mcpb` CLI (formerly `dxt`)
pip install --target lib .          # vendor bio_mcp + deps into lib/
mcpb validate manifest.json
mcpb pack . bio-mcp.mcpb
```

Then drag `bio-mcp.mcpb` onto your client and fill in the optional key.

Three things worth knowing, all measured on a real packed bundle:

- **A bundle is platform- and Python-version-specific.** `pip install
  --target` vendors *binary* wheels — here `cp310-win_amd64` — so a bundle
  packed on Windows/3.10 runs only there. Build one per platform you ship to.
  (`manifest.json` lists all three platforms because the *source* supports
  all three.)
- **It is large**: ~63 MB packed, ~181 MB unpacked. `pandas`, `pyarrow` and
  `mcp`'s own `pywin32` dependency dominate.
- **First launch takes ~10 s** while Python imports that tree cold; later
  launches are quicker once `.pyc` files exist.

The bundle runs [`mcpb_entry.py`](mcpb_entry.py) rather than `python -m
bio_mcp` directly. That shim exists for a specific reason: `pip install
--target` does not process `.pth` files, so `PYTHONPATH=lib` is *not*
equivalent to installing — and `mcp` depends on `pywin32` on Windows, which
relies on a `.pth` to put `pywintypes` on the path. Without the shim the
bundled server dies on import before it speaks any JSON-RPC.

The packed `.mcpb` isn't published as a release asset yet — build it locally
with the commands above.

### Option 2 — Smithery · *pending release*

Once bio-mcp is published, [`smithery.yaml`](smithery.yaml) lets Smithery
install it:

```bash
npx -y @smithery/cli install bio-mcp --client claude
```

The config is structurally valid and its `commandFunction` produces the right
launch spec with and without a key, but it has never been run against
Smithery itself — nothing is published for it to resolve. Smithery's schema
also moves; re-check it against their current docs before submitting.

### Option 3 — pip / uvx / pipx · *pending PyPI release*

```bash
pip install bio-mcp            # then command: "bio-mcp"
uvx bio-mcp                    # run without installing (like npx)
pipx install bio-mcp
```

MCP client config (stdio):

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

### Option 4 — from source *(works today)*

```bash
git clone https://github.com/siavashghaffari/bio-mcp
cd bio-mcp
pip install -e .
python -m bio_mcp              # starts the stdio server
```

MCP client config:

```json
{
  "mcpServers": {
    "bio-mcp": {
      "command": "python",
      "args": ["-m", "bio_mcp"],
      "env": { "ORCS_ACCESS_KEY": "your-key-here" }
    }
  }
}
```

The Census tools need the precomputed tables in `~/.cache/bio-mcp/` — build
them with `pip install -e ".[precompute]"` and the jobs in
[Performance](#performance) (Linux/macOS), or point `BIO_MCP_CUBE_BASE_URL`
at a host that has them.

### Option 5 — Docker

```bash
docker build -t bio-mcp .
```

```json
{
  "mcpServers": {
    "bio-mcp": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
               "-e", "ORCS_ACCESS_KEY",
               "-v", "bio-mcp-cache:/home/app/.cache/bio-mcp",
               "bio-mcp"]
    }
  }
}
```

The `-i` flag is required (stdio). The named volume persists the Census
tables between runs.

The image builds `bio-mcp` from source in a first stage and installs only the
resulting wheel into the runtime stage, which runs as a non-root user. Note
the build has not been executed end-to-end yet — it needs to reach PyPI from
inside the container, which a TLS-intercepting network will block until the
proxy's root CA is added to the image.

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
