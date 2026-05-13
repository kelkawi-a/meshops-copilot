"""Collect dashboard signals from Superset REST APIs.

Fetches dashboards, activity logs, chart mappings, query performance,
and dataset certification — all via the :class:`SupersetConnector`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class ViewRecord:
    """A single dashboard-view event from the activity log."""

    user_id: int | str
    user_name: str
    dashboard_id: int
    dttm: str                     # ISO-8601


@dataclass
class QueryStat:
    """Duration + status for one query execution."""

    chart_id: int
    duration_ms: float
    status: str                   # "success" | "failed" | …


class GoldenReportCollector:
    """Fetches and parses all signals required for golden-report scoring.

    Parameters
    ----------
    connector:
        Authenticated :class:`SupersetConnector`.
    lookback_days:
        Rolling window for usage and performance signals.
    """

    def __init__(self, connector, lookback_days: int = 30) -> None:  # noqa: ANN001 (SupersetConnector)
        self._conn = connector
        self._lookback_days = lookback_days
        self._since = (
            datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        self._log_warnings: list[str] = []

    @property
    def warnings(self) -> list[str]:
        """Warnings accumulated during collection (e.g. timeouts)."""
        return list(self._log_warnings)

    # ── Dashboards ─────────────────────────────────────────────────────────

    def collect_dashboards(self) -> list[dict]:
        """Return raw dashboard dicts from ``/api/v1/dashboard/``."""
        return self._conn.list_dashboards()

    # ── Activity logs ──────────────────────────────────────────────────────

    def collect_dashboard_views(
        self, max_records: int = 50_000,
    ) -> dict[int, list[ViewRecord]]:
        """Fetch log entries for dashboard views and group by dashboard_id.

        Uses server-side RISON filters for ``action='dashboard'`` and the
        lookback time window to avoid pulling the entire log table.

        Returns an empty dict (graceful degradation) if the log endpoint
        is unavailable or times out.
        """
        try:
            raw = self._conn.get_logs(
                action="dashboard",
                since=self._since,
                max_records=max_records,
            )
        except Exception as exc:
            self._log_warnings.append(
                f"Activity logs unavailable ({exc}); "
                f"usage signals will be zero for all dashboards."
            )
            return {}

        views: dict[int, list[ViewRecord]] = {}
        for row in raw:
            did = row.get("dashboard_id")
            if did is None:
                # Try extracting from json payload
                did = _extract_dashboard_id(row)
            if did is None:
                continue

            record = ViewRecord(
                user_id=row.get("user_id", row.get("user", {}).get("id", 0)),
                user_name=(
                    row.get("user", {}).get("username", "")
                    if isinstance(row.get("user"), dict)
                    else str(row.get("user_id", ""))
                ),
                dashboard_id=int(did),
                dttm=row.get("dttm", ""),
            )
            views.setdefault(record.dashboard_id, []).append(record)
        return views

    # ── Chart → dashboard mapping ──────────────────────────────────────────

    def collect_chart_mapping(
        self,
    ) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
        """Build ``{dashboard_id: [chart_ids]}`` and ``{chart_id: [dataset_ids]}``.

        Returns
        -------
        chart_by_dashboard:
            Mapping from dashboard id to its chart ids.
        datasets_by_chart:
            Mapping from chart id to its dataset ids.
        """
        all_charts = self._conn.list_charts(max_items=1000)

        chart_by_dashboard: dict[int, list[int]] = {}
        datasets_by_chart: dict[int, list[int]] = {}

        for chart in all_charts:
            cid = chart.get("id")
            if cid is None:
                continue

            # Dataset link
            ds_id = chart.get("datasource_id")
            if ds_id is not None:
                datasets_by_chart.setdefault(cid, []).append(int(ds_id))

            # Dashboard membership
            for dash in chart.get("dashboards", []):
                did = dash.get("id")
                if did is not None:
                    chart_by_dashboard.setdefault(int(did), []).append(int(cid))

        return chart_by_dashboard, datasets_by_chart

    # ── Query performance ──────────────────────────────────────────────────

    def collect_query_performance(
        self, max_records: int = 10_000,
    ) -> dict[int, list[QueryStat]]:
        """Fetch recent queries and group stats by chart id.

        Uses ``/api/v1/query/`` which is smaller than the log table and
        contains per-query duration and status.  Degrades gracefully if
        the endpoint is unavailable.
        """
        try:
            query_results = self._fetch_queries(max_records)
        except Exception as exc:
            self._log_warnings.append(
                f"Query performance data unavailable ({exc}); "
                f"performance signals will be zero."
            )
            return {}

        stats: dict[int, list[QueryStat]] = {}
        for q in query_results:
            chart_id = _extract_chart_id_from_query(q)
            if chart_id is None:
                continue
            duration = q.get("end_result_backend_time") or q.get("end_time")
            start = q.get("start_time") or q.get("start_running_time")
            if duration and start:
                try:
                    dur_ms = (float(duration) - float(start)) * 1000
                except (TypeError, ValueError):
                    dur_ms = 0.0
            else:
                dur_ms = 0.0

            status = q.get("status", "unknown")
            stats.setdefault(chart_id, []).append(
                QueryStat(chart_id=chart_id, duration_ms=dur_ms, status=status)
            )
        return stats

    def _fetch_queries(self, max_records: int) -> list[dict]:
        """GET /api/v1/query/ with pagination."""
        self._conn._ensure_logged_in()
        import json
        import urllib.request

        results: list[dict] = []
        page = 0
        page_size = 100

        while len(results) < max_records:
            url = (
                f"{self._conn.url}/api/v1/query/"
                f"?q=(page_size:{page_size},page:{page},"
                f"order_column:start_time,order_direction:desc)"
            )
            req = urllib.request.Request(url, headers=self._conn._headers())
            try:
                body = json.loads(self._conn._open(req))
            except Exception:
                break

            page_results = body.get("result", [])
            results.extend(page_results)
            if len(page_results) < page_size or len(results) >= body.get("count", len(results)):
                break
            page += 1

        return results[:max_records]

    # ── Dataset certification ──────────────────────────────────────────────

    def collect_dataset_certification(
        self, dataset_ids: list[int],
    ) -> dict[int, bool]:
        """Check certification status for each dataset id.

        Fetches the full dataset list once and filters, falling back to
        individual ``GET /api/v1/dataset/{id}`` calls.
        """
        if not dataset_ids:
            return {}

        cert: dict[int, bool] = {}
        unique_ids = set(dataset_ids)

        try:
            all_datasets = self._conn.list_datasets(max_items=1000)
            for ds in all_datasets:
                ds_id = ds.get("id")
                if ds_id in unique_ids:
                    cert[ds_id] = bool(
                        ds.get("is_certified")
                        or ds.get("certified_by")
                        or ds.get("extra", {}).get("certification", {}).get("certified_by")
                    )
        except Exception:
            pass

        # Fill in any that weren't found in the list
        for ds_id in unique_ids - set(cert):
            try:
                ds = self._conn.get_dataset(ds_id)
                cert[ds_id] = bool(
                    ds.get("is_certified")
                    or ds.get("certified_by")
                    or ds.get("extra", {}).get("certification", {}).get("certified_by")
                )
            except Exception:
                cert[ds_id] = False

        return cert

    # ── Aggregation helpers ───────────────────────────────────────────────

    @staticmethod
    def compute_usage_signals(
        views: list[ViewRecord],
    ) -> tuple[int, int, int]:
        """Compute (view_count, unique_viewers, active_weeks) from view records."""
        if not views:
            return 0, 0, 0

        view_count = len(views)
        unique_viewers = len({v.user_id for v in views})

        # Count distinct ISO-weeks
        weeks: set[str] = set()
        for v in views:
            if v.dttm:
                try:
                    dt = datetime.fromisoformat(v.dttm.replace("Z", "+00:00"))
                    weeks.add(f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}")
                except (ValueError, TypeError):
                    pass
        active_weeks = len(weeks)

        return view_count, unique_viewers, active_weeks

    @staticmethod
    def compute_performance_signals(
        stats: list[QueryStat],
    ) -> tuple[float, float, float]:
        """Compute (median_ms, p95_ms, error_rate) from query stats."""
        if not stats:
            return 0.0, 0.0, 0.0

        durations = [s.duration_ms for s in stats if s.duration_ms > 0]
        failed = sum(1 for s in stats if s.status in ("failed", "error"))
        error_rate = failed / len(stats) if stats else 0.0

        if not durations:
            return 0.0, 0.0, error_rate

        durations.sort()
        median = statistics.median(durations)
        idx_95 = int(len(durations) * 0.95)
        p95 = durations[min(idx_95, len(durations) - 1)]

        return round(median, 1), round(p95, 1), round(error_rate, 4)


# ── Private helpers ───────────────────────────────────────────────────────────

def _extract_dashboard_id(row: dict) -> int | None:
    """Try to extract a dashboard_id from a log row's JSON payload."""
    payload = row.get("json") or row.get("payload") or ""
    if isinstance(payload, str):
        import json as _json
        try:
            payload = _json.loads(payload)
        except (ValueError, TypeError):
            return None
    if isinstance(payload, dict):
        did = payload.get("dashboard_id") or payload.get("source_id")
        if did is not None:
            try:
                return int(did)
            except (TypeError, ValueError):
                pass
    return None


def _extract_chart_id_from_query(query: dict) -> int | None:
    """Try to extract a chart/slice id from a query record."""
    # tab_name often contains "slice_<id>" or the slice name
    tab = query.get("tab_name", "")
    if isinstance(tab, str) and tab.startswith("slice_"):
        try:
            return int(tab.split("_")[1])
        except (IndexError, ValueError):
            pass

    # Some versions store it directly
    for key in ("chart_id", "slice_id", "viz_id"):
        val = query.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None
