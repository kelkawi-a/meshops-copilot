"""Score dashboards as golden report candidates.

Each signal is normalised to [0, 1] before being multiplied by its
weight, so the final composite score is always in [0, 1].  Weights sum
to exactly 1.0.

Category assignment uses both the composite score **and** hard
anti-golden overrides (stale, high error rate, excessively slow).

Tuning
------
Adjust ``WEIGHTS``, ``_CAPS``, and the thresholds in ``categorize()``
to reflect what matters in your organisation.
"""

from __future__ import annotations

from meshops_copilot.skills.golden_report.models import (
    Category,
    DashboardSignals,
    GoldenCandidate,
)


# ── Normalisation caps ────────────────────────────────────────────────────────
# A dashboard at or above the cap receives the full contribution for
# that signal.

_CAPS: dict[str, float] = {
    "view_count_30d":     1000.0,   # >=1 000 views/month -> full score
    "unique_viewers_30d":   50.0,   # >=50 unique viewers -> full score
    "active_weeks_30d":      4.0,   # all 4 weeks with views -> full score
    "days_stable":          90.0,   # unchanged for 90+ days -> full score
}


# ── Weights (must sum to 1.0) ─────────────────────────────────────────────────

WEIGHTS: dict[str, float] = {
    "view_count_30d":              0.25,   # most important: frequent use = real value
    "unique_viewers_30d":          0.20,   # cross-team / cross-user adoption
    "active_weeks_30d":            0.15,   # recurring, not one-off
    "has_owner":                   0.12,   # clear ownership is a hard signal
    "days_stable":                 0.08,   # stable definition
    "certified_dataset_fraction":  0.10,   # linked to certified datasets
    "has_description":             0.05,   # metadata quality / discoverability
    "published":                   0.05,   # published status
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "WEIGHTS must sum to 1.0"


# ── Anti-golden thresholds ────────────────────────────────────────────────────

STALE_VIEW_THRESHOLD: int = 0            # zero views in lookback = stale

# ── Score thresholds ──────────────────────────────────────────────────────────

GOLDEN_MIN_SCORE: float = 0.65
NEEDS_WORK_MIN_SCORE: float = 0.40


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(value: float, cap: float) -> float:
    """Clamp *value* to ``[0, cap]`` then normalise to ``[0, 1]``."""
    if cap <= 0:
        return 0.0
    return min(max(value, 0.0), cap) / cap


# ── Public API ────────────────────────────────────────────────────────────────

def score(signals: DashboardSignals) -> tuple[float, dict[str, float]]:
    """Return ``(total_score, per_signal_breakdown)`` for a dashboard.

    ``breakdown`` maps each weight key to its weighted contribution so
    callers can display which signals drove the score.
    """
    raw: dict[str, float] = {
        "view_count_30d":             _norm(signals.view_count_30d,
                                            _CAPS["view_count_30d"]),
        "unique_viewers_30d":         _norm(signals.unique_viewers_30d,
                                            _CAPS["unique_viewers_30d"]),
        "active_weeks_30d":           _norm(signals.active_weeks_30d,
                                            _CAPS["active_weeks_30d"]),
        "has_owner":                  1.0 if signals.has_owner else 0.0,
        "days_stable":                _norm(signals.days_since_change,
                                            _CAPS["days_stable"]),
        "certified_dataset_fraction": signals.certified_dataset_fraction,
        "has_description":            1.0 if signals.has_description else 0.0,
        "published":                  1.0 if signals.published else 0.0,
    }

    breakdown = {k: round(v * WEIGHTS[k], 4) for k, v in raw.items()}
    total = round(sum(breakdown.values()), 4)
    return total, breakdown


def categorize(total_score: float, signals: DashboardSignals) -> Category:
    """Assign a :class:`Category` based on score and hard overrides."""
    # Anti-golden override: no views = stale
    if signals.view_count_30d <= STALE_VIEW_THRESHOLD:
        return Category.ANTI_GOLDEN

    if total_score >= GOLDEN_MIN_SCORE:
        return Category.GOLDEN
    if total_score >= NEEDS_WORK_MIN_SCORE:
        return Category.NEEDS_WORK
    return Category.ANTI_GOLDEN


def identify_gaps(signals: DashboardSignals) -> list[str]:
    """Return human-readable gap descriptions for a dashboard."""
    gaps: list[str] = []
    if not signals.has_owner:
        gaps.append("no owner assigned")
    if not signals.has_description:
        gaps.append("missing description")
    if not signals.published:
        gaps.append("not published")
    if not signals.certified:
        gaps.append("not certified")
    if signals.certified_dataset_fraction < 0.5:
        pct = round(signals.certified_dataset_fraction * 100)
        gaps.append(f"only {pct}% of datasets certified")
    if signals.view_count_30d == 0:
        gaps.append("no views in last 30 days (stale)")
    if signals.active_weeks_30d < 2:
        gaps.append("low recurring usage")
    return gaps


def to_candidate(
    signals: DashboardSignals,
    justification: str = "",
) -> GoldenCandidate:
    """Score a dashboard and wrap it in a :class:`GoldenCandidate`."""
    total, breakdown = score(signals)
    category = categorize(total, signals)
    gaps = identify_gaps(signals)
    return GoldenCandidate(
        dashboard_id=signals.dashboard_id,
        title=signals.display_name,
        score=total,
        category=category,
        signals=signals,
        score_breakdown=breakdown,
        gaps=gaps,
        justification=justification,
    )
