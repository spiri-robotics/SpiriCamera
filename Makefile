# SpiriCamera tasks. Each mirrors a CI job, so `make check` passing locally
# means CI should too.

.PHONY: help check lint format test docs docs-serve pdf lint-docs docker clean

help:
	@echo "make check       lint and test and build docs, as CI does"
	@echo "make lint        ruff format --check and ruff check"
	@echo "make format      apply ruff formatting and fixes"
	@echo "make test        run pytest"
	@echo "make docs        build HTML docs into docs/_build/html"
	@echo "make docs-serve  build docs and serve them on http://127.0.0.1:8000"
	@echo "make pdf         build the controlled PDF (needs LaTeX: latexmk, texlive, rsvg-convert)"
	@echo "make lint-docs   check the docs' prose with Vale"
	@echo "make docker      build the runtime image"
	@echo "make clean       remove build output"

check: lint test docs
lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest

docs:
	uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html

docs-serve: docs
	uv run python -m http.server --bind 127.0.0.1 --directory docs/_build/html 8000

# Named by spiri_docs for the document number and revision, as CI publishes it.
pdf:
	uv run --group docs sphinx-build -W --keep-going -b latex docs docs/_build/latex
	$(MAKE) -C docs/_build/latex all-pdf
	@echo "docs/_build/latex/$$(uv run --group docs python -m spiri_docs name).pdf"

lint-docs:
	vale sync
	vale docs/

docker:
	docker build -f docker/Dockerfile -t spiricamera:latest .

clean:
	rm -rf docs/_build dist .pytest_cache .ruff_cache .coverage htmlcov
