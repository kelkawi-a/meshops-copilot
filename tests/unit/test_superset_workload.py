"""Unit tests for superset_stress workload resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshops_copilot.core.errors import ScenarioError
from meshops_copilot.skills.superset_stress.workload import (
    BUILTIN_CHARTS,
    DASHBOARD_CHARTS,
    load_scenario,
    resolve_charts,
)


# ── BUILTIN_CHARTS sanity ──────────────────────────────────────────────────────

def test_builtin_charts_loaded():
    assert len(BUILTIN_CHARTS) == 27


def test_builtin_chart_structure():
    for key, entry in BUILTIN_CHARTS.items():
        assert "chart_id" in entry, f"{key} missing chart_id"
        assert "name" in entry, f"{key} missing name"
        assert "query_context" in entry, f"{key} missing query_context"
        assert isinstance(entry["chart_id"], int), f"{key} chart_id not int"


def test_dashboard_groupings():
    # Workshop has 3 dashboards.
    assert len(DASHBOARD_CHARTS) == 3
    total = sum(len(v) for v in DASHBOARD_CHARTS.values())
    assert total == 27


# ── resolve_charts: no charts key → all builtins ──────────────────────────────

def test_no_charts_returns_all_builtins():
    result = resolve_charts({})
    assert result == BUILTIN_CHARTS


# ── resolve_charts: list of names ────────────────────────────────────────────

def test_list_subset():
    result = resolve_charts({"charts": ["events_over_time", "daily_active_sessions"]})
    assert set(result.keys()) == {"events_over_time", "daily_active_sessions"}


def test_list_unknown_raises():
    with pytest.raises(ScenarioError, match="Unknown built-in chart"):
        resolve_charts({"charts": ["does_not_exist"]})


# ── resolve_charts: inline dict ───────────────────────────────────────────────

def test_inline_builtin_alias():
    """A 'builtin' marker in an inline dict pulls from BUILTIN_CHARTS."""
    result = resolve_charts({"charts": {"events_over_time": "builtin"}})
    assert result["events_over_time"] == BUILTIN_CHARTS["events_over_time"]


def test_inline_custom_spec():
    spec = {"chart_id": 99, "query_context": {"datasource": {"id": 99, "type": "table"}}}
    result = resolve_charts({"charts": {"my_chart": spec}})
    assert result["my_chart"]["chart_id"] == 99


def test_inline_missing_chart_id_raises():
    with pytest.raises(ScenarioError, match="requires 'chart_id' and 'query_context'"):
        resolve_charts({"charts": {"bad": {"query_context": {}}}})


# ── resolve_charts: dashboard_id filter ──────────────────────────────────────

def test_dashboard_id_filter():
    # Dashboard 1 has the eCommerce charts (8 charts: ids 1–8).
    result = resolve_charts({"dashboard_id": 1})
    assert len(result) == 8
    for entry in result.values():
        assert entry["dashboard_id"] == 1


def test_dashboard_id_unknown_raises():
    with pytest.raises(ScenarioError, match="No built-in charts found"):
        resolve_charts({"dashboard_id": 999})


# ── load_scenario ─────────────────────────────────────────────────────────────

def test_load_scenario_missing_file():
    with pytest.raises(ScenarioError, match="not found"):
        load_scenario("/nonexistent/path.yaml")


def test_load_scenario_returns_dict(tmp_path: Path):
    p = tmp_path / "test.yaml"
    p.write_text("name: test\ndashboard_id: 1\n")
    data = load_scenario(p)
    assert data["name"] == "test"
    assert data["dashboard_id"] == 1
