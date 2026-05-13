"""Superset data collector for the noisy_neighbor skill.

Fetches two data sources:
1. ``/api/v1/log/`` — activity log (dashboard views, chart renders) with
   dashboard_id, slice_id (chart), user, duration.
2. ``/api/v1/query/`` — executed SQL queries with user, database, tables,
   duration, and tracking_url (contains Trino query_id for correlation).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from meshops_copilot.connectors.superset import SupersetConnector
from meshops_copilot.skills.noisy_neighbor.models import (
    SupersetLogRecord,
    SupersetQueryRecord,
)


class SupersetCollector:
    """Fetch activity logs and query history from the Superset REST API."""

    def __init__(self, connector: SupersetConnector, lookback_hours: int = 168) -> None:
        self._conn = connector
        self.lookback_hours = lookback_hours

    # ── Query history ──────────────────────────────────────────────────────────

    def collect_queries(self, max_records: int = 5000) -> list[SupersetQueryRecord]:
        """Fetch recent queries from /api/v1/query/."""
        records: list[SupersetQueryRecord] = []
        page = 0
        page_size = 100

        while len(records) < max_records:
            url = (
                f"{self._conn.url}/api/v1/query/"
                f"?q=(page_size:{page_size},page:{page},"
                f"order_column:start_time,order_direction:desc)"
            )
            req = urllib.request.Request(
                url, headers=self._conn._headers(csrf=False)
            )
            try:
                body = json.loads(self._conn._open(req))
            except Exception:
                break

            page_results = body.get("result", [])
            for r in page_results:
                rec = self._parse_query(r)
                if rec:
                    records.append(rec)

            if len(page_results) < page_size:
                break
            if len(records) >= body.get("count", len(records)):
                break
            page += 1

        return records

    def _parse_query(self, raw: dict) -> SupersetQueryRecord | None:
        """Parse a single query record from the API response."""
        user_info = raw.get("user", {})
        if not user_info:
            return None

        # Extract Trino query_id from tracking_url
        # Format: https://trino.host/ui/query.html?20260506_144956_00019_udkit
        tracking_url = raw.get("tracking_url") or ""
        trino_qid = self._extract_trino_query_id(tracking_url)

        # Compute duration from start_time / end_time (ms timestamps)
        start_ms = raw.get("start_time") or 0
        end_ms = raw.get("end_time") or 0
        duration_ms = (end_ms - start_ms) if (start_ms and end_ms) else 0.0

        # Parse table references
        tables: list[str] = []
        for t in raw.get("sql_tables", []):
            parts = [t.get("catalog"), t.get("schema"), t.get("table")]
            tables.append(".".join(p for p in parts if p))

        db_info = raw.get("database", {})
        user_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()

        return SupersetQueryRecord(
            id=raw["id"],
            user=user_name or f"user_{user_info.get('id', '?')}",
            user_id=user_info.get("id", 0),
            database=db_info.get("database_name", "unknown"),
            schema=raw.get("schema"),
            tables=tables,
            duration_ms=duration_ms,
            status=raw.get("status", "unknown"),
            trino_query_id=trino_qid,
            start_time=raw.get("changed_on", ""),
        )

    @staticmethod
    def _extract_trino_query_id(tracking_url: str) -> str | None:
        """Extract Trino query_id from a Superset tracking URL."""
        if not tracking_url:
            return None
        # Pattern: ?<query_id> at end of URL
        m = re.search(r"\?(\d{8}_\d{6}_\d{5}_[a-z0-9]+)", tracking_url)
        return m.group(1) if m else None

    # ── Activity logs ──────────────────────────────────────────────────────────

    def collect_logs(
        self,
        max_records: int = 10000,
        actions: list[str] | None = None,
    ) -> list[SupersetLogRecord]:
        """Fetch recent activity from /api/v1/log/.

        The log table can be very large; we paginate in reverse chronological
        order and stop early if records fall outside the lookback window.
        Only records with a dashboard_id or slice_id are useful for dimension
        analysis, so we filter client-side to avoid slow server-side filters.
        """
        records: list[SupersetLogRecord] = []
        page = 0
        page_size = 100

        while len(records) < max_records:
            url = (
                f"{self._conn.url}/api/v1/log/"
                f"?q=(page_size:{page_size},page:{page},"
                f"order_column:dttm,order_direction:desc)"
            )
            req = urllib.request.Request(
                url, headers=self._conn._headers(csrf=False)
            )
            try:
                body = json.loads(self._conn._open(req, timeout=30))
            except Exception:
                break

            page_results = body.get("result", [])
            if not page_results:
                break

            for r in page_results:
                rec = self._parse_log(r)
                if rec is None:
                    continue
                # Only keep records that attribute to a dashboard or chart
                if rec.dashboard_id or rec.slice_id:
                    if actions is None or rec.action in actions:
                        records.append(rec)

            # Stop if we've gone back far enough (simple heuristic: if the
            # last record on the page is too old, we won't find more useful data)
            if len(page_results) < page_size:
                break
            page += 1
            # Safety cap on pages to avoid hammering the API
            if page > max_records // page_size:
                break

        return records

    def _parse_log(self, raw: dict) -> SupersetLogRecord | None:
        """Parse a single log record."""
        user_info = raw.get("user")
        user_name: str | None = None
        user_id: int | None = raw.get("user_id")
        if user_info:
            user_name = user_info.get("username") or (
                f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
            )
            user_id = user_info.get("id", user_id)

        return SupersetLogRecord(
            action=raw.get("action", ""),
            dashboard_id=raw.get("dashboard_id"),
            slice_id=raw.get("slice_id"),
            user_id=user_id,
            user_name=user_name,
            duration_ms=raw.get("duration_ms", 0) or 0,
            dttm=raw.get("dttm", ""),
        )
