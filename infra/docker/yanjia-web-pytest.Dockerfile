FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384

ARG RUNNER_PACKAGE_DIGEST

LABEL io.testops.package.digest="${RUNNER_PACKAGE_DIGEST}" \
      io.testops.runtime.uid="1001" \
      io.testops.runtime.gid="1001" \
      io.testops.package.name="yanjia-web-pytest"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=8 \
    PYTHONPATH=/app/packages/contracts/src:/app/runners/web_playwright/src:/app/runners/legacy_pytest/src:/app/services/worker/src \
    TESTOPS_LEGACY_PYTEST_PROJECT_ROOT=/opt/yanjia-web

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends novnc x11vnc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY --from=yanjia_web requirements.txt /tmp/yanjia-requirements.txt
RUN test -n "${RUNNER_PACKAGE_DIGEST}" \
    && python -m pip install --no-cache-dir --ignore-installed ".[kubernetes-executor,platform,runner]" \
    && python -m pip install --no-cache-dir -r /tmp/yanjia-requirements.txt

COPY --chown=pwuser:pwuser packages/contracts packages/contracts
COPY --chown=pwuser:pwuser runners/web_playwright runners/web_playwright
COPY --chown=pwuser:pwuser runners/legacy_pytest runners/legacy_pytest
COPY --chown=pwuser:pwuser services/worker services/worker

COPY --from=yanjia_web --chown=pwuser:pwuser pages /opt/yanjia-web/pages
COPY --from=yanjia_web --chown=pwuser:pwuser tests /opt/yanjia-web/tests
COPY --from=yanjia_web --chown=pwuser:pwuser utils /opt/yanjia-web/utils
COPY --from=yanjia_web --chown=pwuser:pwuser test_assets /opt/yanjia-web/test_assets
COPY --from=yanjia_web --chown=pwuser:pwuser test_cases/test_case.xlsx /opt/yanjia-web/test_cases/test_case.xlsx
COPY --from=yanjia_web --chown=pwuser:pwuser pytest.ini /opt/yanjia-web/pytest.ini
COPY --from=yanjia_web --chown=pwuser:pwuser run_parallel_tests.py /opt/yanjia-web/run_parallel_tests.py

RUN chmod -R a-w /opt/yanjia-web \
    && install -d -o pwuser -g pwuser /var/lib/testops/runs /run/testops-input

USER pwuser
