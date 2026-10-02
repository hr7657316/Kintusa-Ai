# Kintsua fixer sandbox image: one container per fix job (see kintsua/fixer/sandbox.py).
#
# Python 3.12 + Node 20 + git + the common test runners and static checkers. No
# ENTRYPOINT and no CMD: the fixer starts the container with `sleep infinity` and execs
# every step itself. Runs as the non-root `sandbox` user; the clone lives on a volume
# mounted at /work, and a second per-job volume is mounted over /opt/venv (Docker seeds
# it from this image's venv on first mount) so what the networked install helper puts
# there is what the no-network test container imports.
#
# Build: make sandbox-image   (docker build -t kintsua-sandbox:latest -f docker/sandbox.Dockerfile docker)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates coreutils build-essential make \
 && rm -rf /var/lib/apt/lists/*

# Node 20 (jest / vitest / tsc)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g typescript@5 pnpm@9 yarn@1 \
 && npm cache clean --force

# uv (lockfile installs) and the baseline Python tooling in a venv the sandbox user owns,
# so `pip install -r requirements.txt` / `pip install -e .` work without root.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/
RUN useradd -m -u 10001 -s /bin/bash sandbox \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir "pip>=24" "pytest>=8" "pytest-timeout>=2" "ruff>=0.5" "poetry>=1.8" \
 && mkdir -p /work \
 && chown -R sandbox:sandbox /opt/venv /work

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME=/home/sandbox \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    GIT_TERMINAL_PROMPT=0 \
    CI=1

USER sandbox
WORKDIR /work
