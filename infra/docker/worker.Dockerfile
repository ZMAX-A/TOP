FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/packages/contracts/src:/app/runners/web_playwright/src:/app/services/worker/src

WORKDIR /app

COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir ".[platform,runner]"

COPY --chown=pwuser:pwuser packages/contracts packages/contracts
COPY --chown=pwuser:pwuser runners/web_playwright runners/web_playwright
COPY --chown=pwuser:pwuser services/worker services/worker

RUN install -d -o pwuser -g pwuser /var/lib/testops/runs

USER pwuser
