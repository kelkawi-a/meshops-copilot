"""Unit tests for SupersetDiscovery (chart catalogue construction)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from meshops_copilot.skills.superset_stress.discovery import (
    DiscoveryResult,
    SupersetDiscovery,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chart(
    chart_id: int,
    name: str,
    viz_type: str = "big_number_total",
    ds_id: int = 1,
    ds_type: str = "table",
    params: str | None = None,
    query_context: str | None = None,
    dashboards: list | None = None,
) -> dict:
    return {
        "id": chart_id,
        "slice_name": name,
        "viz_type": viz_type,
        "datasource_id": ds_id,
        "datasource_type": ds_type,
        "params": params or "{}",
        "query_context": query_context,
        "dashboards": dashboards or [],
    }


def _make_discovery(charts: list[dict]) -> tuple[SupersetDiscovery, MagicMock]:
    connector = MagicMock()
    connector.list_charts.return_value = charts
    connector.get_chart.return_value = {}   # detail endpoint returns nothing by default
    disc = SupersetDiscovery(connector, max_charts=500)
    return disc, connector


# ── _to_key ───────────────────────────────────────────────────────────────────

def test_to_key_basic():
    assert SupersetDiscovery._to_key("Total Revenue") == "total_revenue"


def test_to_key_special_chars():
    assert SupersetDiscovery._to_key("Events by Type (All)") == "events_by_type_all"


def test_to_key_empty_fallback():
    assert SupersetDiscovery._to_key("!!!") == "chart"


# ── _parse_qc ─────────────────────────────────────────────────────────────────

def test_parse_qc_none():
    assert SupersetDiscovery._parse_qc(None) is None


def test_parse_qc_empty_string():
    assert SupersetDiscovery._parse_qc("") is None


def test_parse_qc_dict():
    qc = {"datasource": {"id": 1, "type": "table"}}
    assert SupersetDiscovery._parse_qc(qc) == qc


def test_parse_qc_json_string():
    import json
    qc = {"datasource": {"id": 1, "type": "table"}, "force": False}
    result = SupersetDiscovery._parse_qc(json.dumps(qc))
    assert result == qc


def test_parse_qc_invalid_json():
    assert SupersetDiscovery._parse_qc("{not valid json") is None


# ── _build_from_params ────────────────────────────────────────────────────────

def _make_chart(ds_id: int = 1, viz: str = "big_number_total", **extra) -> dict:
    return {"id": 1, "slice_name": "Test", "viz_type": viz,
            "datasource_id": ds_id, "datasource_type": "table", **extra}


def test_build_single_metric():
    chart = _make_chart()
    params = {"metric": {"expressionType": "SQL", "sqlExpression": "SUM(x)", "label": "X"}}
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc is not None
    assert qc["force"] is True
    assert qc["datasource"] == {"id": 1, "type": "table"}
    assert qc["queries"][0]["metrics"][0]["sqlExpression"] == "SUM(x)"
    assert qc["queries"][0]["columns"] == []


def test_build_metrics_list():
    chart = _make_chart()
    params = {"metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "N"}]}
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc is not None
    assert len(qc["queries"][0]["metrics"]) == 1


def test_build_with_groupby():
    chart = _make_chart()
    params = {
        "metric": {"expressionType": "SIMPLE", "aggregate": "COUNT",
                   "column": {"column_name": "id"}, "label": "N"},
        "groupby": ["status", "country"],
        "row_limit": 50,
    }
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc["queries"][0]["columns"] == ["status", "country"]
    assert qc["queries"][0]["row_limit"] == 50


def test_build_no_metrics_returns_none():
    chart = _make_chart()
    qc = SupersetDiscovery._build_from_params(chart, {"row_limit": 100})
    assert qc is None


def test_build_no_datasource_returns_none():
    chart = {"id": 1, "slice_name": "X", "viz_type": "table",
             "datasource_id": None, "datasource_type": "table"}
    params = {"metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "N"}}
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc is None


def test_build_row_limit_capped():
    chart = _make_chart()
    params = {"metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "N"},
              "row_limit": 999_999}
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc["queries"][0]["row_limit"] == 10_000


def test_build_timeseries_omits_x_axis():
    """x_axis should NOT appear in columns — only groupby columns are included."""
    chart = _make_chart(viz="echarts_timeseries_bar")
    params = {
        "x_axis": "created_at",
        "metrics": [{"expressionType": "SIMPLE", "aggregate": "COUNT",
                     "column": {"column_name": "id"}, "label": "N"}],
    }
    qc = SupersetDiscovery._build_from_params(chart, params)
    assert qc is not None
    assert qc["queries"][0]["columns"] == []   # x_axis not included


# ── SupersetDiscovery.run ─────────────────────────────────────────────────────

def test_run_builds_catalogue_from_params():
    charts = [
        _chart(1, "Revenue", params='{"metric": {"expressionType": "SQL", "sqlExpression": "SUM(x)", "label": "X"}}',
               dashboards=[{"id": 1}]),
        _chart(2, "Orders", params='{"metric": {"expressionType": "SIMPLE", "aggregate": "COUNT", "column": {"column_name": "id"}, "label": "N"}}'),
    ]
    disc, _ = _make_discovery(charts)
    result = disc.run()

    assert result.total_found == 2
    assert result.built == 2
    assert result.skipped == 0
    assert "revenue" in result.catalogue
    assert "orders" in result.catalogue
    assert result.catalogue["revenue"]["chart_id"] == 1
    assert result.catalogue["revenue"]["dashboard_id"] == 1
    assert result.catalogue["orders"]["dashboard_id"] is None


def test_run_skips_charts_without_metrics():
    charts = [
        _chart(1, "No Metrics", params='{"groupby": ["status"]}'),
        _chart(2, "Has Metrics", params='{"metric": {"expressionType": "SQL", "sqlExpression": "SUM(x)", "label": "X"}}'),
    ]
    disc, _ = _make_discovery(charts)
    result = disc.run()

    assert result.built == 1
    assert result.skipped == 1
    assert "No Metrics" in result.skipped_names


def test_run_uses_stored_qc_when_available():
    import json
    stored_qc = {"datasource": {"id": 5, "type": "table"}, "force": False,
                 "queries": [{"metrics": [{"expressionType": "SQL", "sqlExpression": "AVG(x)", "label": "A"}],
                               "columns": [], "filters": [], "extras": {"having": "", "where": ""},
                               "applied_time_extras": {}, "annotation_layers": [],
                               "row_limit": 100, "series_columns": [], "series_limit": 0,
                               "series_limit_metric": None, "url_params": {}, "custom_params": {},
                               "custom_form_data": {}, "post_processing": [], "time_range": "No filter"}],
                 "form_data": {}, "result_format": "json", "result_type": "full"}
    charts = [
        _chart(1, "Stored", query_context=json.dumps(stored_qc),
               params='{"metric": {"expressionType": "SQL", "sqlExpression": "SUM(x)", "label": "X"}}'),
    ]
    disc, connector = _make_discovery(charts)
    result = disc.run()

    # Should use stored QC and set force=True; get_chart should NOT be called.
    connector.get_chart.assert_not_called()
    assert result.catalogue["stored"]["query_context"]["force"] is True
    # The stored QC's original metric (AVG) should be preserved.
    assert result.catalogue["stored"]["query_context"]["queries"][0]["metrics"][0]["sqlExpression"] == "AVG(x)"


def test_run_deduplicates_names():
    params = '{"metric": {"expressionType": "SQL", "sqlExpression": "SUM(x)", "label": "X"}}'
    charts = [
        _chart(1, "Sales", params=params),
        _chart(2, "Sales", params=params),  # duplicate name
    ]
    disc, _ = _make_discovery(charts)
    result = disc.run()

    assert result.built == 2
    assert "sales" in result.catalogue
    assert "sales_2" in result.catalogue


def test_run_falls_back_to_detail_endpoint():
    """If list has no QC and params have no metrics, try detail endpoint."""
    import json
    stored_qc = {"datasource": {"id": 3, "type": "table"}, "force": False,
                 "queries": [{"metrics": [{"expressionType": "SQL", "sqlExpression": "MAX(y)", "label": "M"}],
                               "columns": [], "filters": [], "extras": {"having": "", "where": ""},
                               "applied_time_extras": {}, "annotation_layers": [],
                               "row_limit": 10, "series_columns": [], "series_limit": 0,
                               "series_limit_metric": None, "url_params": {}, "custom_params": {},
                               "custom_form_data": {}, "post_processing": [], "time_range": "No filter"}],
                 "form_data": {}, "result_format": "json", "result_type": "full"}

    charts = [_chart(3, "Detail Only", params='{}')]  # no params, no list QC
    disc, connector = _make_discovery(charts)
    connector.get_chart.return_value = {"query_context": json.dumps(stored_qc)}

    result = disc.run()
    assert result.built == 1
    assert result.catalogue["detail_only"]["query_context"]["queries"][0]["metrics"][0]["sqlExpression"] == "MAX(y)"
