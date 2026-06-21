# DX Spotter — common development tasks.
# All commands use the project venv via `uv run` so no manual activation is needed.

.PHONY: test test-v docs docs-pdf

test:
	uv run pytest tests/

test-v:
	uv run pytest tests/ -v

docs:
	./build_docs.sh

docs-pdf:
	./build_docs.sh --pdf
