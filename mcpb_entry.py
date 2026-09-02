"""Entry point for the packed MCPB/DXT bundle. Not used by any other install.

A bundle vendors its dependencies with `pip install --target lib` (see the
release workflow), and that install mode has two sharp edges this shim exists
to file down.

**`.pth` files are never processed outside `site-packages`.** Setting
`PYTHONPATH=lib` is therefore not equivalent to installing: any dependency
that relies on a `.pth` to extend `sys.path` is simply unimportable.
`pywin32` is exactly such a dependency, and `mcp` requires it on Windows
(`pywin32>=311; sys_platform == 'win32'`). Its `pywin32.pth` appends `win32/`,
`win32/lib/` and `pythonwin/`; without that, `mcp.server.stdio` dies on
`import pywintypes` before the server speaks a byte of JSON-RPC.

**Vendored binary wheels are tied to one CPython minor version.** `pandas`,
`pyarrow` and `pydantic-core` ship `cp312`-style extensions, so a bundle built
for 3.12 cannot run on 3.11. Left alone that surfaces as a bare
`ModuleNotFoundError` naming some private submodule, which tells the user
nothing. `_check_runtime` turns it into a sentence they can act on.

Doing all of this here rather than through `PYTHONPATH` in manifest.json also
sidesteps the fact that a manifest is one file while the path separator is not
(`;` on Windows, `:` elsewhere). Every branch is guarded by an existence check,
so the same shim is correct on a macOS or Linux bundle where none of the
pywin32 directories exist.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BUNDLE_LIB = Path(__file__).resolve().parent / "lib"

# Directories pywin32.pth would have added, in its order.
_PYWIN32_PATHS = ("win32", "win32/lib", "pythonwin")
# pywintypes<ver>.dll and friends live here; the .pyd extensions load them, so
# the directory has to be on the DLL search path too.
_PYWIN32_DLLS = "pywin32_system32"

# Matches the interpreter tag in a vendored extension filename, e.g.
# "_pydantic_core.cp312-win_amd64.pyd" or
# "_pydantic_core.cpython-312-x86_64-linux-gnu.so". Deliberately does not match
# stable-ABI ("abi3") wheels, which are valid across versions.
_ABI_TAG = re.compile(r"\.cp(?:ython-)?(\d)(\d+)-")


def _vendored_python_version() -> str | None:
    """The CPython version this bundle's binary wheels were built for."""
    for pattern in ("*/*.pyd", "*/*.so", "*/*/*.pyd", "*/*/*.so"):
        for path in BUNDLE_LIB.glob(pattern):
            match = _ABI_TAG.search(path.name)
            if match:
                return f"{match.group(1)}.{match.group(2)}"
    return None


def _check_runtime() -> None:
    vendored = _vendored_python_version()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if vendored is None or vendored == running:
        return
    sys.exit(
        f"bio-mcp: this extension bundles binary dependencies for Python "
        f"{vendored}, but your MCP client launched it with Python {running}.\n"
        f"Install the bio-mcp bundle built for Python {running}, or install "
        f"from source instead:\n"
        f"    pip install git+https://github.com/Siavashghaffari/Bio-MCP\n"
        f"then point the client at the `bio-mcp` command."
    )


def _bootstrap() -> None:
    if BUNDLE_LIB.is_dir():
        sys.path.insert(0, str(BUNDLE_LIB))

    for relative in _PYWIN32_PATHS:
        path = BUNDLE_LIB / relative
        if path.is_dir():
            sys.path.append(str(path))

    dll_dir = BUNDLE_LIB / _PYWIN32_DLLS
    if dll_dir.is_dir():
        os.environ["PATH"] = f"{dll_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:  # Windows only
            add_dll_directory(str(dll_dir))


def run() -> None:
    """Check the runtime, fix up sys.path, then start the server."""
    _check_runtime()
    _bootstrap()
    # Imported here, not at module scope: bio_mcp and its dependencies only
    # become importable once _bootstrap() has put lib/ on sys.path.
    from bio_mcp.server import main

    main()


if __name__ == "__main__":
    run()
