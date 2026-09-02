"""The real test suite (design.md section 6): fixtures, no network, ever.

Covers trim.py's rendering/budget logic, http.py's retry/cache policy,
sources/orcs.py's response parsing, sources/census.py's query logic against
fixture parquet tables, and server.py's partial-failure degradation in
gene_evidence — plus a size regression test guarding the whole premise of
the project (no tool response over the 1,000-token ceiling).
"""

from __future__ import annotations

import httpx
import pytest

import bio_mcp.http as http_module
import bio_mcp.server as server_module
import bio_mcp.sources.census as census_module
import bio_mcp.sources.gene_ids as gene_ids_module
import bio_mcp.sources.orcs as orcs_module
from bio_mcp import trim
from bio_mcp.errors import SourceError

# ---------------------------------------------------------------------------
# trim.py
# ---------------------------------------------------------------------------


class TestClip:
    def test_short_text_unchanged(self):
        assert trim.clip("hello world", tokens=100) == "hello world"

    def test_empty_text(self):
        assert trim.clip("", tokens=100) == ""

    def test_long_text_truncates_on_word_boundary_with_marker(self):
        text = "word " * 200  # ~1000 chars, well over a 10-token (40-char) budget
        result = trim.clip(text, tokens=10)
        assert result.endswith("…")
        assert len(result) <= 41  # 40 chars + marker
        assert not result[:-1].endswith(" ")  # trimmed trailing space before marker

    def test_no_word_boundary_still_truncates(self):
        text = "a" * 1000
        result = trim.clip(text, tokens=10)
        assert result.endswith("…")
        assert len(result) <= 41


class TestTable:
    COLUMNS = [("name", "Name"), ("count", "Count")]

    def test_empty_rows(self):
        assert trim.table([], self.COLUMNS) == "_no results_"

    def test_basic_rendering(self):
        rows = [{"name": "a", "count": 1}, {"name": "b", "count": 2}]
        out = trim.table(rows, self.COLUMNS)
        assert "| Name | Count |" in out
        assert "| a | 1 |" in out
        assert "| b | 2 |" in out

    def test_missing_field_renders_blank(self):
        out = trim.table([{"name": "a"}], self.COLUMNS)
        assert "| a |  |" in out

    def test_hidden_rows_reported(self):
        rows = [{"name": str(i), "count": i} for i in range(25)]
        out = trim.table(rows, self.COLUMNS, max_rows=20)
        assert "20 more row" not in out  # 25 - 20 = 5, not 20
        assert "(5 more rows not shown)" in out
        assert "| 20 | 20 |" not in out  # row index 20 (21st row) was cut

    def test_singular_hidden_row(self):
        rows = [{"name": str(i), "count": i} for i in range(21)]
        out = trim.table(rows, self.COLUMNS, max_rows=20)
        assert "(1 more row not shown)" in out

    def test_pipe_and_newline_in_cell_are_escaped(self):
        out = trim.table([{"name": "a|b\nc", "count": 1}], self.COLUMNS)
        assert "a\\|b c" in out


class TestKv:
    def test_drops_empty_values(self):
        out = trim.kv({"a": 1, "b": None, "c": "", "d": [], "e": {}, "f": "x"})
        assert "**a:**" in out
        assert "**f:**" in out
        for dropped in ("b", "c", "d", "e"):
            assert f"**{dropped}:**" not in out

    def test_all_empty(self):
        assert trim.kv({"a": None, "b": ""}) == "_no data_"

    def test_custom_labels(self):
        out = trim.kv({"a": 1}, labels={"a": "Alpha"})
        assert "**Alpha:** 1" in out


def test_estimate_tokens_scales_with_length():
    assert trim.estimate_tokens("a" * 400) == 100
    assert trim.estimate_tokens("a") >= 1


