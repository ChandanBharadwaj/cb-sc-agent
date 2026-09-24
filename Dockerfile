# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app

FROM base AS build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM base AS runtime
RUN useradd --create-home --uid 10001 sanctions
COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY schemas ./schemas
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src \
    SANCTIONS_BLOB_ROOT=/data/blobs SANCTIONS_WORK_DIR=/data/work
RUN mkdir -p /data/blobs /data/work && chown -R sanctions /data
USER sanctions
EXPOSE 8080
# default: API + UI; the supervisor runs as a second container with `sanctions-agent supervise`
CMD ["sanctions-agent", "serve"]
