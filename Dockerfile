# Build stage
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc AS builder

WORKDIR /build

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

# LICENSE and LICENSES/ are not optional extras here: pyproject force-includes
# them into the wheel, so the build fails without them. A published image
# redistributes this code and the third-party code under LICENSES/, and both
# licences require their texts to travel with it.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY LICENSES/ LICENSES/
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

# Apply Alpine security patches released since the base image was built. The
# FROM above is digest-pinned for reproducibility, which also pins its known
# CVEs until upstream cuts a new image: libssl3/libcrypto3 3.5.7-r0 shipped
# with CVE-2026-14456 open. A published image is worth more patched than
# bit-identical, so the trade is made deliberately here rather than left
# implicit.
RUN apk --no-cache upgrade

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
