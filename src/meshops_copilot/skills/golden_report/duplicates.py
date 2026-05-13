"""Detect near-duplicate dashboards via chart-set Jaccard similarity.

Two dashboards that share a high fraction of the same charts are
candidates for merging.  The more-viewed dashboard is recommended as the
surviving report.
"""

from __future__ import annotations

from meshops_copilot.skills.golden_report.models import DuplicatePair


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity coefficient for two sets."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def find_duplicates(
    chart_mapping: dict[int, list[int]],
    dashboard_titles: dict[int, str],
    dashboard_views: dict[int, int],
    min_jaccard: float = 0.50,
    min_charts: int = 2,
) -> list[DuplicatePair]:
    """Return pairs of dashboards with Jaccard similarity >= *min_jaccard*.

    Parameters
    ----------
    chart_mapping:
        ``{dashboard_id: [chart_id, ...]}`` — the chart roster per dashboard.
    dashboard_titles:
        ``{dashboard_id: title}`` — human-readable names.
    dashboard_views:
        ``{dashboard_id: view_count_30d}`` — used to recommend which
        dashboard to keep.
    min_jaccard:
        Minimum similarity to report a pair (default 0.50).
    min_charts:
        Ignore dashboards with fewer than this many charts (avoids
        trivially-similar tiny dashboards).
    """
    ids = [
        did for did, charts in chart_mapping.items()
        if len(charts) >= min_charts
    ]
    ids.sort()

    chart_sets: dict[int, set[int]] = {
        did: set(chart_mapping[did]) for did in ids
    }

    pairs: list[DuplicatePair] = []
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1:]:
            sim = _jaccard(chart_sets[a_id], chart_sets[b_id])
            if sim < min_jaccard:
                continue

            shared = sorted(chart_sets[a_id] & chart_sets[b_id])
            a_views = dashboard_views.get(a_id, 0)
            b_views = dashboard_views.get(b_id, 0)

            if a_views >= b_views:
                keep, merge = a_id, b_id
            else:
                keep, merge = b_id, a_id

            recommendation = (
                f"merge into '{dashboard_titles.get(keep, keep)}' "
                f"(higher usage: {max(a_views, b_views)} views)"
            )

            pairs.append(DuplicatePair(
                dashboard_a_id=a_id,
                dashboard_a_title=dashboard_titles.get(a_id, str(a_id)),
                dashboard_b_id=b_id,
                dashboard_b_title=dashboard_titles.get(b_id, str(b_id)),
                shared_charts=shared,
                jaccard_similarity=round(sim, 3),
                recommendation=recommendation,
            ))

    pairs.sort(key=lambda p: p.jaccard_similarity, reverse=True)
    return pairs
