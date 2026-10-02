# Kintsua hosted service: one image, three roles (api | worker | watchers).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Node 20 is needed for stdio MCP servers (npx -y ...) and TypeScript static checks.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra server --no-install-project

COPY kintsua ./kintsua
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra server

RUN useradd -m -u 10001 kintsua && mkdir -p /data && chown -R kintsua:kintsua /app /data
USER kintsua
ENV DATA_DIR=/data PORT=8000
EXPOSE 8000

# Default role is the API; the worker and the daily watcher run override CMD.
CMD ["python", "-m", "kintsua.api"]
