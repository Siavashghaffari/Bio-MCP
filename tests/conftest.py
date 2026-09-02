from __future__ import annotations

import pandas as pd
import pytest

import bio_mcp.http as http_module
import bio_mcp.sources.census as census_module


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own empty disk cache and in-memory table cache.

    Without this, tests would share ~/.cache/bio-mcp and bleed state into
    each other (and into a real cache a developer has on disk).
    """
    monkeypatch.setattr(http_module, "CACHE_DIR", tmp_path / "http_cache")
    monkeypatch.setattr(census_module, "CACHE_DIR", tmp_path / "census_cache")
    census_module._reset_cache_for_tests()
    yield
    census_module._reset_cache_for_tests()


def write_cell_counts_fixture(path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


def write_expression_cube_fixture(path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


def write_datasets_fixture(path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


@pytest.fixture(autouse=True)
async def _reset_http_client():
    """Ensure each test starts with no shared httpx client from a prior test."""
    http_module._client = None
    yield
    if http_module._client is not None:
        try:
            await http_module._client.aclose()
        except Exception:
            pass
        http_module._client = None
