"""Domain models for the duplicate_detector skill.

All models are flat dataclasses so the scorer can apply weights without
introspecting nested structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DetectionReason(str, Enum):
    """Why two dashboards were flagged as duplicates."""

    NAME = "name_similarity"
    CHARTS = "chart_overlap"
    DATASETS = "dataset_overlap"
    TERMS = "term_overlap"
    SQL = "sql_fingerprint"


@dataclass
class DashboardProfile:
    """All signals collected for a single dashboard from DataHub (and optionally Superset).

    Signals are flat so detectors can operate on simple set comparisons.
    """

    urn: str
    title: str = ""
    platform: str = ""
    description: str = ""

    # Ownership
    owners: list[str] = field(default_factory=list)
    owner_teams: list[str] = field(default_factory=list)

    # Structural signals (DataHub)
    chart_urns: list[str] = field(default_factory=list)
    dataset_urns: list[str] = field(default_factory=list)   # upstream datasets (via lineage)
    glossary_term_urns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    # SQL signals (Superset, opt-in)
    sql_fingerprints: list[str] = field(default_factory=list)   # SHA-256 of normalised SQL

    # Graceful degradation carrier
    collection_errors: list[str] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        return self.title or self.urn


@dataclass
class DuplicatePair:
    """Per-signal similarity scores between two dashboards.

    Produced by the detectors; consumed by the scorer to compute ``confidence``.
    """

    urn_a: str
    urn_b: str
    name_similarity: float = 0.0
    chart_jaccard: float = 0.0
    dataset_jaccard: float = 0.0
    term_jaccard: float = 0.0
    sql_overlap: float = 0.0
    confidence: float = 0.0
    reasons: list[DetectionReason] = field(default_factory=list)


@dataclass
class DuplicateGroup:
    """A cluster of 2+ dashboards that are likely duplicates.

    Produced by the scorer after union-find clustering of high-confidence pairs.
    """

    group_id: str                           # stable hash of sorted member URNs
    members: list[DashboardProfile]
    confidence: float                       # 0.0–1.0
    reasons: list[DetectionReason]
    score_breakdown: dict[str, float]       # per-signal weighted contribution
    recommendation: str = ""               # "keep X, deprecate Y and Z"
    consolidation_note: str = ""           # LLM-generated free-text
