FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=8 \
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
