"""Domain models for the data_product_discovery skill."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatasetSignals:
    """All signals collected for a single dataset from DataHub MCP.

    Signals are intentionally flat so the scorer can apply weights without
    introspecting nested structures.
    """

    urn: str
    name: str
    platform: str = ""
    description: str = ""
    domain: str = ""

    # ── Usage (DataHub usage stats, 30-day window) ─────────────────────────────
    query_count_30d: int = 0
    unique_users_30d: int = 0

    # ── Lineage (downstream graph) ─────────────────────────────────────────────
    downstream_dataset_count: int = 0
    downstream_dashboard_count: int = 0
    downstream_chart_count: int = 0

    # ── Ownership ──────────────────────────────────────────────────────────────
    owners: list[str] = field(default_factory=list)
    owner_teams: list[str] = field(default_factory=list)

    # ── Schema / discoverability ───────────────────────────────────────────────
    schema_field_count: int = 0
    has_description: bool = False
    tags: list[str] = field(default_factory=list)

    # ── Collection health ──────────────────────────────────────────────────────
    collection_errors: list[str] = field(default_factory=list)

    @property
    def has_owner(self) -> bool:
        return bool(self.owners or self.owner_teams)

    @property
    def display_name(self) -> str:
        return self.name or self.urn


@dataclass
class DataProductCandidate:
    """A dataset scored and ranked as a data product candidate."""

    urn: str
    name: str
    platform: str
    score: float                               # 0.0 – 1.0
    signals: DatasetSignals
    score_breakdown: dict[str, float] = field(default_factory=dict)
    justification: str = ""                    # LLM-generated sentence

    @property
    def display_name(self) -> str:
        return self.name or self.urn
