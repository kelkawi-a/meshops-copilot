"""Built-in query catalogue and scenario YAML loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from meshops_copilot.core.errors import ScenarioError

# ── Built-in query catalogue ──────────────────────────────────────────────────

BUILTIN_QUERIES: dict[str, str] = {
    "light_count": """
        SELECT COUNT(*) FROM postgresql.public.events
    """,
    "medium_agg": """
        SELECT event_type,
               COUNT(*)                    AS occurrences,
               COUNT(DISTINCT user_id)     AS unique_users
        FROM postgresql.public.events
        GROUP BY event_type
        ORDER BY occurrences DESC
    """,
    "heavy_join": """
        SELECT p.category,
               COUNT(DISTINCT o.id)                       AS orders,
               SUM(oi.quantity)                           AS units_sold,
               CAST(SUM(oi.total_price) AS DECIMAL(15,2)) AS revenue
        FROM postgresql.public.order_items oi
        JOIN postgresql.public.orders   o ON oi.order_id  = o.id
        JOIN postgresql.public.products p ON oi.product_id = p.id
        GROUP BY p.category
        ORDER BY revenue DESC
    """,
    "window_functions": """
        SELECT user_id,
               total_amount,
               status,
               ROW_NUMBER() OVER (ORDER BY total_amount DESC)                    AS global_rank,
               RANK()       OVER (PARTITION BY status ORDER BY total_amount DESC) AS status_rank,
               SUM(total_amount)  OVER (PARTITION BY status)                     AS status_total,
               AVG(total_amount)  OVER (PARTITION BY status)                     AS status_avg
        FROM postgresql.public.orders
    """,
    "high_cardinality": """
        SELECT u.country,
               u.id,
               COUNT(e.id)                  AS event_count,
               COUNT(DISTINCT e.session_id) AS sessions,
               MIN(e.created_at)            AS first_seen,
               MAX(e.created_at)            AS last_seen
        FROM postgresql.public.users  u
        JOIN postgresql.public.events e ON u.id = e.user_id
        GROUP BY u.country, u.id
        ORDER BY event_count DESC
        LIMIT 500
    """,
    "cross_catalog": """
        SELECT cs.name                                        AS segment,
               COUNT(DISTINCT o.id)                          AS orders,
               CAST(SUM(oi.total_price) AS DECIMAL(15,2))    AS revenue
        FROM crm.public.customer_segment_members csm
        JOIN crm.public.customer_segments  cs ON csm.segment_id = cs.id
        JOIN postgresql.public.orders       o ON csm.user_id    = o.user_id
        JOIN postgresql.public.order_items oi ON o.id           = oi.order_id
        WHERE o.status = 'delivered'
        GROUP BY cs.name
        ORDER BY revenue DESC
    """,
}


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


def resolve_queries(scenario: dict) -> dict[str, str]:
    """Return the effective query dict for a scenario.

    Queries can be:
      - Omitted: falls back to all built-in queries.
      - A list of built-in names: e.g. [heavy_join, cross_catalog]
      - An inline dict: name → SQL string.
    """
    raw = scenario.get("queries")
    if raw is None:
        return dict(BUILTIN_QUERIES)

    if isinstance(raw, list):
        result = {}
        for name in raw:
            if name not in BUILTIN_QUERIES:
                raise ScenarioError(f"Unknown built-in query: '{name}'")
            result[name] = BUILTIN_QUERIES[name]
        return result

    if isinstance(raw, dict):
        result = {}
        for name, sql in raw.items():
            if sql is None or str(sql).strip().lower() == "builtin":
                if name not in BUILTIN_QUERIES:
                    raise ScenarioError(f"Unknown built-in query: '{name}'")
                result[name] = BUILTIN_QUERIES[name]
            else:
                result[name] = str(sql)
        return result

    raise ScenarioError(f"'queries' must be a list or mapping, got: {type(raw)}")
