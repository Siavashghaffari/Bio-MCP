# bio-mcp — stdio MCP server.
#
# Build:  docker build -t bio-mcp .
# Run:    docker run --rm -i -e ORCS_ACCESS_KEY=... \
#             -v bio-mcp-cache:/home/app/.cache/bio-mcp bio-mcp
#
# The `-i` is required: the MCP client talks JSON-RPC over stdin/stdout.
# The volume persists the precomputed Census tables between runs so they
# are downloaded only once.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /dist

FROM python:3.12-slim
# Run as a non-root user; give it a home for the ~/.cache/bio-mcp table cache.
RUN useradd --create-home --uid 1000 app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER app
WORKDIR /home/app
ENV BIO_MCP_CACHE_DIR=/home/app/.cache/bio-mcp
ENTRYPOINT ["python", "-m", "bio_mcp"]
