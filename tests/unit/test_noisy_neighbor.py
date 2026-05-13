"""Unit tests for the noisy_neighbor skill."""

from __future__ import annotations

import pytest

from meshops_copilot.skills.noisy_neighbor.models import (
    DimensionResult,
    EntityScore,
    NoisyNeighborReport,
    Severity,
    SupersetLogRecord,
    SupersetQueryRecord,
    TrinoQueryRecord,
)
from meshops_copilot.skills.noisy_neighbor.correlator import (
    CorrelatedQuery,
    CorrelationResult,
    Correlator,
)
from meshops_copilot.skills.noisy_neighbor.analyzer import Analyzer
from meshops_copilot.skills.noisy_neighbor.collectors.superset import SupersetCollector


# ── Severity classification ────────────────────────────────────────────────────

class TestSeverity:
    def test_critical(self):
        assert Severity.from_ratio(3.0) == Severity.CRITICAL
        assert Severity.from_ratio(5.0) == Severity.CRITICAL

    def test_moderate(self):
        assert Severity.from_ratio(1.5) == Severity.MODERATE
        assert Severity.from_ratio(2.9) == Severity.MODERATE

    def test_normal(self):
        assert Severity.from_ratio(1.0) == Severity.NORMAL
        assert Severity.from_ratio(0.5) == Severity.NORMAL
        assert Severity.from_ratio(1.49) == Severity.NORMAL


# ── EntityScore.compute ────────────────────────────────────────────────────────

class TestEntityScore:
    def test_basic_compute(self):
        e = EntityScore.compute("dash_A", 10, 100, 5000.0, 10000.0)
        assert e.activity_share == pytest.approx(0.1)
        assert e.cost_share == pytest.approx(0.5)
        assert e.noise_ratio == pytest.approx(5.0)
        assert e.severity == Severity.CRITICAL

    def test_proportionate(self):
        e = EntityScore.compute("user_1", 50, 100, 5000.0, 10000.0)
        assert e.noise_ratio == pytest.approx(1.0)
        assert e.severity == Severity.NORMAL

    def test_zero_activity(self):
        e = EntityScore.compute("ghost", 0, 100, 0.0, 10000.0)
        assert e.noise_ratio == 0.0
        assert e.severity == Severity.NORMAL

    def test_zero_totals(self):
        e = EntityScore.compute("empty", 0, 0, 0.0, 0.0)
        assert e.activity_share == 0.0
        assert e.cost_share == 0.0
        assert e.noise_ratio == 0.0

    def test_detail_string(self):
        e = EntityScore.compute("Dashboard X", 4, 100, 3800.0, 10000.0)
        assert "4.0% of activity" in e.detail
        assert "38.0% of query time" in e.detail
        assert "9.5x" in e.detail


# ── DimensionResult ────────────────────────────────────────────────────────────

class TestDimensionResult:
    def test_noisy_count(self):
        dim = DimensionResult(
            dimension="user",
            total_activity=100,
            total_cost_ms=10000.0,
            entities=[
                EntityScore.compute("noisy", 5, 100, 5000.0, 10000.0),    # 10x → critical
                EntityScore.compute("moderate", 10, 100, 2000.0, 10000.0), # 2x → moderate
                EntityScore.compute("normal", 85, 100, 3000.0, 10000.0),  # 0.35x → normal
            ],
        )
        assert dim.noisy_count == 2

    def test_top_offenders_sorted(self):
        dim = DimensionResult(
            dimension="database",
            total_activity=100,
            total_cost_ms=10000.0,
            entities=[
                EntityScore.compute("mod", 10, 100, 2000.0, 10000.0),    # 2x
                EntityScore.compute("crit", 5, 100, 5000.0, 10000.0),   # 10x
            ],
        )
        offenders = dim.top_offenders
        assert len(offenders) == 2
        assert offenders[0].name == "crit"
        assert offenders[1].name == "mod"


# ── Correlator ─────────────────────────────────────────────────────────────────

