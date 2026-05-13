"""Live Superset chart discovery.

Fetches the chart catalogue from a running Superset instance via its REST API,
constructs a valid ``query_context`` for each chart (from stored context or
synthesised from ``params``), and returns a catalogue in the same format as
``workload.BUILTIN_CHARTS``.

This mirrors the role of ``trino_stress.discovery`` / ``SchemaDiscovery``:
the skill tries discovery first, then falls back to BUILTIN_CHARTS if
discovery fails or yields nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from meshops_copilot.connectors.superset import SupersetConnector


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class DiscoveryResult:
    """Summary of a discovery run."""

    total_found: int = 0        # charts returned by the API
    built: int = 0              # charts with a usable query_context
    skipped: int = 0            # charts we could not build a QC for
    skipped_names: list[str] = field(default_factory=list)
    dashboard_count: int = 0    # unique dashboards represented
    catalogue: dict[str, dict] = field(default_factory=dict)


# ── Discovery class ───────────────────────────────────────────────────────────

class SupersetDiscovery:
    """Discover charts from a live Superset instance and synthesise their query_contexts.

    Resolution order per chart:
    1. ``query_context`` stored on the chart (set after first render) — use as-is.
    2. ``query_context`` from the detail endpoint ``GET /api/v1/chart/{id}`` — sometimes
       populated even when the list endpoint returns null.
    3. Construct a minimal but valid QC from the chart's ``params`` (form_data):
       extract metrics and optional groupby columns; omit x_axis to avoid
       time-grain complexity (reduces to a plain aggregate, still exercises
       the full Superset → Trino → data source pipeline).

    Charts that cannot produce a valid QC (no metrics, no datasource) are
    skipped and listed in ``DiscoveryResult.skipped_names``.
    """

    def __init__(
        self,
        connector: SupersetConnector,
        max_charts: int = 500,
    ) -> None:
        self._conn = connector
        self.max_charts = max_charts

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> DiscoveryResult:
        """Fetch charts, build catalogue, return a ``DiscoveryResult``."""
        charts_raw = self._conn.list_charts(max_items=self.max_charts)

        catalogue: dict[str, dict] = {}
        skipped: list[str] = []

        for chart in charts_raw:
            qc = self._resolve_qc(chart)
            if qc is None:
                skipped.append(chart.get("slice_name", f"chart_{chart.get('id')}"))
                continue

            key = self._to_key(chart["slice_name"])
            # Disambiguate duplicate names (e.g. two "Sales" charts).
            if key in catalogue:
                key = f"{key}_{chart['id']}"

            chart_dashboards = chart.get("dashboards", [])
            dashboard_id = chart_dashboards[0]["id"] if chart_dashboards else None

            catalogue[key] = {
                "chart_id": chart["id"],
                "name": chart["slice_name"],
                "dashboard_id": dashboard_id,
                "viz_type": chart.get("viz_type", ""),
                "query_context": qc,
            }

        dashboard_ids = {
            v["dashboard_id"]
            for v in catalogue.values()
            if v["dashboard_id"] is not None
        }

        return DiscoveryResult(
            total_found=len(charts_raw),
            built=len(catalogue),
            skipped=len(skipped),
            skipped_names=skipped[:20],
            dashboard_count=len(dashboard_ids),
            catalogue=catalogue,
        )

    # ── QC resolution ─────────────────────────────────────────────────────────

    def _resolve_qc(self, chart: dict) -> dict | None:
        """Return a usable query_context for ``chart``, or None if not possible."""

        # 1. Stored QC on the list-endpoint result (populated after first render).
        qc = self._parse_qc(chart.get("query_context"))
        if qc:
            qc["force"] = True
            return qc

        # 2. Stored QC from the detail endpoint (sometimes populated when list isn't).
        try:
            detail = self._conn.get_chart(chart["id"])
            qc = self._parse_qc(detail.get("query_context"))
            if qc:
                qc["force"] = True
                return qc
        except Exception:
            pass  # detail fetch failed; continue to param-based construction

        # 3. Build from params.
        params_raw = chart.get("params") or "{}"
        try:
            params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
        except Exception:
            return None

        return self._build_from_params(chart, params)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_qc(raw: str | dict | None) -> dict | None:
        """Parse a query_context value which may be a JSON string, dict, or None."""
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw if raw else None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) and parsed else None
        except Exception:
            return None

    @staticmethod
    def _build_from_params(chart: dict, params: dict) -> dict | None:
        """Synthesise a minimal but valid query_context from a chart's form_data.

        Strategy
        --------
        * ``metrics`` — taken from ``params["metrics"]`` (list) or
          ``params["metric"]`` (single object).  Required; return None if absent.
        * ``columns`` — taken from ``params["groupby"]`` if present.
          ``x_axis`` (time dimension on timeseries charts) is intentionally
          **omitted**: without a time grain the aggregate still exercises the
          full query path and avoids date-formatting edge cases.
        * ``row_limit`` — capped at 10 000.
        * ``time_range`` — "No filter" to avoid time-windowed empty results.
        * ``force`` — True, to bypass any result cache.
        """
        ds_id = chart.get("datasource_id")
        ds_type = chart.get("datasource_type", "table")
        viz_type = chart.get("viz_type", "table")

        if not ds_id:
            return None

        # Metrics (required).
        metrics: list[dict] = []
        if "metrics" in params and params["metrics"]:
            metrics = list(params["metrics"])
        elif "metric" in params and params["metric"]:
            metrics = [params["metric"]]

        if not metrics:
            return None

        # Columns (optional groupby).
        columns: list = []
        if isinstance(params.get("groupby"), list):
            columns = params["groupby"]

        row_limit = min(int(params.get("row_limit", 1000)), 10_000)

        return {
            "datasource": {"id": ds_id, "type": ds_type},
            "force": True,
            "queries": [
                {
                    "filters": [],
                    "extras": {"having": "", "where": ""},
                    "applied_time_extras": {},
                    "columns": columns,
                    "metrics": metrics,
                    "annotation_layers": [],
                    "row_limit": row_limit,
                    "series_columns": [],
                    "series_limit": 0,
                    "series_limit_metric": None,
                    "url_params": {},
                    "custom_params": {},
                    "custom_form_data": {},
                    "post_processing": [],
                    "time_range": "No filter",
                }
            ],
            "form_data": {"viz_type": viz_type},
            "result_format": "json",
            "result_type": "full",
        }

    @staticmethod
    def _to_key(name: str) -> str:
        """Convert a chart display name to a safe, lowercase dict key."""
        key = name.lower()
        key = re.sub(r"[^a-z0-9]+", "_", key)
        return key.strip("_") or "chart"
