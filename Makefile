.PHONY: install test test-network lint fmt build

install:
	uv sync --all-extras

test:
	uv run pytest -q -m "not network"

test-network:
	uv run pytest -q -m network

lint:
	uv run ruff check kintsua tests && uv run ruff format --check kintsua tests

fmt:
	uv run ruff check --fix kintsua tests && uv run ruff format kintsua tests

build:
	uv build