def _ss_query(id: int, user: str, trino_qid: str | None = None, db: str = "db") -> SupersetQueryRecord:
    return SupersetQueryRecord(
        id=id, user=user, user_id=id, database=db, schema=None,
        tables=[], duration_ms=100.0, status="success",
        trino_query_id=trino_qid, start_time="2026-05-13T12:00:00",
    )


def _trino_query(qid: str, user: str, source: str | None = "Apache Superset",
                 duration_ms: float = 200.0) -> TrinoQueryRecord:
    return TrinoQueryRecord(
        query_id=qid, user=user, source=source, state="FINISHED",
        duration_ms=duration_ms, queued_time_ms=0.0, planning_time_ms=10.0,
        query_prefix="SELECT ...", created="2026-05-13 12:00:00.000 UTC",
    )


class TestCorrelator:
    def test_direct_match(self):
        ss = [_ss_query(1, "alice", trino_qid="q_001")]
        tr = [_trino_query("q_001", "alice")]
        result = Correlator().correlate(ss, tr)
        assert len(result.correlated) == 1
        assert result.correlated[0].correlation == "direct"
        assert len(result.unmatched_superset) == 0
        assert len(result.superset_source_trino) == 0

    def test_no_match_superset_source(self):
        ss = [_ss_query(1, "alice")]  # no tracking_url
        tr = [_trino_query("q_002", "alice", source="Apache Superset")]
        result = Correlator().correlate(ss, tr)
        assert len(result.correlated) == 0
        assert len(result.unmatched_superset) == 1
        assert len(result.superset_source_trino) == 1

    def test_non_superset_trino_unmatched(self):
        ss = []
        tr = [_trino_query("q_003", "system", source="trino-cli")]
        result = Correlator().correlate(ss, tr)
        assert len(result.unmatched_trino) == 1
        assert len(result.superset_source_trino) == 0

    def test_mixed(self):
        ss = [
            _ss_query(1, "alice", trino_qid="q_001"),
            _ss_query(2, "bob"),  # no match
        ]
        tr = [
            _trino_query("q_001", "alice"),
            _trino_query("q_004", "charlie", source="Apache Superset"),
            _trino_query("q_005", "system", source=None),
        ]
        result = Correlator().correlate(ss, tr)
        assert len(result.correlated) == 1
        assert len(result.unmatched_superset) == 1
        assert len(result.superset_source_trino) == 1
        assert len(result.unmatched_trino) == 1

    def test_total_correlated_cost(self):
        ss = [_ss_query(1, "a", "q1"), _ss_query(2, "b", "q2")]
        tr = [_trino_query("q1", "a", duration_ms=1000), _trino_query("q2", "b", duration_ms=3000)]
        result = Correlator().correlate(ss, tr)
        assert result.total_correlated_cost_ms == pytest.approx(4000.0)


# ── Analyzer ───────────────────────────────────────────────────────────────────

