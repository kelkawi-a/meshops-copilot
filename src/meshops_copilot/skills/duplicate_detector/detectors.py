"""Duplicate detection logic.

Four independent detectors — name similarity, chart-set overlap, dataset-set
overlap, glossary-term overlap — each produce a float score in [0, 1] for
every pair of dashboards.

An optional fifth detector uses SQL fingerprints (Superset, opt-in).

A simple union-find algorithm then clusters the high-confidence pairs into
``DuplicateGroup`` s so that transitive duplicates (A~B, B~C → {A,B,C}) are
surfaced as a single consolidation opportunity.
"""

from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from meshops_copilot.skills.duplicate_detector.models import (
    DashboardProfile,
    DetectionReason,
    DuplicatePair,
)

if TYPE_CHECKING:
    pass


# ── Similarity primitives ──────────────────────────────────────────────────────

def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity for two sets. Returns 0.0 for two empty sets."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# Patterns to strip when normalising a dashboard title for name comparison.
# These capture version suffixes ("v2", "copy", "draft", "(old)") and
# common prefixes / separators that carry no semantic content.
_VERSION_RE = re.compile(
    r"\b(v\d+|copy|draft|old|test|temp|backup|archive|deprecated|new|updated)\b",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[" + re.escape(string.punctuation) + r"]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """Normalise a dashboard title for fuzzy name comparison."""
    t = title.lower()
    t = _VERSION_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    t = _WHITESPACE_RE.sub(" ", t).strip()
    return t


def _name_similarity(a: str, b: str) -> float:
    """Token-aware similarity between two normalised titles.

    Uses ``difflib.SequenceMatcher`` on the normalised token sequences so that
    reordered words ("Sales Overview" vs "Overview Sales") score higher than
    purely character-level matching.
    """
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    tokens_a = na.split()
    tokens_b = nb.split()
    # Compare token-level
    token_ratio = SequenceMatcher(None, sorted(tokens_a), sorted(tokens_b)).ratio()
    # Compare character-level on the normalised strings
    char_ratio = SequenceMatcher(None, na, nb).ratio()
    # Take the higher of the two — favours reordered but lexically-close titles
    return max(token_ratio, char_ratio)


# ── Pair detection ─────────────────────────────────────────────────────────────

def detect_all_pairs(
    profiles: list[DashboardProfile],
    *,
    min_chart_jaccard: float = 0.5,
    min_dataset_jaccard: float = 0.5,
    min_name_similarity: float = 0.7,
    min_term_jaccard: float = 0.5,
    min_sql_overlap: float = 0.5,
    min_any_confidence: float = 0.1,
) -> list[DuplicatePair]:
    """Compute pairwise similarity for all profiles and return candidate pairs.

    A pair is included if **any** individual signal exceeds its minimum
    threshold.  The caller should then apply the scorer to get a combined
    confidence score and filter by ``min_confidence``.

    Parameters
    ----------
    profiles:
        All dashboards to compare — typically the full collection from
        ``DashboardCollector.collect_all()``.
    min_chart_jaccard / min_dataset_jaccard / min_name_similarity / min_term_jaccard:
        Per-signal thresholds; a pair with no signal above its threshold is
        excluded even if the aggregate confidence would be non-zero.
    min_any_confidence:
        Absolute floor on the highest single-signal score; avoids returning
        pairs where every signal is negligible.
    """
    pairs: list[DuplicatePair] = []
    n = len(profiles)
    for i in range(n):
        a = profiles[i]
        a_charts = set(a.chart_urns)
        a_datasets = set(a.dataset_urns)
        a_terms = set(a.glossary_term_urns)
        a_sql = set(a.sql_fingerprints)

        for j in range(i + 1, n):
            b = profiles[j]

            chart_j = _jaccard(a_charts, set(b.chart_urns))
            dataset_j = _jaccard(a_datasets, set(b.dataset_urns))
            name_sim = _name_similarity(a.title, b.title)
            term_j = _jaccard(a_terms, set(b.glossary_term_urns))
            sql_ov = _jaccard(a_sql, set(b.sql_fingerprints)) if a_sql or b.sql_fingerprints else 0.0

            # Check if any signal exceeds its threshold
            passes = (
                (chart_j >= min_chart_jaccard and (a_charts or b.chart_urns))
                or (dataset_j >= min_dataset_jaccard and (a_datasets or b.dataset_urns))
                or (name_sim >= min_name_similarity)
                or (term_j >= min_term_jaccard and (a_terms or b.glossary_term_urns))
                or (sql_ov >= min_sql_overlap and (a_sql or b.sql_fingerprints))
            )
            max_signal = max(chart_j, dataset_j, name_sim, term_j, sql_ov)
            if not passes or max_signal < min_any_confidence:
                continue

            reasons: list[DetectionReason] = []
            if chart_j >= min_chart_jaccard and (a_charts or b.chart_urns):
                reasons.append(DetectionReason.CHARTS)
            if dataset_j >= min_dataset_jaccard and (a_datasets or b.dataset_urns):
                reasons.append(DetectionReason.DATASETS)
            if name_sim >= min_name_similarity:
                reasons.append(DetectionReason.NAME)
            if term_j >= min_term_jaccard and (a_terms or b.glossary_term_urns):
                reasons.append(DetectionReason.TERMS)
            if sql_ov >= min_sql_overlap and (a_sql or b.sql_fingerprints):
                reasons.append(DetectionReason.SQL)

            pairs.append(DuplicatePair(
                urn_a=a.urn,
                urn_b=b.urn,
                chart_jaccard=round(chart_j, 3),
                dataset_jaccard=round(dataset_j, 3),
                name_similarity=round(name_sim, 3),
                term_jaccard=round(term_j, 3),
                sql_overlap=round(sql_ov, 3),
                reasons=reasons,
            ))

    return pairs


# ── Union-find clustering ─────────────────────────────────────────────────────

class _UnionFind:
    """Path-compressed union-find for transitive clustering of URN pairs."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, x: str, y: str) -> None:
        px, py = self.find(x), self.find(y)
        if px != py:
            self._parent[px] = py


def cluster_pairs(
    pairs: list[DuplicatePair],
    all_urns: list[str],
) -> list[list[str]]:
    """Cluster pairs into transitive groups via union-find.

    Parameters
    ----------
    pairs:
        The scored ``DuplicatePair`` list (confidence already set).
    all_urns:
        All dashboard URNs — needed to initialise the union-find even for
        dashboards that ended up in no pair.

    Returns
    -------
    A list of URI groups, each group being a list of URNs that form a
    duplicate cluster (size ≥ 2).  Singletons are excluded.
    """
    uf = _UnionFind()
    for urn in all_urns:
        uf.find(urn)   # ensure all nodes are registered
    for pair in pairs:
        uf.union(pair.urn_a, pair.urn_b)

    # Collect groups of size ≥ 2
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for urn in all_urns:
        root = uf.find(urn)
        groups[root].append(urn)

    return [sorted(g) for g in groups.values() if len(g) >= 2]
