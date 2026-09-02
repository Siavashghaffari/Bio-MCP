# bio-mcp — MVP

Paste this whole file into Claude Code as the build brief.

---

## 1. What to build

An MCP server called `bio-mcp` that gives an AI agent access to three public
biology databases that currently have no MCP server, plus one tool that joins them.

It answers a question that today takes three separate tools and half a day:

> Is this gene expressed in my cell type, and does knocking it out actually do anything?

## 2. Why these three sources

Every public bio database that is a simple REST lookup already has an MCP server,
several of them official (Open Targets, cBioPortal). Those are closed.

The three below have no MCP server because they cannot be queried with a simple
HTTP GET. They need real data engineering. That difficulty is the entire point of
this project — it is what stops someone reproducing it in a weekend.

| Source | What it holds | Why it is hard |
|---|---|---|
| CZ CELLxGENE Census | ~100M curated single cells | TileDB-SOMA, queries are slow if written naively |
| BioGRID ORCS | Hit calls from ~1,500 published CRISPR screens | REST, but needs a key and result normalisation |
| ARCHS4 | 1.5M uniformly processed RNA-seq samples | 30GB+ HDF5, cannot be queried live |

## 3. Verified API facts

Use these. Do not guess at endpoints or invent field names. If something below
turns out to be wrong when you run it, fix it against the live API and say so.

### CZ CELLxGENE Census

Python package: `pip install cellxgene-census`

Main functions:

- `cellxgene_census.open_soma()` — open the Census by version or URI
- `cellxgene_census.get_anndata()` — build and run a query, return an AnnData
- `cellxgene_census.get_obs()` — cell metadata for a query
- `cellxgene_census.get_var()` — gene metadata for a query
- `cellxgene_census.get_presence_matrix()` — feature presence, scipy sparse

Queries filter on `obs` columns using a `value_filter` string, e.g.
`tissue_general == 'lung' and disease == 'normal'`.

### BioGRID ORCS

Base URL: `https://orcsws.thebiogrid.org/`

Requires a free access key from a registration form at that URL. Passed as
`accesskey=<32-char key>` on every request.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `/organisms/` | Supported organisms |
| `/vocabs/` and `/vocab/<ID>` | Controlled vocabularies |
| `/screens/` | Search and filter screens |
| `/screen/<ID>` | Scores for one screen |
| `/gene/<ID>` | Scores across all screens for one gene |
| `/genes/` | Multiple genes at once |

Common params: `accessKey` (required), `format` (`tab` or `json`, default `tab`),
`header` (default `no`). GET and POST both work.

### ARCHS4

Python package: `pip install archs4py`

Data functions all return pandas DataFrames, genes as rows, samples as columns:
`archs4py.data.rand()`, `.index()`, `.meta()`, `.samples()`, `.series()`.
A parallel `archs4py.meta` module returns metadata only, with a `field()` function
for one attribute across the whole dataset.

**Important constraint:** the HDF5 files are over 30GB each and must be downloaded
before use. An MCP server cannot ask users to download 30GB. See the build order
in section 6.

## 4. Tools to expose

Seven or fewer. Do not add more. Tool count is a design constraint, not an
afterthought — agents get worse at picking the right tool as the list grows.

| Tool | Source | Arguments | Returns |
|---|---|---|---|
| `find_cells` | Census | tissue, cell_type, disease, assay (all optional) | Cell counts and matching dataset IDs |
| `expression_by_cell_type` | Census | gene, tissue | Mean expression and percent expressing, per cell type |
| `census_datasets` | Census | free-text query | Matching datasets with cell counts |
| `crispr_screen_hits` | ORCS | gene, organism | Screens calling it a hit: cell line, screen type, score |
| `screens_in_cell_line` | ORCS | cell_line | Available screens for that line |
| `gene_evidence` | Census + ORCS | gene, tissue | The join. See below. |

`gene_evidence` is the reason this repo exists. It runs the Census and ORCS
queries in parallel and returns one answer: where the gene is expressed by cell
type, and whether knocking it out has a phenotype in published screens. Everything
else is supporting cast.

## 5. Hard requirements

**Speed.** `gene_evidence` must return in under 3 seconds. A tool that takes 40
seconds will not be used. If a live Census query cannot hit that, pre-compute the
aggregate and query the pre-computed table instead. Solving this is the project.

**Token budget.** Every tool returns trimmed markdown, never raw API JSON. Put the
budgets in one module (`trim.py` or similar) from the first commit. Large result
sets become markdown tables that state how many rows were hidden, not truncated
JSON. Target: no single tool response over 1,000 tokens.

**Graceful degradation.** `gene_evidence` fans out with
`asyncio.gather(return_exceptions=True)`. If ORCS is down, return the Census half
plus a line naming what failed. Never fail the whole call because one upstream had
a bad minute.

**Politeness.** Cap concurrency with a semaphore. Retry with exponential backoff on
429 and 5xx only, never on 4xx. Cache responses on disk with a TTL. ORCS is a small
academic service — do not hammer it.

**Secrets.** The ORCS key comes from an environment variable (`ORCS_ACCESS_KEY`).
Never commit a key. If it is missing, the ORCS tools return a clear message telling
the user how to get one, and the Census tools keep working.

## 6. Build order

**Phase 1 — Census.** Get `find_cells` and `expression_by_cell_type` working and
fast. This is the hardest part and the whole moat. Do not move on until a query
returns in under 3 seconds.

**Phase 2 — ORCS.** Register for a key first. Add `crispr_screen_hits` and
`screens_in_cell_line`. Straightforward REST, mostly normalisation work.

**Phase 3 — the join.** `gene_evidence`. Parallel, degrades gracefully.

**Phase 4 — ARCHS4, only if phases 1 to 3 are solid.** Do not put a 30GB download
in the install path. Instead, run a one-off offline job that computes what the
tools actually need (per-gene tissue-level mean expression, and top co-expressed
genes), and ship that as a compact table. Treat this as a separate decision, not
part of the MVP.

## 7. Stack

- Python 3.10+
- `mcp` SDK, FastMCP, stdio transport
- `cellxgene-census`, `httpx`
- `pytest` with offline fixture-based tests
- Optional Dockerfile

## 8. Testing

Offline tests using recorded fixtures for all parsing and trimming logic. These
must pass with no network.

A separate `selftest` module that hits the live APIs once each and prints the
response size of every tool, so it is obvious if an answer starts ballooning.

## 9. Done when

`gene_evidence("MYC", tissue="lung")` returns cell-type expression from Census and
CRISPR hit status from ORCS, in one answer, in under 3 seconds, under 1,000 tokens.

The README shows that transcript from a real run.

## 10. Out of scope

- Any source that already has an MCP server. If a user needs UniProt or Open
  Targets, tell them to install those servers alongside this one. Do not reimplement.
- Mouse and other species. Human only.
- HTTP transport, auth, hosted version.
- ARCHS4, until phase 4.

## 11. Rules for you, the builder

Do not write example output in the README that you have not actually run. If you
cannot reach an API, leave the example blank and say so in the file. An invented
transcript is worse than no transcript.

Mark clearly in code comments which API response shapes you verified against a live
call and which you took from documentation.

If a source turns out to be unreachable or the API differs from section 3, stop and
report it rather than writing code around a guess.
