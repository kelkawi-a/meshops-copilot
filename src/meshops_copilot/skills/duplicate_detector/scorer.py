"""Scoring logic for duplicate dashboard detection.

Each pair of dashboards receives a weighted confidence score in [0, 1].
The weights reflect the relative evidential strength of each signal:

- ``chart_jaccard`` (0.35): sharing the same visualisation components is the
  strongest structural indicator that two dashboards answer the same question.
- ``dataset_jaccard`` (0.30): drawing from the same data lineage implies the
  same business area; meaningful only when lineage data is available.
- ``name_similarity`` (0.20): catches "Sales Overview v2", "Sales Overview
  (copy)" and similar human-naming patterns.
- ``term_jaccard`` (0.15): shared business glossary terms indicate the same
  KPI / metric domain even when chart/dataset sets differ.

When SQL fingerprints are available (``--with-sql``), ``sql_overlap``
replaces ``term_jaccard`` in the weight table — fingerprint-level evidence
is more precise than glossary-term overlap.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from meshops_copilot.skills.duplicate_detector.models import (
    DashboardProfile,
    DetectionReason,
    DuplicateGroup,
    DuplicatePair,
)

# ── Weight tables ──────────────────────────────────────────────────────────────

WEIGHTS: dict[str, float] = {
    "chart_jaccard": 0.35,
    "dataset_jaccard": 0.30,
    "name_similarity": 0.20,
    "term_jaccard": 0.15,
}

# When SQL fingerprints replace term overlap
WEIGHTS_WITH_SQL: dict[str, float] = {
    "chart_jaccard": 0.35,
    "dataset_jaccard": 0.30,
    "name_similarity": 0.20,
    "sql_overlap": 0.15,
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
assert abs(sum(WEIGHTS_WITH_SQL.values()) - 1.0) < 1e-9


# ── Pair scoring ──────────────────────────────────────────────────────────────

def score_pair(
    pair: DuplicatePair,
    use_sql: bool = False,
) -> tuple[float, dict[str, float]]:
    """Compute a weighted confidence score for a pair of dashboards.

    Returns
    -------
    (confidence, breakdown)
        ``confidence`` is a float in [0, 1].
        ``breakdown`` maps each weight key to its individual contribution.
    """
    weights = WEIGHTS_WITH_SQL if use_sql else WEIGHTS
    signals: dict[str, float] = {
        "chart_jaccard": pair.chart_jaccard,
        "dataset_jaccard": pair.dataset_jaccard,
        "name_similarity": pair.name_similarity,
        "term_jaccard": pair.term_jaccard,
        "sql_overlap": pair.sql_overlap,
    }
    breakdown: dict[str, float] = {}
    total = 0.0
    for key, weight in weights.items():
        contribution = signals.get(key, 0.0) * weight
        breakdown[key] = round(contribution, 4)
        total += contribution

    return round(min(total, 1.0), 4), breakdown


def score_pairs(
    pairs: list[DuplicatePair],
    use_sql: bool = False,
) -> list[DuplicatePair]:
    """Compute and set ``.confidence`` on every pair in-place; return the list."""
    for pair in pairs:
        confidence, _ = score_pair(pair, use_sql=use_sql)
        pair.confidence = confidence
    return pairs


# ── Group assembly ────────────────────────────────────────────────────────────

def build_groups(
    clusters: list[list[str]],
    profiles_by_urn: dict[str, DashboardProfile],
    pairs_by_urn_pair: dict[tuple[str, str], DuplicatePair],
    use_sql: bool = False,
) -> list[DuplicateGroup]:
    """Build ``DuplicateGroup`` objects from union-find clusters.

    For each cluster the group confidence is the *maximum* pairwise confidence
    found within it (conservative but interpretable).  The score breakdown is
    the average across all pairs in the cluster.

    Parameters
    ----------
    clusters:
        Output of ``detectors.cluster_pairs()``.
    profiles_by_urn:
        Mapping ``urn → DashboardProfile`` for all collected dashboards.
    pairs_by_urn_pair:
        Mapping ``(urn_a, urn_b) → DuplicatePair`` (sorted URN order) for
        quick lookup.
    use_sql:
        If ``True`` use ``WEIGHTS_WITH_SQL`` for the breakdown keys.
    """
    groups: list[DuplicateGroup] = []
    weights = WEIGHTS_WITH_SQL if use_sql else WEIGHTS

    for cluster_urns in clusters:
        members = [
            profiles_by_urn[u] for u in cluster_urns if u in profiles_by_urn
        ]
        if len(members) < 2:
            continue

        # Collect all intra-cluster pairs
        cluster_pairs: list[DuplicatePair] = []
        for i, a_urn in enumerate(cluster_urns):
            for b_urn in cluster_urns[i + 1:]:
                key = (min(a_urn, b_urn), max(a_urn, b_urn))
                pair = pairs_by_urn_pair.get(key)
                if pair:
                    cluster_pairs.append(pair)

        if not cluster_pairs:
            continue

        # Aggregate: max confidence, union of reasons, average breakdown
        max_confidence = max(p.confidence for p in cluster_pairs)
        all_reasons: list[DetectionReason] = []
        for p in cluster_pairs:
            for r in p.reasons:
                if r not in all_reasons:
                    all_reasons.append(r)

        # Average breakdown
        sum_breakdown: dict[str, float] = defaultdict(float)
        for p in cluster_pairs:
            _, bd = score_pair(p, use_sql=use_sql)
            for k, v in bd.items():
                sum_breakdown[k] += v
        n_pairs = len(cluster_pairs)
        avg_breakdown = {k: round(v / n_pairs, 4) for k, v in sum_breakdown.items()}

        group_id = _group_id(cluster_urns)
        recommendation = _recommend(members)

        groups.append(DuplicateGroup(
            group_id=group_id,
            members=members,
            confidence=max_confidence,
            reasons=all_reasons,
            score_breakdown=avg_breakdown,
            recommendation=recommendation,
        ))

    groups.sort(key=lambda g: g.confidence, reverse=True)
    return groups


# ── Helpers ───────────────────────────────────────────────────────────────────

def _group_id(urns: list[str]) -> str:
    """Stable short ID derived from the sorted URN set."""
    key = "|".join(sorted(urns)).encode()
    return hashlib.sha256(key).hexdigest()[:12]


def _recommend(members: list[DashboardProfile]) -> str:
    """Heuristic: keep the dashboard with the best metadata quality.

    Priority:
    1. Has description
    2. Has owners / owner_teams
    3. Has glossary terms
    4. Most chart URNs (richest content)
    5. Alphabetical title (deterministic tie-break)
    """
    def _quality(p: DashboardProfile) -> tuple:
        return (
            bool(p.description),
            bool(p.owners or p.owner_teams),
            bool(p.glossary_term_urns),
            len(p.chart_urns),
            p.title,
        )

    ranked = sorted(members, key=_quality, reverse=True)
    keep = ranked[0]
    deprecate = ranked[1:]
    deprecate_names = ", ".join(
        f"'{d.display_title}'" for d in deprecate
    )
    return (
        f"Keep '{keep.display_title}' "
        f"(best metadata quality); "
        f"deprecate {deprecate_names}."
    )
