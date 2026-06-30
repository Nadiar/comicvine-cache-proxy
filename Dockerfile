# syntax=docker/dockerfile:1
FROM python:3.13-slim

WORKDIR /app

# pysqlite3-binary ships pre-built manylinux wheels for all supported Python
# versions, so no compiler or system SQLite headers are needed.
# It bundles its own modern SQLite with FTS5 always enabled.

# ── dependency layer ─────────────────────────────────────────────────────────
# Copy only the package manifest so this layer is only invalidated when
# dependencies change (not on every source edit).
# A stub __init__.py lets setuptools resolve the package without real source.
# The BuildKit pip cache mount (~/.cache/pip) persists across builds on this
# host so pysqlite3-binary is only compiled once — even if this layer is
# invalidated — and subsequent builds reuse the cached wheel.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel && \
    mkdir -p src/cvproxy && touch src/cvproxy/__init__.py && \
    pip install . && \
    rm src/cvproxy/__init__.py

# ── source layer ─────────────────────────────────────────────────────────────
# This COPY invalidates only when application code changes.
# --no-deps skips all the heavy work above; it just installs the package itself.
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

# Create data directory
RUN mkdir -p /data/images

EXPOSE 8585

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8585/health')"

CMD ["cvproxy"]
