.PHONY: quality install format lint type test sec audit

install:
	uv sync --all-extras

lint:
	uv run ruff check app tests

format:
	uv run ruff format app tests

type:
	uv run pyright

test:
	uv run pytest -q

sec:
	uv run bandit -c pyproject.toml -r app -ll -ii

audit:
	@tmp=$$(mktemp -t pipaudit-XXXXXX.txt); \
	uv export --format requirements-txt --no-emit-project --no-hashes > $$tmp; \
	uv run pip-audit --strict --disable-pip --no-deps -r $$tmp; \
	rm -f $$tmp

quality:
	@bash scripts/quality.sh
