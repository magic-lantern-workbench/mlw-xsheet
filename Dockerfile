# syntax=docker/dockerfile:1

# Targets:
#   dev  - toolchain + deps only; source is bind-mounted and auto-reloaded
#          (used by compose.yaml + compose.override.yaml, i.e. `docker compose up`)
#   prod - slim, non-root, code baked in, no reload (the default target)

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

# Build the virtualenv once; both targets reuse it.
FROM base AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

FROM builder AS dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py"]

FROM base AS prod
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . .
ENV MLW_RELOAD=0 \
    MLW_DATA_DIR=/data \
    MLW_PORT=8080
USER app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MLW_PORT\"]}/', timeout=4)"
CMD ["python", "main.py"]
