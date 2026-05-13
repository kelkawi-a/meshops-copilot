.PHONY: install install-dev lint fmt typecheck test stress clean

# ── Setup ──────────────────────────────────────────────────────────────────────

install:
	uv pip install -e .

install-dev:
	uv pip install -e ".[dev,llm]"

# ── Quality ────────────────────────────────────────────────────────────────────

lint:
	ruff check src tests

fmt:
	ruff format src tests

typecheck:
	mypy src

# ── Tests ──────────────────────────────────────────────────────────────────────

test:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

# ── Skills ────────────────────────────────────────────────────────────────────

stress:
	meshops stress run --scenario scenarios/trino/high_concurrency.yaml

stress-light:
	meshops stress run --scenario scenarios/trino/light.yaml

diagnose:
	meshops diagnose

discover:
	meshops discover

report:
	meshops report --output reports/

# ── Clean ──────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info
