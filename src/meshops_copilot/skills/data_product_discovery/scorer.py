"""Score datasets as data product candidates.

Each signal is normalised to [0, 1] before being multiplied by its weight,
so the final score is always in [0, 1].  Weights sum to exactly 1.0.

Tuning
------
Adjust ``WEIGHTS`` and ``_CAPS`` to reflect what matters most in your
organisation.  For example, if ownership is a hard pre-requisite, raise
``has_owner`` weight and set ``min_score`` high enough to exclude datasets
without an owner.
"""

from __future__ import annotations

from meshops_copilot.skills.data_product_discovery.models import (
    DataProductCandidate,
    DatasetSignals,
)

# ── Normalisation caps ────────────────────────────────────────────────────────
# A dataset at or above the cap receives the full contribution for that signal.
_CAPS: dict[str, float] = {
    "query_count_30d":            500.0,   # ≥500 queries/month → full score
    "unique_users_30d":           30.0,    # ≥30 unique users/month → full score
    "downstream_dashboard_count": 10.0,    # ≥10 dashboards powered → full score
    "downstream_dataset_count":   8.0,     # ≥8 downstream datasets → full score
    "owner_teams":                3.0,     # ≥3 different owner teams → full score
    "schema_field_count":         50.0,    # ≥50 schema fields → full score
}

# ── Weights (must sum to 1.0) ─────────────────────────────────────────────────
WEIGHTS: dict[str, float] = {
    "query_count_30d":            0.25,   # most important: frequent use = real value
    "unique_users_30d":           0.20,   # cross-user value (not a personal dataset)
    "downstream_dashboard_count": 0.20,   # powers decision-making
    "downstream_dataset_count":   0.10,   # central in the lineage graph
    "has_owner":                  0.10,   # productisation requires clear ownership
    "owner_teams":                0.05,   # cross-team = higher org-wide value
    "has_description":            0.05,   # minimum discoverability signal
    "schema_field_count":         0.05,   # proxy for schema richness / stability
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "WEIGHTS must sum to 1.0"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(value: float, cap: float) -> float:
    """Clamp value to [0, cap] then normalise to [0, 1]."""
    if cap <= 0:
        return 0.0
    return min(value, cap) / cap


# ── Public API ────────────────────────────────────────────────────────────────

def score(signals: DatasetSignals) -> tuple[float, dict[str, float]]:
    """Return ``(total_score, per_signal_breakdown)`` for a dataset.

    ``breakdown`` maps each weight key to its weighted contribution so callers
    can display which signals drove the score.
    """
    raw: dict[str, float] = {
        "query_count_30d":            _norm(signals.query_count_30d,            _CAPS["query_count_30d"]),
        "unique_users_30d":           _norm(signals.unique_users_30d,           _CAPS["unique_users_30d"]),
        "downstream_dashboard_count": _norm(signals.downstream_dashboard_count, _CAPS["downstream_dashboard_count"]),
        "downstream_dataset_count":   _norm(signals.downstream_dataset_count,   _CAPS["downstream_dataset_count"]),
        "has_owner":                  1.0 if signals.has_owner else 0.0,
        "owner_teams":                _norm(len(signals.owner_teams),           _CAPS["owner_teams"]),
        "has_description":            1.0 if signals.has_description else 0.0,
        "schema_field_count":         _norm(signals.schema_field_count,         _CAPS["schema_field_count"]),
    }
    breakdown = {k: round(v * WEIGHTS[k], 4) for k, v in raw.items()}
    total = round(sum(breakdown.values()), 4)
    return total, breakdown


def to_candidate(
    signals: DatasetSignals,
    justification: str = "",
) -> DataProductCandidate:
    """Score a dataset's signals and wrap them in a ``DataProductCandidate``."""
    total, breakdown = score(signals)
    return DataProductCandidate(
        urn=signals.urn,
        name=signals.display_name,
        platform=signals.platform,
        score=total,
        signals=signals,
        score_breakdown=breakdown,
        justification=justification,
    )
