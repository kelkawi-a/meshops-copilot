"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def grafana_metrics():
    return json.loads((FIXTURES_DIR / "grafana_metrics.json").read_text())


@pytest.fixture
def datahub_search_results():
    return json.loads((FIXTURES_DIR / "datahub_search_results.json").read_text())
