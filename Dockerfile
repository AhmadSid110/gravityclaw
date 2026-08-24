FROM docker.io/library/python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd -r gravityclaw && useradd --no-log-init -r -g gravityclaw -m gravityclaw

# ─── Build the web console ────────────────────────────────────────────────────
FROM docker.io/library/node:22-slim AS frontend

WORKDIR /build
COPY web/package.json web/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install
COPY web/ ./
RUN npm run build

# ─── Install Python package ───────────────────────────────────────────────────
FROM base AS backend

WORKDIR /opt/gravityclaw

# Copy built frontend into the expected location before pip install
COPY --from=frontend /build/dist web/dist/

# Install the package (hatch bundles web/dist into the wheel)
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

# ─── Runtime image ────────────────────────────────────────────────────────────
FROM base

COPY --from=backend /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=backend /usr/local/bin/gravityclaw* /usr/local/bin/

# Create data directory
RUN mkdir -p /data && chown gravityclaw:gravityclaw /data

USER gravityclaw
WORKDIR /data

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health')" || exit 1

ENTRYPOINT ["gravityclaw-server"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--log-level", "info"]
