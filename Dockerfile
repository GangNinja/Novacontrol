# ============================================
# NovaControl — Multi-stage Dockerfile
# ============================================

# --- Stage 1: Builder ---
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install \
    -e ".[database,redis,ai]"

# --- Stage 2: Runtime ---
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source.
# docs/ is deliberately NOT copied: .dockerignore may exclude docs/, and a
# COPY of an ignore-excluded path fails the whole build ("file not found" /
# "excluded by .dockerignore"). Docs are a development artifact — the runtime
# image ships application code, configs, and scripts only.
COPY src/ src/
COPY configs/ configs/
COPY scripts/ scripts/

# Create data directory
RUN mkdir -p data logs

# Set Python path
ENV PYTHONPATH=/app/src

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# Default command: run the API server
CMD ["python", "-m", "uvicorn", "novacontrol.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
