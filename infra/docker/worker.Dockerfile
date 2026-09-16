FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384

ARG RUNNER_PACKAGE_DIGEST=sha256:01bd1368544969cdfa4567a90e633698517c2386d6e978e97448d7681a16ca36

LABEL io.testops.package.digest="${RUNNER_PACKAGE_DIGEST}" \
      io.testops.runtime.uid="1001" \
      io.testops.runtime.gid="1001"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=8 \
    PYTHONPATH=/app/packages/contracts/src:/app/runners/web_playwright/src:/app/runners/legacy_pytest/src:/app/runners/android_appium/src:/app/services/worker/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends novnc x11vnc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir --ignore-installed ".[kubernetes-executor,platform,runner]"

COPY --chown=pwuser:pwuser packages/contracts packages/contracts
COPY --chown=pwuser:pwuser runners/web_playwright runners/web_playwright
COPY --chown=pwuser:pwuser runners/legacy_pytest runners/legacy_pytest
COPY --chown=pwuser:pwuser runners/android_appium runners/android_appium
COPY --chown=pwuser:pwuser services/worker services/worker

RUN install -d -o pwuser -g pwuser /var/lib/testops/runs /run/testops-input

USER pwuser
