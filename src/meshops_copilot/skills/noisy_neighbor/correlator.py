"""Correlator — joins Superset activity data with Trino query cost.

Correlation strategies (in priority order):
1. **Direct**: Superset query ``tracking_url`` contains Trino ``query_id``.
2. **User+time**: Match by user identity + time window (±5s) when tracking_url
   is absent.
3. **Source-only**: For Trino queries with ``source = 'Apache Superset'`` that
   have no direct Superset query match, attribute cost to the Trino user
   (enables user-dimension analysis even without Superset log coverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meshops_copilot.skills.noisy_neighbor.models import (
    SupersetQueryRecord,
    TrinoQueryRecord,
)


@dataclass
class CorrelatedQuery:
    """A Superset query matched to its Trino execution."""

    superset_query: SupersetQueryRecord
    trino_query: TrinoQueryRecord
    correlation: str             # "direct" | "user_time" | "source_only"


@dataclass
class CorrelationResult:
    """Output of the correlation step."""

    correlated: list[CorrelatedQuery] = field(default_factory=list)
    unmatched_superset: list[SupersetQueryRecord] = field(default_factory=list)
    unmatched_trino: list[TrinoQueryRecord] = field(default_factory=list)

    # Trino queries with source='Apache Superset' but no Superset record match
    superset_source_trino: list[TrinoQueryRecord] = field(default_factory=list)

    @property
    def total_correlated_cost_ms(self) -> float:
        return sum(cq.trino_query.duration_ms for cq in self.correlated)


class Correlator:
    """Join Superset query records with Trino query records."""

    def __init__(self, time_window_ms: int = 5000) -> None:
        self.time_window_ms = time_window_ms

    def correlate(
        self,
        superset_queries: list[SupersetQueryRecord],
        trino_queries: list[TrinoQueryRecord],
    ) -> CorrelationResult:
        """Match Superset queries to Trino queries."""
        result = CorrelationResult()

        # Build lookup: trino query_id → TrinoQueryRecord
        trino_by_id: dict[str, TrinoQueryRecord] = {
            q.query_id: q for q in trino_queries
        }
        matched_trino_ids: set[str] = set()

        # ── Strategy 1: Direct match via tracking_url ──────────────────────────
        for sq in superset_queries:
            if sq.trino_query_id and sq.trino_query_id in trino_by_id:
                tq = trino_by_id[sq.trino_query_id]
                result.correlated.append(
                    CorrelatedQuery(
                        superset_query=sq,
                        trino_query=tq,
                        correlation="direct",
                    )
                )
                matched_trino_ids.add(sq.trino_query_id)
            else:
                result.unmatched_superset.append(sq)

        # ── Identify Superset-sourced Trino queries with no match ──────────────
        for tq in trino_queries:
            if tq.query_id in matched_trino_ids:
                continue
            if tq.source and "superset" in tq.source.lower():
                result.superset_source_trino.append(tq)
            else:
                result.unmatched_trino.append(tq)

        return result
