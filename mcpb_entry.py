"""Entry point for the packed MCPB/DXT bundle. Not used by any other install.

A bundle vendors its dependencies with `pip install --target lib` (see README
Option 1), and that install mode has one sharp edge: **`.pth` files are never
processed outside `site-packages`.** Setting `PYTHONPATH=lib` is therefore not
equivalent to installing — any dependency that relies on a `.pth` to extend
`sys.path` is simply unimportable.

`pywin32` is exactly such a dependency, and `mcp` requires it on Windows
(`pywin32>=311; sys_platform == 'win32'`). Its `pywin32.pth` appends `win32/`,
`win32/lib/` and `pythonwin/`; without that, `mcp.server.stdio` dies on
`import pywintypes` before the server ever speaks a byte of JSON-RPC.

So this shim re-creates that path setup itself, then hands off to the normal
`bio_mcp.server:main`. Doing it here rather than via `PYTHONPATH` in
manifest.json also sidesteps the fact that a manifest is one file while the
path separator is not (`;` on Windows, `:` elsewhere). Every branch below is
guarded by an existence check, so the same shim is correct on a macOS or Linux
bundle where none of these directories exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BUNDLE_LIB = Path(__file__).resolve().parent / "lib"

# Directories pywin32.pth would have added, in its order.
_PYWIN32_PATHS = ("win32", "win32/lib", "pythonwin")
# pywintypes<ver>.dll and friends live here; they are loaded by the .pyd
# extensions, so the directory has to be on the DLL search path too.
_PYWIN32_DLLS = "pywin32_system32"


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


_bootstrap()

from bio_mcp.server import main  # noqa: E402  - must follow _bootstrap()

if __name__ == "__main__":
    main()
