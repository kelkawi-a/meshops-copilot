"""Unit tests for the golden_report skill.

Tests cover:
  - models (DashboardSignals, GoldenCandidate, DuplicatePair, Category)
  - scorer (score, categorize, identify_gaps, to_candidate)
  - duplicates (find_duplicates, Jaccard similarity)
  - collectors (compute_usage_signals, compute_performance_signals, parsers)
"""

from __future__ import annotations

import pytest

from meshops_copilot.skills.golden_report.models import (
    Category,
    DashboardSignals,
    DuplicatePair,
    GoldenCandidate,
)
from meshops_copilot.skills.golden_report.scorer import (
    WEIGHTS,
    _norm,
    categorize,
    identify_gaps,
    score,
    to_candidate,
)
from meshops_copilot.skills.golden_report.duplicates import find_duplicates
from meshops_copilot.skills.golden_report.collectors import (
    GoldenReportCollector,
    QueryStat,
    ViewRecord,
    _extract_dashboard_id,
    _extract_chart_id_from_query,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_signals(**overrides) -> DashboardSignals:
    """Factory for DashboardSignals with sensible defaults."""
    defaults = dict(
        dashboard_id=1,
        title="Test Dashboard",
        url="http://localhost:8088/superset/dashboard/1/",
        view_count_30d=500,
        unique_viewers_30d=25,
        active_weeks_30d=4,
        owners=["alice"],
        has_description=True,
        published=True,
        certified=True,
        certified_by="data-team",
        days_since_change=60,
        chart_count=5,
        chart_ids=[1, 2, 3, 4, 5],
        median_query_duration_ms=1200.0,
        p95_query_duration_ms=5000.0,
        error_rate=0.02,
        dataset_ids=[10, 20],
        certified_dataset_fraction=1.0,
    )
    defaults.update(overrides)
    return DashboardSignals(**defaults)


# ── Model tests ───────────────────────────────────────────────────────────────

class TestModels:
    def test_has_owner_true(self):
        s = _make_signals(owners=["alice"])
        assert s.has_owner is True

    def test_has_owner_false(self):
        s = _make_signals(owners=[])
        assert s.has_owner is False

    def test_display_name_uses_title(self):
        s = _make_signals(title="My Report")
        assert s.display_name == "My Report"

    def test_display_name_fallback(self):
        s = _make_signals(title="", dashboard_id=42)
        assert "42" in s.display_name

    def test_category_enum_values(self):
        assert Category.GOLDEN.value == "golden_candidate"
        assert Category.NEEDS_WORK.value == "needs_work"
        assert Category.ANTI_GOLDEN.value == "anti_golden"

    def test_golden_candidate_dataclass(self):
        s = _make_signals()
        c = GoldenCandidate(
            dashboard_id=1,
            title="Test",
            score=0.75,
            category=Category.GOLDEN,
            signals=s,
            gaps=[],
        )
        assert c.score == 0.75
        assert c.category == Category.GOLDEN

    def test_duplicate_pair_dataclass(self):
        d = DuplicatePair(
            dashboard_a_id=1,
            dashboard_a_title="A",
            dashboard_b_id=2,
            dashboard_b_title="B",
            shared_charts=[10, 20],
            jaccard_similarity=0.8,
            recommendation="merge into A",
        )
        assert d.jaccard_similarity == 0.8
        assert len(d.shared_charts) == 2


# ── Scorer tests ──────────────────────────────────────────────────────────────

class TestScorer:
    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

    def test_norm_basic(self):
        assert _norm(50, 100) == 0.5
        assert _norm(200, 100) == 1.0
        assert _norm(0, 100) == 0.0

    def test_norm_zero_cap(self):
        assert _norm(50, 0) == 0.0

    def test_norm_negative_value(self):
        assert _norm(-10, 100) == 0.0

    def test_score_perfect_dashboard(self):
        s = _make_signals(
            view_count_30d=2000,
            unique_viewers_30d=100,
            active_weeks_30d=4,
            owners=["alice"],
            has_description=True,
            published=True,
            days_since_change=120,
            certified_dataset_fraction=1.0,
        )
        total, breakdown = score(s)
        assert total == pytest.approx(1.0, abs=0.01)
        assert all(v >= 0 for v in breakdown.values())

    def test_score_zero_dashboard(self):
        s = _make_signals(
            view_count_30d=0,
            unique_viewers_30d=0,
            active_weeks_30d=0,
            owners=[],
            has_description=False,
            published=False,
            days_since_change=0,
            certified_dataset_fraction=0.0,
        )
        total, breakdown = score(s)
        assert total < 0.05
        assert all(v >= 0 for v in breakdown.values())

    def test_score_breakdown_keys_match_weights(self):
        s = _make_signals()
        _, breakdown = score(s)
        assert set(breakdown.keys()) == set(WEIGHTS.keys())

    def test_categorize_golden(self):
        s = _make_signals(view_count_30d=500)
        assert categorize(0.70, s) == Category.GOLDEN

    def test_categorize_needs_work(self):
        s = _make_signals(view_count_30d=100)
        assert categorize(0.50, s) == Category.NEEDS_WORK

    def test_categorize_anti_golden_low_score(self):
        s = _make_signals(view_count_30d=10)
        assert categorize(0.20, s) == Category.ANTI_GOLDEN

    def test_categorize_anti_golden_stale(self):
        s = _make_signals(view_count_30d=0)
        assert categorize(0.80, s) == Category.ANTI_GOLDEN

    def test_identify_gaps_no_owner(self):
        s = _make_signals(owners=[])
        gaps = identify_gaps(s)
        assert "no owner assigned" in gaps

    def test_identify_gaps_no_description(self):
        s = _make_signals(has_description=False)
        gaps = identify_gaps(s)
        assert "missing description" in gaps

    def test_identify_gaps_low_certification(self):
        s = _make_signals(certified_dataset_fraction=0.2)
        gaps = identify_gaps(s)
        assert any("certified" in g for g in gaps)

    def test_identify_gaps_stale(self):
        s = _make_signals(view_count_30d=0)
        gaps = identify_gaps(s)
        assert any("stale" in g for g in gaps)

    def test_identify_gaps_not_published(self):
        s = _make_signals(published=False)
        gaps = identify_gaps(s)
        assert "not published" in gaps

    def test_identify_gaps_low_recurring(self):
        s = _make_signals(active_weeks_30d=1)
        gaps = identify_gaps(s)
        assert any("recurring" in g for g in gaps)

    def test_identify_gaps_perfect_dashboard(self):
        s = _make_signals()
        gaps = identify_gaps(s)
        assert gaps == []

    def test_to_candidate(self):
        s = _make_signals()
        c = to_candidate(s)
        assert isinstance(c, GoldenCandidate)
        assert c.dashboard_id == 1
        assert 0.0 <= c.score <= 1.0
        assert c.category in Category


# ── Duplicate tests ───────────────────────────────────────────────────────────

class TestDuplicates:
    def test_identical_chart_sets(self):
        mapping = {
            1: [10, 20, 30],
            2: [10, 20, 30],
        }
        pairs = find_duplicates(mapping, {1: "A", 2: "B"}, {1: 100, 2: 50})
        assert len(pairs) == 1
        assert pairs[0].jaccard_similarity == 1.0
        assert "A" in pairs[0].recommendation  # A has more views

    def test_no_overlap(self):
        mapping = {
            1: [10, 20, 30],
            2: [40, 50, 60],
        }
        pairs = find_duplicates(mapping, {1: "A", 2: "B"}, {1: 100, 2: 50})
        assert len(pairs) == 0

    def test_partial_overlap_above_threshold(self):
        mapping = {
            1: [10, 20, 30, 40],
            2: [10, 20, 30, 50],
        }
        pairs = find_duplicates(
            mapping, {1: "A", 2: "B"}, {1: 100, 2: 200},
            min_jaccard=0.5,
        )
        assert len(pairs) == 1
        assert pairs[0].jaccard_similarity == 0.6  # 3/5
        assert "B" in pairs[0].recommendation  # B has more views

    def test_partial_overlap_below_threshold(self):
        mapping = {
            1: [10, 20, 30, 40, 50],
            2: [10, 60, 70, 80, 90],
        }
        pairs = find_duplicates(
            mapping, {1: "A", 2: "B"}, {1: 100, 2: 50},
            min_jaccard=0.5,
        )
        assert len(pairs) == 0

    def test_min_charts_filter(self):
        mapping = {
            1: [10],         # only 1 chart — below min_charts=2
            2: [10],
        }
        pairs = find_duplicates(mapping, {1: "A", 2: "B"}, {1: 10, 2: 10})
        assert len(pairs) == 0

    def test_multiple_pairs(self):
        mapping = {
            1: [10, 20, 30],
            2: [10, 20, 30],
            3: [10, 20, 30],
        }
        pairs = find_duplicates(
            mapping, {1: "A", 2: "B", 3: "C"}, {1: 10, 2: 20, 3: 30},
        )
        assert len(pairs) == 3  # A-B, A-C, B-C

    def test_sorted_by_similarity_desc(self):
        mapping = {
            1: [10, 20, 30, 40],
            2: [10, 20, 30, 40],     # identical to 1
            3: [10, 20, 50, 60],     # partial overlap with 1
        }
        pairs = find_duplicates(
            mapping, {1: "A", 2: "B", 3: "C"}, {1: 10, 2: 10, 3: 10},
            min_jaccard=0.3,
        )
        assert len(pairs) >= 2
        assert pairs[0].jaccard_similarity >= pairs[-1].jaccard_similarity

    def test_empty_mapping(self):
        pairs = find_duplicates({}, {}, {})
        assert pairs == []


# ── Collector helper tests ────────────────────────────────────────────────────

class TestCollectorHelpers:
    def test_compute_usage_empty(self):
        count, viewers, weeks = GoldenReportCollector.compute_usage_signals([])
        assert count == 0
        assert viewers == 0
        assert weeks == 0

    def test_compute_usage_basic(self):
        views = [
            ViewRecord(user_id=1, user_name="alice", dashboard_id=10, dttm="2025-01-06T10:00:00+00:00"),
            ViewRecord(user_id=2, user_name="bob", dashboard_id=10, dttm="2025-01-06T11:00:00+00:00"),
            ViewRecord(user_id=1, user_name="alice", dashboard_id=10, dttm="2025-01-13T10:00:00+00:00"),
        ]
        count, viewers, weeks = GoldenReportCollector.compute_usage_signals(views)
        assert count == 3
        assert viewers == 2
        assert weeks == 2  # week 2 and week 3 of 2025

    def test_compute_usage_bad_dttm(self):
        views = [
            ViewRecord(user_id=1, user_name="a", dashboard_id=10, dttm="not-a-date"),
            ViewRecord(user_id=1, user_name="a", dashboard_id=10, dttm=""),
        ]
        count, viewers, weeks = GoldenReportCollector.compute_usage_signals(views)
        assert count == 2
        assert viewers == 1
        assert weeks == 0

    def test_compute_performance_empty(self):
        median, p95, err = GoldenReportCollector.compute_performance_signals([])
        assert median == 0.0
        assert p95 == 0.0
        assert err == 0.0

    def test_compute_performance_basic(self):
        stats = [
            QueryStat(chart_id=1, duration_ms=100, status="success"),
            QueryStat(chart_id=1, duration_ms=200, status="success"),
            QueryStat(chart_id=1, duration_ms=300, status="success"),
            QueryStat(chart_id=1, duration_ms=400, status="success"),
            QueryStat(chart_id=1, duration_ms=5000, status="failed"),
        ]
        median, p95, err = GoldenReportCollector.compute_performance_signals(stats)
        assert median == 300.0
        assert p95 >= 400  # 95th percentile of the successful + failed
        assert err == pytest.approx(0.2, abs=0.01)

    def test_compute_performance_all_failed(self):
        stats = [
            QueryStat(chart_id=1, duration_ms=100, status="failed"),
            QueryStat(chart_id=1, duration_ms=200, status="error"),
        ]
        _, _, err = GoldenReportCollector.compute_performance_signals(stats)
        assert err == 1.0

    def test_compute_performance_zero_durations(self):
        stats = [
            QueryStat(chart_id=1, duration_ms=0, status="success"),
            QueryStat(chart_id=1, duration_ms=0, status="success"),
        ]
        median, p95, err = GoldenReportCollector.compute_performance_signals(stats)
        assert median == 0.0
        assert err == 0.0


class TestExtractors:
    def test_extract_dashboard_id_from_json(self):
        row = {"json": '{"dashboard_id": 42}'}
        assert _extract_dashboard_id(row) == 42

    def test_extract_dashboard_id_from_dict(self):
        row = {"json": {"dashboard_id": 7}}
        assert _extract_dashboard_id(row) == 7

    def test_extract_dashboard_id_missing(self):
        row = {"json": '{"other": "data"}'}
        assert _extract_dashboard_id(row) is None

    def test_extract_dashboard_id_invalid_json(self):
        row = {"json": "not json at all"}
        assert _extract_dashboard_id(row) is None

    def test_extract_dashboard_id_no_payload(self):
        row = {}
        assert _extract_dashboard_id(row) is None

    def test_extract_chart_id_from_tab_name(self):
        q = {"tab_name": "slice_42"}
        assert _extract_chart_id_from_query(q) == 42

    def test_extract_chart_id_from_direct_field(self):
        q = {"chart_id": 99}
        assert _extract_chart_id_from_query(q) == 99

    def test_extract_chart_id_missing(self):
        q = {"tab_name": "some_tab", "status": "success"}
        assert _extract_chart_id_from_query(q) is None
