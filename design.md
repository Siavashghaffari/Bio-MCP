# bio-mcp — design

Third of three files. Read alongside `scope.md` and `MVP.md`.

- `scope.md` — boundaries, phases, what never to build
- `MVP.md` — sources, verified API facts, tool signatures
- **this file** — how the code is structured and why

Where they disagree: scope wins on boundaries, MVP.md wins on API mechanics, this
file wins on structure.

---

## 1. Shape of the repo

```
bio-mcp/
  pyproject.toml
  README.md
  LICENSE
  Dockerfile                  optional, phase 4
  .github/workflows/ci.yml
  src/bio_mcp/
    __init__.py
    server.py                 tool definitions only, no source logic
    http.py                   one HTTP client, retries, cache, concurrency cap
    trim.py                   token budgets and markdown rendering
    selftest.py               live smoke test
    sources/
      __init__.py
      census.py               CELLxGENE Census
      orcs.py                 BioGRID ORCS
  tests/
    test_offline.py
```

Two rules hold this together:

**`server.py` contains no source logic.** It defines tools, calls a source module,
renders the result through `trim`, and returns. If you find yourself parsing an API
response in `server.py`, it belongs in `sources/`.

**Source modules return plain dicts, never markdown.** Rendering happens in
`server.py`. This is what makes the source modules testable offline without
asserting on formatting.

## 2. Data flow

A `gene_evidence` call:

```
server.gene_evidence(gene, tissue)
  |
  +-- asyncio.gather(return_exceptions=True)
  |     |
  |     +-- census.expression_by_cell_type(gene, tissue) --> dict
  |     |     via cellxgene_census, or the precomputed table (section 5)
  |     |
  |     +-- orcs.screen_hits(gene) --> dict
  |           via http.get_json --> cache --> ORCS REST
  |
  +-- render each section through trim.table / trim.kv
  +-- append a line naming any source that raised
  |
  +-- return markdown string
```

The single-source tools are the same flow without the fan-out.

## 3. Design decisions

**Return markdown, not JSON.**
Markdown tables cost fewer tokens than nested objects and models read them more
reliably. Every tool returns a string. No tool returns raw upstream JSON.

**Token budgets live in one module.**
`trim.py` owns every limit. If a budget is hardcoded anywhere else, that is a bug.
Trimming is the product, not cleanup, so it exists from the first commit.

**Truncation is always visible.**
A clipped string ends with a marker. A shortened table states how many rows were
hidden. Never silently drop data.

**Partial failure is normal.**
`asyncio.gather(return_exceptions=True)`, then check each result with
`isinstance(x, Exception)`. A dead upstream becomes a named line in the output, not
a raised error. A user with one working source is better served than a user with an
exception.

**One error type.**
`SourceError(source, message)` carries which upstream failed, so the degradation
line can name it. Everything from a source module raises this or returns a dict.
Nothing else propagates.

**Retry only what is worth retrying.**
429 and 5xx get exponential backoff. 4xx fails immediately, because retrying a bad
request just wastes the upstream's time.

**Cache on disk, keyed by request.**
Public APIs rate-limit and a demo that dies mid-conversation is worthless. A broken
cache must never break a query, so wrap every cache read and write in try/except and
carry on.

**Cap concurrency globally.**
One semaphore in `http.py`, not per-source. A single `gene_evidence` call must never
look like an attack to a small academic service.

## 4. Module contracts

**`sources/*.py`**
Async functions. Take primitives, return plain dicts with condensed fields. Raise
`SourceError` on failure. Never import from `server.py`. Never produce markdown.

**`http.py`**
`get_json(source, url, params=..., cache=True)` and `post_json(...)`. Owns retries,
caching, the semaphore, the user agent, and timeouts. Every outbound request goes
through it so these are solved once.

**`trim.py`**
- `clip(text, tokens=n)` — truncate on a word boundary, mark the cut
- `table(rows, columns, max_rows=n)` — markdown table, report hidden rows
- `kv(dict)` — key/value block, drop empty values
- `BUDGETS` — every limit in the project, in one dict

**`server.py`**
One `@mcp.tool()` per tool. Docstrings are the tool descriptions the agent reads, so
write them for an agent deciding whether to call the tool, not for a human reading
source.

## 5. The Census performance decision

This is the crux of the project and the one place where the design may have to change
mid-build.

**Try first: live queries.** `cellxgene_census.open_soma()` with a pinned version,
`get_obs` with a `value_filter`, aggregate in pandas. Measure the wall time of a
realistic call.

**If it exceeds 3 seconds, switch to precomputed.** Run an offline job that computes,
per gene and per cell type, the mean expression and percent of cells expressing. Ship
the result as a compact table (parquet or DuckDB) and have the tools query that.

Do not attempt both paths at once, and do not build the precompute pipeline
speculatively. Measure first, then choose, and tell the user before switching.

If you do switch, the source module's public functions keep the same signatures and
return the same dicts. Only the internals change. Nothing in `server.py` should need
editing.

## 6. Testing design

**Offline tests are the real suite.** They use fixtures captured from real API
responses and must pass with no network. Test the condensing functions, the query
builders, the trimming, and the partial-failure path in `gene_evidence` with
monkeypatched sources.

**Include a size regression test.** Feed `gene_evidence` mock sources returning 200
rows each and assert the output stays under the token budget. This guards the whole
premise of the server.

**The live smoke test is separate.** `python -m bio_mcp.selftest` hits every tool
once and prints the character and approximate token count of each answer. It is not
part of CI's required checks, because public APIs have bad days and that should not
redden the repo.

## 7. Conventions

- Async throughout. No sync HTTP calls.
- Type hints on every public function.
- `from __future__ import annotations` at the top of every module.
- Log to stderr only. stdout is the MCP transport and anything written there
  corrupts the protocol.
- Line length 100, ruff for linting.
- Secrets from environment variables only. `ORCS_ACCESS_KEY`. Never in a file.
