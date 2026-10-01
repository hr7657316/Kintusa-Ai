.PHONY: install db migrate seed api worker watch test lint fmt sandbox-image dev

install:
	uv sync --all-extras

db:
	docker run -d --name kintsua-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=kintsua -p 5432:5432 postgres:16 || docker start kintsua-pg

migrate:
	uv run alembic upgrade head

seed:
	uv run python -m kintsua.seed

api:
	uv run python -m kintsua.api

worker:
	uv run python -m kintsua.worker

watch:
	uv run python -m kintsua.watchers.run --all --report

test:
	uv run pytest -q -m "not network"

lint:
	uv run ruff check kintsua tests alembic/env.py && uv run ruff format --check kintsua tests alembic/env.py

fmt:
	uv run ruff check --fix kintsua tests alembic/env.py && uv run ruff format kintsua tests alembic/env.py

sandbox-image:
	docker build -t kintsua-sandbox:latest -f docker/sandbox.Dockerfile docker

dev: migrate seed
	( uv run python -m kintsua.worker & ) ; uv run python -m kintsua.api
