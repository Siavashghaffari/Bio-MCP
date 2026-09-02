"""The MCPB bundle's entry shim (`mcpb_entry.py` at the repo root).

Not part of the installed package — it only runs inside a packed `.mcpb` — but
it is the first thing that executes when someone double-clicks the extension,
so its two guards are worth pinning. Both bugs it defends against were found
by actually running a packed bundle, and both surfaced as unreadable
`ModuleNotFoundError`s naming private submodules.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_shim():
    """Import mcpb_entry.py by path, without running it."""
    spec = importlib.util.spec_from_file_location("mcpb_entry", REPO_ROOT / "mcpb_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shim():
    return _load_shim()


class TestVendoredPythonVersion:
    """Reads the CPython tag off the vendored binary wheels in lib/."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("_pydantic_core.cp312-win_amd64.pyd", "3.12"),
            ("_pydantic_core.cpython-312-x86_64-linux-gnu.so", "3.12"),
            ("interval.cp310-win_amd64.pyd", "3.10"),
            ("lib.cpython-39-darwin.so", "3.9"),
        ],
    )
    def test_reads_tag_from_extension_filename(
        self, shim, tmp_path, monkeypatch, filename, expected
    ):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / filename).touch()
        monkeypatch.setattr(shim, "BUNDLE_LIB", tmp_path)
        assert shim._vendored_python_version() == expected

    def test_ignores_stable_abi_wheels(self, shim, tmp_path, monkeypatch):
        # abi3 wheels are valid across versions, so they must not be read as a
        # version constraint.
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "_core.abi3.so").touch()
        monkeypatch.setattr(shim, "BUNDLE_LIB", tmp_path)
        assert shim._vendored_python_version() is None

    def test_no_lib_directory_is_not_an_error(self, shim, tmp_path, monkeypatch):
        monkeypatch.setattr(shim, "BUNDLE_LIB", tmp_path / "does-not-exist")
        assert shim._vendored_python_version() is None


class TestCheckRuntime:
    def test_mismatch_exits_with_an_actionable_message(self, shim, monkeypatch):
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        monkeypatch.setattr(shim, "_vendored_python_version", lambda: "2.7")

        with pytest.raises(SystemExit) as exc_info:
            shim._check_runtime()

        message = str(exc_info.value)
        assert "2.7" in message, "must name the version the bundle was built for"
        assert running in message, "must name the version it was launched with"
        assert "install from source" in message.lower()

    def test_matching_version_passes(self, shim, monkeypatch):
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        monkeypatch.setattr(shim, "_vendored_python_version", lambda: running)
        shim._check_runtime()  # must not raise

    def test_no_native_extensions_passes(self, shim, monkeypatch):
        # A pure-Python bundle has no version constraint to enforce.
        monkeypatch.setattr(shim, "_vendored_python_version", lambda: None)
        shim._check_runtime()  # must not raise


class TestBootstrap:
    def test_adds_pywin32_paths_that_exist(self, shim, tmp_path, monkeypatch):
        # pip install --target never runs pywin32.pth, so the shim re-creates
        # the paths it would have added. `mcp` needs these on Windows.
        for relative in ("win32", "win32/lib", "pythonwin"):
            (tmp_path / relative).mkdir(parents=True)
        monkeypatch.setattr(shim, "BUNDLE_LIB", tmp_path)
        monkeypatch.setattr(sys, "path", list(sys.path))

        shim._bootstrap()

        assert sys.path[0] == str(tmp_path)
        for relative in ("win32", "win32/lib", "pythonwin"):
            assert str(tmp_path / relative) in sys.path

    def test_absent_pywin32_paths_are_skipped(self, shim, tmp_path, monkeypatch):
        # The same shim ships in macOS and Linux bundles, where none of those
        # directories exist.
        monkeypatch.setattr(shim, "BUNDLE_LIB", tmp_path)
        monkeypatch.setattr(sys, "path", list(sys.path))

        shim._bootstrap()

        assert sys.path[0] == str(tmp_path)
        # Only entries the shim itself added are in scope here; the ambient
        # sys.path on a Windows dev machine already contains real pywin32 dirs.
        added_under_bundle = [e for e in sys.path if e.startswith(str(tmp_path))]
        assert added_under_bundle == [str(tmp_path)]