class TestAnalyzer:
    def _make_correlation(self) -> CorrelationResult:
        """Correlation with 3 queries: alice=2 (fast), bob=1 (slow)."""
        cq1 = CorrelatedQuery(
            superset_query=_ss_query(1, "alice", "q1", db="sales"),
            trino_query=_trino_query("q1", "alice", duration_ms=100),
            correlation="direct",
        )
        cq2 = CorrelatedQuery(
            superset_query=_ss_query(2, "alice", "q2", db="sales"),
            trino_query=_trino_query("q2", "alice", duration_ms=100),
            correlation="direct",
        )
        cq3 = CorrelatedQuery(
            superset_query=_ss_query(3, "bob", "q3", db="analytics"),
            trino_query=_trino_query("q3", "bob", duration_ms=800),
            correlation="direct",
        )
        return CorrelationResult(correlated=[cq1, cq2, cq3])

    def test_user_dimension(self):
        corr = self._make_correlation()
        result = Analyzer().analyze_all(corr, [])
        user_dim = result["user"]
        assert user_dim.total_activity == 3
        assert user_dim.total_cost_ms == pytest.approx(1000.0)

        # bob: 1/3 activity = 33%, 800/1000 cost = 80% → ratio ~2.4x
        bob = next(e for e in user_dim.entities if e.name == "bob")
        assert bob.noise_ratio == pytest.approx(2.4, rel=0.1)
        assert bob.severity == Severity.MODERATE

    def test_database_dimension(self):
        corr = self._make_correlation()
        result = Analyzer().analyze_all(corr, [])
        db_dim = result["database"]
        analytics = next(e for e in db_dim.entities if e.name == "analytics")
        assert analytics.activity_count == 1
        assert analytics.cost_ms == pytest.approx(800.0)
        # 33% of activity, 80% of cost → ~2.4x
        assert analytics.noise_ratio == pytest.approx(2.4, rel=0.1)

    def test_dashboard_dimension_from_logs(self):
        logs = [
            SupersetLogRecord("explore", dashboard_id=1, slice_id=None, user_id=1, user_name="alice", duration_ms=500, dttm="2026-05-13T12:00:00"),
            SupersetLogRecord("explore", dashboard_id=1, slice_id=None, user_id=1, user_name="alice", duration_ms=500, dttm="2026-05-13T12:01:00"),
            SupersetLogRecord("explore", dashboard_id=2, slice_id=None, user_id=2, user_name="bob", duration_ms=4000, dttm="2026-05-13T12:02:00"),
        ]
        result = Analyzer().analyze_all(CorrelationResult(), logs, dashboard_names={1: "Sales", 2: "Pipeline"})
        dash_dim = result["dashboard"]
        assert dash_dim.total_activity == 3

        pipeline = next(e for e in dash_dim.entities if e.name == "Pipeline")
        # 1/3 views = 33%, 4000/5000 cost = 80% → ~2.4x
        assert pipeline.noise_ratio == pytest.approx(2.4, rel=0.1)

    def test_chart_dimension_from_logs(self):
        logs = [
            SupersetLogRecord("chartdata", dashboard_id=None, slice_id=10, user_id=1, user_name="alice", duration_ms=100, dttm="2026-05-13T12:00:00"),
            SupersetLogRecord("chartdata", dashboard_id=None, slice_id=20, user_id=1, user_name="alice", duration_ms=900, dttm="2026-05-13T12:01:00"),
        ]
        result = Analyzer().analyze_all(CorrelationResult(), logs, chart_names={10: "Light Chart", 20: "Heavy Chart"})
        chart_dim = result["chart"]
        heavy = next(e for e in chart_dim.entities if e.name == "Heavy Chart")
        assert heavy.cost_share == pytest.approx(0.9)
        assert heavy.noise_ratio == pytest.approx(1.8, rel=0.1)

    def test_no_logs_skips_dashboard_chart(self):
        result = Analyzer().analyze_all(CorrelationResult(), [])
        assert "dashboard" not in result
        assert "chart" not in result

    def test_time_of_day_dimension(self):
        corr = self._make_correlation()
        result = Analyzer().analyze_all(corr, [])
        tod = result["time_of_day"]
        assert tod.total_activity > 0

    def test_extract_hour(self):
        assert Analyzer._extract_hour("2026-05-13 14:30:00.000 UTC") == 14
        assert Analyzer._extract_hour("2026-05-13T08:00:00") == 8
        assert Analyzer._extract_hour("garbage") is None


# ── SupersetCollector helpers ──────────────────────────────────────────────────

class TestSupersetCollectorHelpers:
    def test_extract_trino_query_id(self):
        url = "https://trino.staging.canonical.com/ui/query.html?20260506_144956_00019_udkit"
        assert SupersetCollector._extract_trino_query_id(url) == "20260506_144956_00019_udkit"

    def test_extract_trino_query_id_none(self):
        assert SupersetCollector._extract_trino_query_id("") is None
        assert SupersetCollector._extract_trino_query_id("https://example.com/query") is None

    def test_extract_trino_query_id_complex(self):
        url = "https://trino.host:8443/ui/query.html?20260101_000000_00001_abc12"
        assert SupersetCollector._extract_trino_query_id(url) == "20260101_000000_00001_abc12"
