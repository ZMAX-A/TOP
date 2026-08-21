FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384

ARG RUNNER_PACKAGE_DIGEST=sha256:e040482d91882b9be417b94cd0c404c072b7988bd61a04bb6d82da1f0b68f5e7

LABEL io.testops.package.digest="${RUNNER_PACKAGE_DIGEST}" \
      io.testops.runtime.uid="1001" \
      io.testops.runtime.gid="1001"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=8 \
    PYTHONPATH=/app/packages/contracts/src:/app/runners/web_playwright/src:/app/services/worker/src

WORKDIR /app

COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir ".[kubernetes-executor,platform,runner]"

COPY --chown=pwuser:pwuser packages/contracts packages/contracts
COPY --chown=pwuser:pwuser runners/web_playwright runners/web_playwright
COPY --chown=pwuser:pwuser services/worker services/worker

RUN install -d -o pwuser -g pwuser /var/lib/testops/runs /run/testops-input

USER pwuser
