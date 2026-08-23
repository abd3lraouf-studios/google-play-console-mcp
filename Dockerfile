# Build stage
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc AS builder

WORKDIR /build

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# Sync straight from the lockfile into the final runtime venv path (so
# shebangs point to /app/.venv/bin/python), instead of building a wheel and
# letting `uv pip install` re-resolve deps fresh — that re-resolution doesn't
# see uv.lock's already-decided prerelease pins and can fail to reproduce it.
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-editable

# Runtime stage
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

LABEL org.opencontainers.image.source="https://github.com/abd3lraouf-studios/google-play-console-mcp"
LABEL org.opencontainers.image.url="https://github.com/abd3lraouf-studios/google-play-console-mcp"
LABEL org.opencontainers.image.documentation="https://abd3lraouf-studios.github.io/google-play-console-mcp"
LABEL org.opencontainers.image.description="MCP server for Google Play Developer API — deploy apps, manage releases, reviews, and more"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.title="google-play-console-mcp"

# Security hardening: non-root user, no shell
RUN addgroup -S mcp && \
    adduser -S -G mcp -h /app -s /sbin/nologin mcp

WORKDIR /app

# Copy virtual environment from builder (already at /app/.venv)
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=stdio \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

USER mcp

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD if [ "$MCP_TRANSPORT" = "streamable-http" ]; then wget -qO- http://localhost:${MCP_PORT:-8000}/health || exit 1; else exit 0; fi

ENTRYPOINT ["google-play-console-mcp"]
