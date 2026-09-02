"""`python -m bio_mcp` — start the stdio MCP server.

Same entry point as the `bio-mcp` console script (pyproject `[project.scripts]`)
and `bio_mcp.server:main`; provided so bundlers that expect a module target
(MCPB/DXT, Smithery) can launch the server without depending on the installed
script shim.
"""

from __future__ import annotations

from bio_mcp.server import main

if __name__ == "__main__":
    main()
