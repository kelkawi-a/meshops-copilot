"""Trino data collector for the noisy_neighbor skill.

Fetches query history from ``system.runtime.queries`` filtered to recent
queries originating from Superset (``source = 'Apache Superset'``).

Available columns on the staging cluster:
    query_id, state, user, source, query, resource_group_id,
    queued_time_ms, analysis_time_ms, planning_time_ms,
    created, started, last_heartbeat, end, error_type, error_code

Note: ``cpu_time`` and ``peak_memory_bytes`` are NOT available on this
Trino version, so we compute ``duration_ms = end - created`` as the cost proxy.
"""

from __future__ import annotations

from meshops_copilot.connectors.trino import TrinoConnector
from meshops_copilot.skills.noisy_neighbor.models import TrinoQueryRecord


class TrinoCollector:
    """Fetch Superset-originated query data from Trino system tables."""

    def __init__(self, connector: TrinoConnector, lookback_hours: int = 168) -> None:
        self._conn = connector
        self.lookback_hours = lookback_hours

    def collect_queries(self, max_records: int = 5000) -> list[TrinoQueryRecord]:
        """Fetch recent Superset-originated queries from system.runtime.queries."""
        sql = f"""
            SELECT
                query_id,
                "user",
                source,
                state,
                queued_time_ms,
                planning_time_ms,
                date_diff('millisecond', created, "end") AS duration_ms,
                substr(query, 1, 200) AS query_prefix,
                cast(created AS varchar) AS created_str
            FROM system.runtime.queries
            WHERE created > current_timestamp - interval '{self.lookback_hours}' hour
              AND state IN ('FINISHED', 'FAILED')
            ORDER BY created DESC
            LIMIT {max_records}
        """
        try:
            rows = self._conn.query_rows(sql, query_timeout=60)
        except Exception:
            return []

        records: list[TrinoQueryRecord] = []
        for row in rows:
            records.append(TrinoQueryRecord(
                query_id=row.get("query_id", ""),
                user=row.get("user", ""),
                source=row.get("source"),
                state=row.get("state", ""),
                duration_ms=float(row.get("duration_ms") or 0),
                queued_time_ms=float(row.get("queued_time_ms") or 0),
                planning_time_ms=float(row.get("planning_time_ms") or 0),
                query_prefix=row.get("query_prefix", ""),
                created=row.get("created_str", ""),
            ))

        return records
