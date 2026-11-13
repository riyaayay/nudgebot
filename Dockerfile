# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: Build virtual environment with dependencies
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Minimal runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_PATH="/app/data/nudgebot.db" \
    HEALTH_PORT="8080"

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create unprivileged application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appgroup /app

# Copy application source code
COPY --chown=appuser:appgroup . .

# Run as non-root user
USER appuser:appgroup

# Built-in lightweight health check using Python standard library
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request, os, sys; port = os.environ.get('HEALTH_PORT', '8080'); res = urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4); sys.exit(0 if res.getcode() == 200 else 1)"

EXPOSE 8080

CMD ["python", "main.py"]