# ---------------------------------------------------------------------------
# http.py
# ---------------------------------------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestGetJson:
    async def test_success_and_cache_hit(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        http_module._client = _mock_client(handler)
        first = await http_module.get_json("test", "https://example.test/x")
        second = await http_module.get_json("test", "https://example.test/x")

        assert first == {"ok": True}
        assert second == {"ok": True}
        assert len(calls) == 1, "second call should have been served from cache"

    async def test_cache_false_bypasses_cache(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"n": len(calls)})

        http_module._client = _mock_client(handler)
        await http_module.get_json("test", "https://example.test/y", cache=False)
        await http_module.get_json("test", "https://example.test/y", cache=False)
        assert len(calls) == 2

    async def test_retries_5xx_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        http_module._client = _mock_client(handler)
        result = await http_module.get_json(
            "test", "https://example.test/retry", cache=False
        )
        assert result == {"ok": True}
        assert len(calls) == 3

    async def test_no_retry_on_4xx(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(404, text="not found")

        http_module._client = _mock_client(handler)
        with pytest.raises(SourceError) as exc_info:
            await http_module.get_json("test", "https://example.test/missing", cache=False)
        assert exc_info.value.source == "test"
        assert len(calls) == 1, "4xx must not be retried"

    async def test_exhausted_retries_raise_source_error(self):
        def handler(request):
            return httpx.Response(500)

        http_module._client = _mock_client(handler)
        with pytest.raises(SourceError):
            await http_module.get_json("test", "https://example.test/dead", cache=False)

    async def test_corrupt_cache_file_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(http_module, "CACHE_DIR", tmp_path)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        http_module._client = _mock_client(handler)
        # Pre-seed a corrupt cache file at the exact key get_json will use.
        key = http_module._cache_key("GET", "https://example.test/z", None, None)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"{key}.json").write_text("{not valid json", encoding="utf-8")

        result = await http_module.get_json("test", "https://example.test/z")
        assert result == {"ok": True}, "a broken cache must never break a query"


# ---------------------------------------------------------------------------
# sources/orcs.py
# ---------------------------------------------------------------------------


class TestOrcsAccessKey:
    async def test_missing_key_raises_clear_source_error(self, monkeypatch):
        monkeypatch.delenv(orcs_module.ACCESS_KEY_ENV_VAR, raising=False)
        with pytest.raises(SourceError) as exc_info:
            await orcs_module.crispr_screen_hits("MYC")
        assert exc_info.value.source == "orcs"
        assert "ORCS_ACCESS_KEY" in exc_info.value.message


class TestGeneIdResolution:
    """gene_ids.py, backed by the real bundled NCBI reference table."""

    def test_resolves_known_official_symbol(self):
        assert gene_ids_module.resolve_entrez_id("MYC") == 4609

    def test_case_insensitive(self):
        assert gene_ids_module.resolve_entrez_id("myc") == 4609

    def test_unknown_symbol_raises_source_error(self):
        with pytest.raises(SourceError) as exc_info:
            gene_ids_module.resolve_entrez_id("NOT-A-REAL-GENE-XYZ123")
        assert exc_info.value.source == "orcs"


