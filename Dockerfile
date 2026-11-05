# Kalshi BTC15M paper desk - single container: engine + live-stats server
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    K15_HOME=/app \
    PORT=8000 \
    TZ=UTC

WORKDIR /app

# dependencies first so code changes do not invalidate this layer
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY kalshi15m /app/kalshi15m

# optional starting snapshot of the history; copied into the volume only if empty
COPY seed /app/seed

# non-root runtime user; /app/data and /app/journal are the persistent volumes
RUN useradd --create-home --uid 10001 desk \
    && mkdir -p /app/data /app/journal /app/logs \
    && chown -R desk:desk /app

USER desk

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "kalshi15m.healthcheck"]

CMD ["python", "-u", "-m", "kalshi15m.entrypoint"]
