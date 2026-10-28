# Production image: API + pipeline (same image, different command).
# Company data is never baked in - it lives on the /app/data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QS_LOG_JSON=1 \
    QS_WORKERS=2 \
    PORT=8000

WORKDIR /app
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app

# dependencies first (cached layer), pinned versions from requirements.lock
COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts
RUN pip install --no-deps -e . \
    && mkdir -p /app/data/raw \
    && chown -R app:app /app/data

ARG QS_VERSION=dev
ENV QS_VERSION=${QS_VERSION}

USER app
VOLUME ["/app/data"]
EXPOSE 8000

# liveness only (the API answers even before data is built); use /ready for traffic readiness
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"

CMD ["sh", "-c", "exec uvicorn quote_system.api.main:app --host 0.0.0.0 --port ${PORT} --workers ${QS_WORKERS} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