class TestOrcsParsing:
    """Field names and shapes below are verified live (2026-09-02, real
    ORCS_ACCESS_KEY) — see sources/orcs.py module docstring for the full
    discovery process, including every symbol-lookup approach that failed.
    """

    def test_as_records_list_shape(self):
        # The real, confirmed shape: format=json returns a plain list.
        assert orcs_module._as_records([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]

    def test_as_records_dict_of_dicts_shape(self):
        # Defensive fallback only — not the shape ORCS actually returns.
        data = {"1": {"a": 1}, "2": {"a": 2}}
        assert orcs_module._as_records(data) == [{"a": 1}, {"a": 2}]

    def test_as_records_error_shape_raises(self):
        # Real shape seen live for a malformed request:
        # {"STATUS":"ERROR","MESSAGE":["..."]}
        data = {"STATUS": "ERROR", "MESSAGE": ["bad access key"]}
        with pytest.raises(SourceError) as exc_info:
            orcs_module._as_records(data)
        assert "bad access key" in exc_info.value.message

    def test_normalize_screen_hit_joins_gene_and_screen_records(self):
        # Real field split, verified live: hit/score fields come from
        # /gene/<id>/, cell line/screen type/phenotype from /screens/.
        gene_row = {
            "OFFICIAL_SYMBOL": "MYC",
            "SCREEN_ID": "16",
            "SCORE.1": "213.612",
            "HIT": "YES",
        }
        screen = {
            "CELL_LINE": "HCT 116",
            "CELL_TYPE": "Colorectal Cancer Cell Line",
            "SCREEN_TYPE": "Negative Selection",
            "PHENOTYPE": "cell proliferation",
            "THROUGHPUT": "High Throughput",
            "SOURCE_ID": "26627737",
            "SOURCE_TYPE": "pubmed",
        }
        normalized = orcs_module._normalize_screen_hit(gene_row, screen)
        assert normalized["gene_symbol"] == "MYC"
        assert normalized["hit"] is True
        assert normalized["cell_line"] == "HCT 116"
        assert normalized["screen_type"] == "Negative Selection"
        assert normalized["publication"] == "26627737"

    def test_normalize_screen_hit_missing_screen_metadata(self):
        # A gene record whose SCREEN_ID has no match in /screens/ must not
        # crash — just render with the screen-side fields blank.
        normalized = orcs_module._normalize_screen_hit({"HIT": "NO"}, None)
        assert normalized["hit"] is False
        assert normalized["cell_line"] is None

    def test_publication_blank_when_source_is_not_pubmed(self):
        normalized = orcs_module._normalize_screen_hit(
            {"HIT": "YES"}, {"SOURCE_ID": "some-dataset-id", "SOURCE_TYPE": "dataset"}
        )
        assert normalized["publication"] is None


class TestNormalizeCellLine:
    def test_strips_punctuation_and_case(self):
        # Verified live: ORCS stores "K-562", users write "K562".
        assert orcs_module._normalize_cell_line("K562") == orcs_module._normalize_cell_line(
            "K-562"
        )
        assert orcs_module._normalize_cell_line("hela") == orcs_module._normalize_cell_line(
            "HeLa"
        )


class TestCrisprScreenHits:
    async def test_joins_gene_records_with_screen_metadata(self, monkeypatch):
        monkeypatch.setenv(orcs_module.ACCESS_KEY_ENV_VAR, "test-key")

        async def fake_get_json(source, url, params=None, **kwargs):
            if url.endswith("/gene/4609/"):
                return [
                    {
                        "SCREEN_ID": "16",
                        "OFFICIAL_SYMBOL": "MYC",
                        "SCORE.1": "213.612",
                        "HIT": "YES",
                    },
                    {"SCREEN_ID": "17", "OFFICIAL_SYMBOL": "MYC", "SCORE.1": "1.0", "HIT": "NO"},
                ]
            if url.endswith("/screens/"):
                return [
                    {
                        "SCREEN_ID": "16",
                        "CELL_LINE": "HCT 116",
                        "SCREEN_TYPE": "Negative Selection",
                        "PHENOTYPE": "cell proliferation",
                        "THROUGHPUT": "High Throughput",
                        "SOURCE_ID": "26627737",
                        "SOURCE_TYPE": "pubmed",
                    },
                    {"SCREEN_ID": "17", "CELL_LINE": "HeLa"},
                ]
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr(orcs_module, "get_json", fake_get_json)
        result = await orcs_module.crispr_screen_hits("MYC")

        assert result["total_screens_tested"] == 2
        assert len(result["hits"]) == 2
        hit = next(h for h in result["hits"] if h["screen_id"] == "16")
        assert hit["hit"] is True
        assert hit["cell_line"] == "HCT 116"
        assert hit["publication"] == "26627737"

    async def test_unknown_gene_raises_before_any_request(self, monkeypatch):
        async def fail_if_called(*args, **kwargs):
            raise AssertionError("should not make a request for an unresolvable gene")

        monkeypatch.setattr(orcs_module, "get_json", fail_if_called)
        with pytest.raises(SourceError):
            await orcs_module.crispr_screen_hits("NOT-A-REAL-GENE-XYZ123")


class TestScreensInCellLine:
    async def test_matches_ignoring_hyphen_and_case(self, monkeypatch):
        monkeypatch.setenv(orcs_module.ACCESS_KEY_ENV_VAR, "test-key")

        async def fake_get_json(source, url, params=None, **kwargs):
            return [
                {
                    "SCREEN_ID": "5",
                    "CELL_LINE": "K-562",
                    "SCREEN_TYPE": "Negative Selection",
                    "SCREEN_NAME": "2-PMID25307932",
                    "SOURCE_ID": "25307932",
                    "SOURCE_TYPE": "pubmed",
                },
                {"SCREEN_ID": "99", "CELL_LINE": "HeLa"},
            ]

        monkeypatch.setattr(orcs_module, "get_json", fake_get_json)
        result = await orcs_module.screens_in_cell_line("k562")
        assert len(result["screens"]) == 1
        assert result["screens"][0]["screen_id"] == "5"
        assert result["screens"][0]["publication"] == "25307932"


# ---------------------------------------------------------------------------
# sources/census.py — query logic against fixture parquet tables
# ---------------------------------------------------------------------------


@pytest.fixture
def cell_counts_fixture(tmp_path):
    import pandas as pd

    path = census_module.CACHE_DIR / "census_cell_counts.parquet"
    census_module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "tissue_general": "lung",
            "tissue": "lung",
            "cell_type": "type II pneumocyte",
            "disease": "normal",
            "assay": "10x 3' v3",
            "cell_count": 5000,
            "dataset_ids": ["ds-1"],
        },
        {
            "tissue_general": "lung",
            "tissue": "lung",
            "cell_type": "type II pneumocyte",
            "disease": "COVID-19",
            "assay": "10x 3' v3",
            "cell_count": 300,
            "dataset_ids": ["ds-2"],
        },
        {
            "tissue_general": "lung",
            "tissue": "lung",
            "cell_type": "macrophage",
            "disease": "normal",
            "assay": "10x 3' v3",
            "cell_count": 2000,
            "dataset_ids": ["ds-1", "ds-3"],
        },
        {
            "tissue_general": "blood",
            "tissue": "blood",
            "cell_type": "T cell",
            "disease": "normal",
            "assay": "10x 3' v3",
            "cell_count": 9000,
            "dataset_ids": ["ds-4"],
        },
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


