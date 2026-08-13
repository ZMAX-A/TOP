FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/packages/contracts/src:/app/apps/api/src:/app/runners/web_playwright/src:/app/services/worker/src

WORKDIR /app

COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir ".[platform]"

RUN groupadd --system testops \
    && useradd --system --gid testops --home-dir /app testops

COPY --chown=testops:testops alembic.ini ./
COPY --chown=testops:testops apps/api apps/api
COPY --chown=testops:testops packages/contracts packages/contracts
COPY --chown=testops:testops runners/web_playwright runners/web_playwright
COPY --chown=testops:testops services/worker services/worker
COPY --chown=testops:testops scripts scripts
COPY --chown=testops:testops infra/alembic infra/alembic

USER testops

EXPOSE 8000
