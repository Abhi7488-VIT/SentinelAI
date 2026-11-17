# syntax=docker/dockerfile:1
# Harness image: load generator, deploy pipeline and the fault controller.
# Only testbed/harness/ and testbed/scenarios/ (ground truth) are copied; the services' code is
# not needed here. Build-context exclusions are in harness.Dockerfile.dockerignore.

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /releases /runs && chown app:app /releases /runs
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app harness ./harness
COPY --chown=app:app scenarios ./scenarios
USER app
