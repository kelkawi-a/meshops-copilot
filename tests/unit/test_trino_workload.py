"""Unit tests for workload query resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshops_copilot.core.errors import ScenarioError
from meshops_copilot.skills.trino_stress.workload import (
    BUILTIN_QUERIES,
    resolve_queries,
)


def test_no_queries_returns_all_builtins():
    result = resolve_queries({})
    assert result == BUILTIN_QUERIES


def test_list_subset():
    result = resolve_queries({"queries": ["light_count", "heavy_join"]})
    assert set(result.keys()) == {"light_count", "heavy_join"}


def test_inline_sql():
    result = resolve_queries({"queries": {"my_query": "SELECT 1"}})
    assert result["my_query"] == "SELECT 1"


def test_unknown_builtin_raises():
    with pytest.raises(ScenarioError, match="Unknown built-in query"):
        resolve_queries({"queries": ["does_not_exist"]})


def test_load_scenario_missing_file():
    from meshops_copilot.skills.trino_stress.workload import load_scenario

    with pytest.raises(ScenarioError, match="not found"):
        load_scenario("/nonexistent/path.yaml")