@pytest.fixture
def expression_cube_fixture(tmp_path):
    import pandas as pd

    path = census_module.CACHE_DIR / "census_expression_cube.parquet"
    census_module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "tissue_general": "lung",
            "cell_type": "type II pneumocyte",
            "feature_name": "SFTPC",
            "mean_expression": 4.2,
            "pct_expressing": 91.0,
            "n_cells_sampled": 800,
        },
        {
            "tissue_general": "lung",
            "cell_type": "macrophage",
            "feature_name": "SFTPC",
            "mean_expression": 0.1,
            "pct_expressing": 6.0,
            "n_cells_sampled": 500,
        },
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


@pytest.fixture
def datasets_fixture(tmp_path):
    import pandas as pd

    path = census_module.CACHE_DIR / "census_datasets.parquet"
    census_module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "dataset_id": "ds-1",
            "dataset_title": "Healthy lung atlas",
            "collection_name": "Human Lung Cell Atlas",
            "dataset_total_cell_count": 50000,
        },
        {
            "dataset_id": "ds-4",
            "dataset_title": "PBMC reference",
            "collection_name": "Immune Cell Atlas",
            "dataset_total_cell_count": 20000,
        },
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


class TestMissingCensusTables:
    """What a user sees before the precompute jobs have been run."""

    async def test_missing_table_names_the_file_and_how_to_build_it(self, monkeypatch):
        monkeypatch.delenv(census_module.CUBE_BASE_URL_ENV_VAR, raising=False)
        with pytest.raises(SourceError) as exc_info:
            await census_module.find_cells(tissue="lung")
        message = exc_info.value.message
        assert exc_info.value.source == "census"
        assert "census_cell_counts.parquet" in message
        assert "build_cell_counts" in message

    async def test_no_request_is_made_when_no_base_url_is_configured(self, monkeypatch):
        # Without a configured host there is nothing to fetch, so the error
        # must come back immediately rather than after a doomed round-trip.
        monkeypatch.delenv(census_module.CUBE_BASE_URL_ENV_VAR, raising=False)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("must not attempt a download with no base URL set")

        monkeypatch.setattr(census_module.httpx, "AsyncClient", fail_if_called)
        with pytest.raises(SourceError):
            await census_module.find_cells(tissue="lung")


