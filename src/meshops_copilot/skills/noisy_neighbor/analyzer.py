"""Analyzer — computes noise ratios per dimension from correlated data.

Dimensions:
- **user**: which users generate disproportionate Trino cost
- **database**: which Superset databases (catalogs) are most expensive
- **table**: which datasets/tables are queried heaviest relative to frequency
- **time_of_day**: hourly buckets showing when expensive queries cluster

Dashboard and chart dimensions require log data (SupersetLogRecord) which
provides dashboard_id and slice_id.  These are analyzed from the log collector
separately since they aren't in the query records.
"""

from __future__ import annotations

from collections import defaultdict

from meshops_copilot.skills.noisy_neighbor.correlator import (
    CorrelatedQuery,
    CorrelationResult,
)
from meshops_copilot.skills.noisy_neighbor.models import (
    DimensionResult,
    EntityScore,
    SupersetLogRecord,
)


class Analyzer:
    """Compute per-dimension noise ratios from correlated query data."""

    def analyze_all(
        self,
        correlation: CorrelationResult,
        logs: list[SupersetLogRecord],
        chart_names: dict[int, str] | None = None,
        dashboard_names: dict[int, str] | None = None,
    ) -> dict[str, DimensionResult]:
        """Run all dimension analyses and return results keyed by dimension name."""
        results: dict[str, DimensionResult] = {}

        results["user"] = self._analyze_user(correlation)
        results["database"] = self._analyze_database(correlation)
        results["time_of_day"] = self._analyze_time_of_day(correlation)

        if logs:
            results["dashboard"] = self._analyze_dashboard(logs, dashboard_names or {})
            results["chart"] = self._analyze_chart(logs, chart_names or {})

        return results

    # ── User dimension ─────────────────────────────────────────────────────────

    def _analyze_user(self, corr: CorrelationResult) -> DimensionResult:
        """Noise ratio per user: activity = query count, cost = total duration."""
        user_activity: dict[str, int] = defaultdict(int)
        user_cost: dict[str, float] = defaultdict(float)

        # Correlated queries
        for cq in corr.correlated:
            user = cq.superset_query.user
            user_activity[user] += 1
            user_cost[user] += cq.trino_query.duration_ms

        # Superset-sourced Trino queries (no direct Superset match)
        for tq in corr.superset_source_trino:
            user = tq.user
            user_activity[user] += 1
            user_cost[user] += tq.duration_ms

        return self._build_dimension("user", user_activity, user_cost)

    # ── Database dimension ─────────────────────────────────────────────────────

    def _analyze_database(self, corr: CorrelationResult) -> DimensionResult:
        """Noise ratio per database/catalog."""
        db_activity: dict[str, int] = defaultdict(int)
        db_cost: dict[str, float] = defaultdict(float)

        for cq in corr.correlated:
            db = cq.superset_query.database
            db_activity[db] += 1
            db_cost[db] += cq.trino_query.duration_ms

        return self._build_dimension("database", db_activity, db_cost)

    # ── Time of day dimension ──────────────────────────────────────────────────

    def _analyze_time_of_day(self, corr: CorrelationResult) -> DimensionResult:
        """Noise ratio per hour-of-day bucket."""
        hour_activity: dict[str, int] = defaultdict(int)
        hour_cost: dict[str, float] = defaultdict(float)

        all_queries = [
            (cq.trino_query, cq.trino_query.duration_ms)
            for cq in corr.correlated
        ] + [
            (tq, tq.duration_ms) for tq in corr.superset_source_trino
        ]

        for tq, cost in all_queries:
            hour = self._extract_hour(tq.created)
            if hour is not None:
                bucket = f"{hour:02d}:00"
                hour_activity[bucket] += 1
                hour_cost[bucket] += cost

        return self._build_dimension("time_of_day", hour_activity, hour_cost)

    # ── Dashboard dimension (from logs) ────────────────────────────────────────

    def _analyze_dashboard(
        self, logs: list[SupersetLogRecord], names: dict[int, str]
    ) -> DimensionResult:
        """Noise ratio per dashboard: activity = view count, cost = duration_ms."""
        db_activity: dict[str, int] = defaultdict(int)
        db_cost: dict[str, float] = defaultdict(float)

        for log in logs:
            if log.dashboard_id is None:
                continue
            name = names.get(log.dashboard_id, f"dashboard_{log.dashboard_id}")
            db_activity[name] += 1
            db_cost[name] += log.duration_ms

        return self._build_dimension("dashboard", db_activity, db_cost)

    # ── Chart dimension (from logs) ────────────────────────────────────────────

    def _analyze_chart(
        self, logs: list[SupersetLogRecord], names: dict[int, str]
    ) -> DimensionResult:
        """Noise ratio per chart: activity = render count, cost = duration_ms."""
        chart_activity: dict[str, int] = defaultdict(int)
        chart_cost: dict[str, float] = defaultdict(float)

        for log in logs:
            if log.slice_id is None:
                continue
            name = names.get(log.slice_id, f"chart_{log.slice_id}")
            chart_activity[name] += 1
            chart_cost[name] += log.duration_ms

        return self._build_dimension("chart", chart_activity, chart_cost)

    # ── Shared builder ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_dimension(
        dimension: str,
        activity: dict[str, int],
        cost: dict[str, float],
    ) -> DimensionResult:
        """Compute EntityScores from raw activity/cost dicts."""
        total_activity = sum(activity.values())
        total_cost = sum(cost.values())

        entities: list[EntityScore] = []
        for name in activity:
            entities.append(
                EntityScore.compute(
                    name=name,
                    activity_count=activity[name],
                    total_activity=total_activity,
                    cost_ms=cost.get(name, 0.0),
                    total_cost_ms=total_cost,
                )
            )

        # Sort by noise_ratio descending
        entities.sort(key=lambda e: e.noise_ratio, reverse=True)

        return DimensionResult(
            dimension=dimension,
            total_activity=total_activity,
            total_cost_ms=total_cost,
            entities=entities,
        )

    @staticmethod
    def _extract_hour(timestamp_str: str) -> int | None:
        """Extract hour (0-23) from a timestamp string."""
        # Format: "2026-05-13 12:40:20.389 UTC" or ISO
        import re
        m = re.search(r"(\d{2}):\d{2}:\d{2}", timestamp_str)
        return int(m.group(1)) if m else None
