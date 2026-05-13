"""Built-in chart catalogue and scenario YAML loader for superset_stress.

The built-in chart catalogue is loaded from ``_data/charts.json``, which was
generated from the workshop Superset instance (27 charts across 3 dashboards).
It is used as a fallback when no explicit chart specification is provided in
the scenario YAML.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from meshops_copilot.core.errors import ScenarioError

# ── Built-in chart catalogue ──────────────────────────────────────────────────

_DATA_FILE = Path(__file__).parent / "_data" / "charts.json"

with open(_DATA_FILE) as _fh:
    BUILTIN_CHARTS: dict[str, dict] = json.load(_fh)
"""
Mapping of chart_key → chart entry.  Each entry has the shape::

    {
        "chart_id": int,
        "name": str,
        "dashboard_id": int | None,
        "viz_type": str,
        "query_context": dict,   # ready to POST to /api/v1/chart/data
    }

Built-in keys (workshop instance):
    total_revenue, total_orders, total_users, daily_revenue, orders_by_status,
    revenue_by_product_category, top_countries_by_orders,
    new_user_registrations_over_time, total_events, unique_sessions,
    active_users, events_over_time, events_by_type, top_pages_by_traffic,
    events_by_page, daily_active_sessions, total_campaigns,
    open_support_tickets, avg_product_rating, support_tickets_by_status,
    support_tickets_by_category, tickets_by_priority_over_time,
    product_rating_distribution, reviews_by_sentiment,
    campaign_conversion_rate_by_channel, campaign_performance_table,
    customer_segment_sizes
"""

# Convenience groupings by dashboard
DASHBOARD_CHARTS: dict[int, list[str]] = {}
for _key, _entry in BUILTIN_CHARTS.items():
    _db_id = _entry.get("dashboard_id")
    if _db_id is not None:
        DASHBOARD_CHARTS.setdefault(_db_id, []).append(_key)


# ── Scenario loader ────────────────────────────────────────────────────────────

def load_scenario(path: str | Path) -> dict:
    """Parse a scenario YAML and return the raw dict."""
    p = Path(path)
    if not p.exists():
        raise ScenarioError(f"Scenario file not found: {p}")
    with open(p) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ScenarioError(f"Scenario YAML must be a mapping, got: {type(data)}")
    return data


def resolve_charts(scenario: dict) -> dict[str, dict]:
    """Return the effective chart catalogue for a scenario.

    Resolution order:
    1. ``charts_file: <path>``  — load a JSON file in the same format as
       ``BUILTIN_CHARTS``.
    2. ``charts: [name, ...]``  — a list of built-in chart keys.
    3. ``charts: {name: {...}}`` — inline chart specs (must contain
       ``chart_id`` and ``query_context``).
    4. ``dashboard_id: <int>``  — restrict built-ins to one dashboard.
    5. (no charts key)          — fall back to all built-in charts.
    """
    # ── 1. External JSON file ─────────────────────────────────────────────────
    charts_file = scenario.get("charts_file")
    if charts_file:
        p = Path(charts_file)
        if not p.exists():
            raise ScenarioError(f"charts_file not found: {p}")
        with open(p) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ScenarioError(
                f"charts_file must be a JSON object, got: {type(data)}"
            )
        return data

    raw = scenario.get("charts")

    # ── 2. List of built-in names ─────────────────────────────────────────────
    if isinstance(raw, list):
        result: dict[str, dict] = {}
        for name in raw:
            if name not in BUILTIN_CHARTS:
                raise ScenarioError(f"Unknown built-in chart: '{name}'")
            result[name] = BUILTIN_CHARTS[name]
        return result

    # ── 3. Inline chart specs ─────────────────────────────────────────────────
    if isinstance(raw, dict):
        result = {}
        for name, spec in raw.items():
            if spec is None or str(spec).strip().lower() == "builtin":
                if name not in BUILTIN_CHARTS:
                    raise ScenarioError(f"Unknown built-in chart: '{name}'")
                result[name] = BUILTIN_CHARTS[name]
            else:
                if not isinstance(spec, dict):
                    raise ScenarioError(
                        f"Inline chart spec for '{name}' must be a mapping"
                    )
                if "chart_id" not in spec or "query_context" not in spec:
                    raise ScenarioError(
                        f"Inline chart '{name}' requires 'chart_id' and 'query_context'"
                    )
                result[name] = spec
        return result

    if raw is not None:
        raise ScenarioError(f"'charts' must be a list or mapping, got: {type(raw)}")

    # ── 4. Single dashboard filter ────────────────────────────────────────────
    dashboard_id = scenario.get("dashboard_id")
    if dashboard_id is not None:
        keys = DASHBOARD_CHARTS.get(int(dashboard_id))
        if not keys:
            raise ScenarioError(
                f"No built-in charts found for dashboard_id={dashboard_id}"
            )
        return {k: BUILTIN_CHARTS[k] for k in keys}

    # ── 5. All built-ins (default) ────────────────────────────────────────────
    return dict(BUILTIN_CHARTS)