class TestFindCells:
    async def test_no_filters_aggregates_everything(self, cell_counts_fixture):
        result = await census_module.find_cells()
        assert result["total_cells"] == 5000 + 300 + 2000 + 9000
        assert len(result["rows"]) == 3  # 3 distinct (tissue, cell_type) pairs

    async def test_tissue_filter_is_case_insensitive(self, cell_counts_fixture):
        result = await census_module.find_cells(tissue="LUNG")
        assert result["total_cells"] == 5000 + 300 + 2000

    async def test_combined_filters(self, cell_counts_fixture):
        result = await census_module.find_cells(tissue="lung", disease="normal")
        assert result["total_cells"] == 5000 + 2000
        cell_types = {r["cell_type"] for r in result["rows"]}
        assert cell_types == {"type II pneumocyte", "macrophage"}

    async def test_no_match_returns_empty_not_error(self, cell_counts_fixture):
        result = await census_module.find_cells(tissue="nonexistent")
        assert result["total_cells"] == 0
        assert result["rows"] == []

    async def test_dataset_ids_are_unioned_and_capped(self, cell_counts_fixture):
        result = await census_module.find_cells(tissue="lung", cell_type="macrophage")
        assert set(result["dataset_ids"]) == {"ds-1", "ds-3"}


class TestExpressionByCellType:
    async def test_returns_rows_sorted_by_mean_expression(
        self, expression_cube_fixture, cell_counts_fixture
    ):
        result = await census_module.expression_by_cell_type("SFTPC", "lung")
        assert result["rows"][0]["cell_type"] == "type II pneumocyte"
        assert result["rows"][0]["mean_expression"] == pytest.approx(4.2)
        assert "sampled estimate" in result["method"]

    async def test_gene_case_insensitive(self, expression_cube_fixture, cell_counts_fixture):
        result = await census_module.expression_by_cell_type("sftpc", "lung")
        assert len(result["rows"]) == 2

    async def test_gene_not_in_cube_returns_empty_rows_not_error(
        self, expression_cube_fixture, cell_counts_fixture
    ):
        result = await census_module.expression_by_cell_type("NOTAGENE", "lung")
        assert result["rows"] == []


class TestCensusDatasets:
    async def test_matches_title_or_collection(self, datasets_fixture):
        by_title = await census_module.census_datasets("lung atlas")
        assert by_title["total_matches"] == 1
        assert by_title["rows"][0]["dataset_id"] == "ds-1"

        by_collection = await census_module.census_datasets("immune")
        assert by_collection["rows"][0]["dataset_id"] == "ds-4"

    async def test_no_match(self, datasets_fixture):
        result = await census_module.census_datasets("nonexistent-xyz")
        assert result["rows"] == []


# ---------------------------------------------------------------------------
# server.py — identity, gene_evidence partial failure, and size regression
# ---------------------------------------------------------------------------


class TestServerIdentity:
    """What a client sees in the MCP `initialize` handshake."""

    def test_reports_a_real_version(self):
        # MCPServer defaults `version` to "", which clients render as an
        # unversioned server. Caught by an end-to-end stdio handshake, so
        # pinned here.
        import bio_mcp

        assert server_module.server.version == bio_mcp.__version__
        assert server_module.server.version, "server must not report an empty version"

    async def test_registers_exactly_the_six_scoped_tools(self):
        # scope.md section 2: "Tools | Six or fewer, one of which is the
        # cross-source join", and section 3 excludes "A seventh tool".
        names = [t.name for t in await server_module.server.list_tools()]
        assert names == [
            "find_cells",
            "expression_by_cell_type",
            "census_datasets",
            "crispr_screen_hits",
            "screens_in_cell_line",
            "gene_evidence",
        ]


