# ── Build stage ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency metadata first for layer caching.
# Installing only the declared deps means source changes won't
# invalidate this expensive layer.
COPY pyproject.toml ./

RUN uv venv /app/.venv \
    && uv pip install --no-cache-dir --python /app/.venv/bin/python \
       "fastmcp>=2.0" "httpx>=0.27" "pydantic-settings>=2.0" "aiosqlite>=0.20"

# ── Runtime stage ─────────────────────────────────────────────
FROM python:3.12-slim

# curl is needed for the docker-compose healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Security: run as non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY pyproject.toml ./
COPY deep_research/ ./deep_research/

# Ensure the venv's Python is on PATH
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# SQLite volume mount point
VOLUME ["/data"]

# HTTP transport port
EXPOSE 8000

USER appuser

CMD ["python", "-m", "deep_research"]
