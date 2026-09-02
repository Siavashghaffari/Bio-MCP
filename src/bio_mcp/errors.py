"""The one error type source modules are allowed to raise.

See design.md section 3 ("One error type"). Everything in `sources/*.py` either
returns a plain dict or raises `SourceError`. Nothing else propagates out of a
source module — that is what lets `server.py` degrade gracefully with
`asyncio.gather(return_exceptions=True)` and name the failing source in the
answer instead of crashing the whole call.
"""

from __future__ import annotations


class SourceError(Exception):
    """An upstream source failed in a way callers should degrade around.

    Attributes:
        source: short machine name of the upstream, e.g. "census" or "orcs".
        message: human-readable reason, safe to show to an end user/agent.
    """

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")