class TestGeneEvidenceDegradation:
    async def test_both_sources_succeed(self, monkeypatch):
        async def fake_census(gene, tissue):
            return {
                "gene": gene,
                "tissue": tissue,
                "rows": [
                    {"cell_type": "T cell", "mean_expression": 1.0, "pct_expressing": 50.0}
                ],
                "n_cell_types_with_signal": 1,
                "n_cell_types_in_tissue": 1,
                "method": "sampled estimate",
            }

        async def fake_orcs(gene):
            return {
                "gene": gene,
                "hits": [
                    {
                        "gene_symbol": gene,
                        "cell_line": "K562",
                        "screen_type": "KO",
                        "score": 0.01,
                        "phenotype": "essential",
                        "hit": True,
                    }
                ],
                "total_screens_tested": 3,
            }

        monkeypatch.setattr(server_module.census, "expression_by_cell_type", fake_census)
        monkeypatch.setattr(server_module.orcs, "crispr_screen_hits", fake_orcs)

        out = await server_module.gene_evidence("MYC", "blood")
        assert "Expression" in out
        assert "CRISPR screen hits" in out
        assert "K562" in out
        assert "unavailable" not in out

    async def test_orcs_down_census_still_answers(self, monkeypatch):
        async def fake_census(gene, tissue):
            return {
                "gene": gene,
                "tissue": tissue,
                "rows": [{"cell_type": "T cell", "mean_expression": 1.0, "pct_expressing": 50.0}],
                "n_cell_types_with_signal": 1,
                "n_cell_types_in_tissue": 1,
                "method": "sampled estimate",
            }

        async def fake_orcs_down(gene):
            raise SourceError("orcs", "connection refused")

        monkeypatch.setattr(server_module.census, "expression_by_cell_type", fake_census)
        monkeypatch.setattr(server_module.orcs, "crispr_screen_hits", fake_orcs_down)

        out = await server_module.gene_evidence("MYC", "blood")
        assert "T cell" in out, "Census half must still be present"
        assert "orcs unavailable: connection refused" in out
        # This is the Phase 3 gate from scope.md: partial failure names the
        # failing source, never raises.

    async def test_census_down_orcs_still_answers(self, monkeypatch):
        async def fake_census_down(gene, tissue):
            raise SourceError("census", "timed out")

        async def fake_orcs(gene):
            return {
                "gene": gene,
                "hits": [
                    {
                        "gene_symbol": gene,
                        "cell_line": "K562",
                        "screen_type": "KO",
                        "score": 0.01,
                        "phenotype": "essential",
                        "hit": True,
                    }
                ],
                "total_screens_tested": 3,
            }

        monkeypatch.setattr(server_module.census, "expression_by_cell_type", fake_census_down)
        monkeypatch.setattr(server_module.orcs, "crispr_screen_hits", fake_orcs)

        out = await server_module.gene_evidence("MYC", "blood")
        assert "K562" in out
        assert "census unavailable: timed out" in out

    async def test_both_sources_down_returns_string_not_exception(self, monkeypatch):
        async def fake_census_down(gene, tissue):
            raise SourceError("census", "down")

        async def fake_orcs_down(gene):
            raise SourceError("orcs", "down")

        monkeypatch.setattr(server_module.census, "expression_by_cell_type", fake_census_down)
        monkeypatch.setattr(server_module.orcs, "crispr_screen_hits", fake_orcs_down)

        out = await server_module.gene_evidence("MYC", "blood")
        assert isinstance(out, str)
        assert "census unavailable" in out
        assert "orcs unavailable" in out


class TestSizeRegression:
    """Guards the whole premise of the server: no tool response balloons."""

    async def test_gene_evidence_stays_under_budget_with_large_inputs(self, monkeypatch):
        async def fake_census(gene, tissue):
            return {
                "gene": gene,
                "tissue": tissue,
                "rows": [
                    {
                        "cell_type": f"cell type number {i} with a fairly long descriptive name",
                        "mean_expression": 1.2345,
                        "pct_expressing": 45.6789,
                    }
                    for i in range(200)
                ],
                "n_cell_types_with_signal": 200,
                "n_cell_types_in_tissue": 200,
                "method": "sampled estimate (contiguous block sample of Census)",
            }

        async def fake_orcs(gene):
            return {
                "gene": gene,
                "hits": [
                    {
                        "gene_symbol": gene,
                        "cell_line": f"cell line {i} with a moderately long name",
                        "screen_type": "Negative Selection Screen",
                        "score": 0.00012345,
                        "phenotype": "a fairly long phenotype description here",
                        "hit": True,
                    }
                    for i in range(200)
                ],
                "total_screens_tested": 200,
            }

        monkeypatch.setattr(server_module.census, "expression_by_cell_type", fake_census)
        monkeypatch.setattr(server_module.orcs, "crispr_screen_hits", fake_orcs)

        out = await server_module.gene_evidence("MYC", "blood")
        assert trim.estimate_tokens(out) <= trim.BUDGETS["max_tokens_per_tool"]

    @pytest.mark.parametrize(
        "budget_key", ["find_cells", "expression_by_cell_type", "census_datasets"]
    )
    def test_table_alone_respects_hard_ceiling(self, budget_key):
        rows = [{"name": f"row {i} with some padding text here", "count": i} for i in range(500)]
        out = trim.table(rows, [("name", "Name"), ("count", "Count")])
        clipped = trim.clip(out, trim.BUDGETS[budget_key])
        assert trim.estimate_tokens(clipped) <= trim.BUDGETS["max_tokens_per_tool"]
