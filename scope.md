# bio-mcp — project scope

Read this together with `MVP.md`.

- **This file** sets the boundaries: what to build, what never to build, when a
  phase is finished, and what to do when something goes wrong.
- **`MVP.md`** gives the technical detail: verified API facts, tool
  signatures, and implementation requirements.

Where they disagree, this file wins on scope and MVP.md wins on mechanics.

---

## 1. What you are building

An open-source MCP server that gives AI agents access to two public biology
databases that currently have no MCP server, plus one tool that joins them.

The server must answer this in a single call:

> Is this gene expressed in my cell type, and does knocking it out do anything?

Today that takes a Census notebook, a separate BioGRID search, and manual
reconciliation. Collapsing it into one tool call is the entire value of the project.

## 2. Build this

| Item | Requirement |
|---|---|
| Sources | CZ CELLxGENE Census and BioGRID ORCS. Only these two |
| Species | Human only |
| Tools | Six or fewer, one of which is the cross-source join |
| Transport | stdio |
| Package name | `bio-mcp` on PyPI. Verified available, do not rename |
| License | Apache-2.0 (was MIT; changed for the explicit patent grant) |
| Tests | Offline fixture tests that pass with no network, plus a live smoke test |
| CI | Lint and offline tests on Python 3.10, 3.11 and 3.12 |
| Docs | README containing a transcript from a real executed run |

## 3. Do not build this

These are hard boundaries. Do not add them, do not suggest adding them mid-build,
and do not quietly include them because they seemed useful.

| Excluded | Reason |
|---|---|
| Ensembl, UniProt, Open Targets, cBioPortal, GEO, GTEx, DepMap | Already have MCP servers, two of them official. Reimplementing them destroys the point of the repo |
| ARCHS4 and recount3 | Require a 30GB+ local download. Deferred to a separate decision after this project ships |
| Mouse or any non-human species | Doubles the surface area for no differentiation |
| HTTP transport, authentication, hosted service | Not needed to prove the idea |
| A web UI or dashboard | This is infrastructure, not an application |
| A seventh tool | Tool count is a design constraint. Adding tools makes agents worse at picking the right one |

If you believe something excluded here is necessary, stop and say so. Do not
build it and explain afterwards.

## 4. Phases and gates

Build in this order. Stop at the end of each phase, show what works, and wait
before continuing.

**Phase 0 — ORCS access key**
Nothing to build. Confirm the user has registered for a free BioGRID ORCS access
key at `orcsws.thebiogrid.org`. If they have not, say so and start Phase 1 anyway,
since Phase 1 does not need it.

**Phase 1 — Census tools**
Build `find_cells`, `expression_by_cell_type`, `census_datasets`.
Before writing code, verify the `cellxgene-census` API details in section 3 of
MVP.md against the installed package and report any differences.
*Gate: a query returns in under 3 seconds. Show the timing.*

**Phase 2 — ORCS tools**
Build `crispr_screen_hits` and `screens_in_cell_line`.
*Gate: both return normalised results from a live call.*

**Phase 3 — the join**
Build `gene_evidence`. Parallel fan-out, degrades gracefully on partial failure.
*Gate: with ORCS deliberately disabled, it still returns the Census half plus a
line naming what failed.*

**Phase 4 — package and publish**
pyproject, README, CI, optional Dockerfile.
*Gate: a clean machine goes from `pip install` to a working tool call using only
the README.*

## 5. Acceptance criteria

The project is done when all five hold:

1. `gene_evidence("MYC", tissue="lung")` returns cell-type expression and CRISPR
   hit status in one answer
2. That call completes in under 3 seconds
3. No single tool response exceeds 1,000 tokens
4. One dead upstream produces a partial answer naming the failure, never a hard error
5. The README transcript came from a real run, not from writing out what the output
   would look like

## 6. When things go wrong

**Census queries cannot hit 3 seconds.** This is the most likely failure and the
one that matters most. Do not relax the target. Switch to pre-computing the
aggregates the tools need offline and querying the pre-computed table instead. Tell
the user you are making this change before you make it.

**The ORCS key is missing or rejected.** ORCS tools return a clear message
explaining how to get a key. Census tools and the Census half of `gene_evidence`
keep working. Never crash the server over a missing key.

**An API in section 3 of MVP.md differs from reality.** Stop. Report the
difference. Do not write code around a guess.

**You discover an MCP server already exists for Census or ORCS.** Stop and tell the
user. It changes whether this project is worth building.

**Census schema changes between versions.** Pin the Census version explicitly.
Never track latest.

## 7. Rules that apply throughout

- Never write example output, in the README or anywhere else, that you have not
  actually executed. An invented transcript is worse than no transcript.
- Mark in code comments which API response shapes you verified against a live call
  and which you took from documentation.
- The ORCS access key comes from the `ORCS_ACCESS_KEY` environment variable. Never
  commit a key or write one into a file.
- Cache upstream responses on disk and cap concurrency. ORCS is a small academic
  service. Do not hammer it.
