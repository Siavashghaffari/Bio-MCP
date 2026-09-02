"""Source modules: one per upstream database.

Each module is async, takes primitives, returns plain dicts, and raises
`bio_mcp.errors.SourceError` on failure. Never markdown, never an import
from `bio_mcp.server`. See design.md section 4.
"""

from __future__ import annotations
