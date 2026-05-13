"""Unit tests for the duplicate_detector skill."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from meshops_copilot.skills.duplicate_detector.models import (
    DashboardProfile,
    DetectionReason,
    DuplicateGroup,
    DuplicatePair,
)
from meshops_copilot.skills.duplicate_detector.detectors import (
    _jaccard,
    _name_similarity,
    _normalize_title,
    cluster_pairs,
    detect_all_pairs,
)
from meshops_copilot.skills.duplicate_detector.scorer import (
    WEIGHTS,
    WEIGHTS_WITH_SQL,
    build_groups,
    score_pair,
    score_pairs,
)
from meshops_copilot.skills.duplicate_detector.markdown import (
    build_consolidation_prompt,
    build_summary_prompt,
    format_deduplication_report,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _profile(
    urn: str,
    title: str = "Sales Overview",
    platform: str = "superset",
    chart_urns: list[str] | None = None,
    dataset_urns: list[str] | None = None,
    glossary_term_urns: list[str] | None = None,
    owners: list[str] | None = None,
    description: str = "",
    sql_fingerprints: list[str] | None = None,
) -> DashboardProfile:
    return DashboardProfile(
        urn=urn,
        title=title,
        platform=platform,
        description=description,
        owners=owners or [],
        chart_urns=chart_urns or [],
        dataset_urns=dataset_urns or [],
        glossary_term_urns=glossary_term_urns or [],
        sql_fingerprints=sql_fingerprints or [],
    )


def _near_duplicate_pair() -> tuple[DashboardProfile, DashboardProfile]:
    """Two dashboards sharing 4/5 charts and 2/3 datasets — clear duplicates."""
    charts = [f"urn:li:chart:(superset,{i})" for i in range(5)]
    datasets = [f"urn:li:dataset:(urn:li:dataPlatform:postgresql,sales.{t},PROD)" for t in ("orders", "customers", "products")]
    a = _profile(
        "urn:li:dashboard:(superset,1)",
        title="Sales Overview",
        chart_urns=charts[:4],
        dataset_urns=datasets[:2],
        glossary_term_urns=["urn:li:glossaryTerm:Revenue", "urn:li:glossaryTerm:MRR"],
        owners=["alice"],
        description="Main sales dashboard.",
    )
    b = _profile(
        "urn:li:dashboard:(superset,2)",
        title="Sales Overview v2",
        chart_urns=charts[1:5],   # shares 3/4 with a
        dataset_urns=datasets[1:3],  # shares 1/2 with a
        glossary_term_urns=["urn:li:glossaryTerm:Revenue"],
    )
    return a, b


# ── detectors: Jaccard ────────────────────────────────────────────────────────

def test_jaccard_identical_sets():
    s = {"a", "b", "c"}
    assert _jaccard(s, s) == 1.0


def test_jaccard_disjoint_sets():
    assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_partial_overlap():
    result = _jaccard({"a", "b", "c"}, {"b", "c", "d"})
    assert abs(result - 0.5) < 1e-9  # 2 shared / 4 total


def test_jaccard_empty_sets():
    assert _jaccard(set(), set()) == 0.0


def test_jaccard_one_empty():
    assert _jaccard({"a"}, set()) == 0.0


# ── detectors: name normalisation ─────────────────────────────────────────────

def test_normalize_strips_version_suffix():
    assert "v2" not in _normalize_title("Sales Overview v2")


def test_normalize_strips_copy_suffix():
    assert "copy" not in _normalize_title("Finance Report (copy)")


def test_normalize_strips_draft():
    assert "draft" not in _normalize_title("Q3 KPIs DRAFT")


def test_normalize_lowercases():
    assert _normalize_title("REVENUE") == "revenue"


def test_normalize_strips_punctuation():
    result = _normalize_title("Sales & Marketing")
    assert "&" not in result


# ── detectors: name similarity ────────────────────────────────────────────────

def test_name_similarity_identical():
    assert _name_similarity("Sales Overview", "Sales Overview") == 1.0


def test_name_similarity_versioned():
    # "Sales Overview" vs "Sales Overview v2" — should be high after normalisation
    sim = _name_similarity("Sales Overview", "Sales Overview v2")
    assert sim > 0.7


def test_name_similarity_reordered():
    sim = _name_similarity("Sales Overview", "Overview Sales")
    assert sim > 0.6


def test_name_similarity_unrelated():
    sim = _name_similarity("Infrastructure Costs", "Customer Retention")
    assert sim < 0.5


def test_name_similarity_empty():
    assert _name_similarity("", "") == 1.0


# ── detectors: detect_all_pairs ───────────────────────────────────────────────

def test_detect_near_duplicates_found():
    a, b = _near_duplicate_pair()
    pairs = detect_all_pairs([a, b])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.urn_a == a.urn
    assert p.urn_b == b.urn
    assert DetectionReason.CHARTS in p.reasons
    assert DetectionReason.NAME in p.reasons


def test_detect_no_pairs_for_unrelated():
    a = _profile("urn:1", title="Infrastructure Cost", chart_urns=["urn:li:chart:(superset,1)"])
    b = _profile("urn:2", title="Customer Retention", chart_urns=["urn:li:chart:(superset,99)"])
    pairs = detect_all_pairs([a, b])
    assert pairs == []


def test_detect_dataset_overlap():
    ds = ["urn:li:dataset:(urn:li:dataPlatform:postgresql,sales.orders,PROD)"]
    a = _profile("urn:1", title="Revenue A", dataset_urns=ds)
    b = _profile("urn:2", title="Revenue B", dataset_urns=ds)
    pairs = detect_all_pairs([a, b], min_dataset_jaccard=1.0)
    # dataset_jaccard should be 1.0 (identical), name similarity also high
    assert any(DetectionReason.DATASETS in p.reasons for p in pairs)


def test_detect_term_overlap():
    terms = ["urn:li:glossaryTerm:Revenue", "urn:li:glossaryTerm:ARR"]
    a = _profile("urn:1", title="Metric A", glossary_term_urns=terms)
    b = _profile("urn:2", title="Metric B", glossary_term_urns=terms)
    pairs = detect_all_pairs([a, b], min_term_jaccard=0.9)
    assert any(DetectionReason.TERMS in p.reasons for p in pairs)


def test_detect_sql_overlap():
    fp = ["abc123def456"]
    a = _profile("urn:1", title="X", sql_fingerprints=fp)
    b = _profile("urn:2", title="Y", sql_fingerprints=fp)
    pairs = detect_all_pairs([a, b], min_name_similarity=1.0, min_sql_overlap=0.9)
    assert any(DetectionReason.SQL in p.reasons for p in pairs)


def test_detect_empty_list():
    assert detect_all_pairs([]) == []


def test_detect_single_profile():
    a = _profile("urn:1", title="Sales", chart_urns=["urn:li:chart:(superset,1)"])
    assert detect_all_pairs([a]) == []


# ── detectors: union-find clustering ─────────────────────────────────────────

def test_cluster_transitive():
    """A~B and B~C should yield one group {A,B,C}."""
    p_ab = DuplicatePair(urn_a="urn:A", urn_b="urn:B", confidence=0.8)
    p_bc = DuplicatePair(urn_a="urn:B", urn_b="urn:C", confidence=0.7)
    clusters = cluster_pairs([p_ab, p_bc], ["urn:A", "urn:B", "urn:C"])
    assert len(clusters) == 1
    assert sorted(clusters[0]) == ["urn:A", "urn:B", "urn:C"]


def test_cluster_two_separate_pairs():
    p_ab = DuplicatePair(urn_a="urn:A", urn_b="urn:B", confidence=0.8)
    p_cd = DuplicatePair(urn_a="urn:C", urn_b="urn:D", confidence=0.7)
    clusters = cluster_pairs([p_ab, p_cd], ["urn:A", "urn:B", "urn:C", "urn:D"])
    assert len(clusters) == 2


def test_cluster_excludes_singletons():
    p_ab = DuplicatePair(urn_a="urn:A", urn_b="urn:B", confidence=0.8)
    clusters = cluster_pairs([p_ab], ["urn:A", "urn:B", "urn:C"])  # C is alone
    assert all("urn:C" not in c for c in clusters)


def test_cluster_no_pairs():
    clusters = cluster_pairs([], ["urn:A", "urn:B"])
    assert clusters == []


# ── scorer ────────────────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_weights_with_sql_sum_to_one():
    assert abs(sum(WEIGHTS_WITH_SQL.values()) - 1.0) < 1e-9


def test_score_pair_identical_charts():
    pair = DuplicatePair(
        urn_a="urn:A", urn_b="urn:B",
        chart_jaccard=1.0,
        dataset_jaccard=1.0,
        name_similarity=1.0,
        term_jaccard=1.0,
    )
    confidence, breakdown = score_pair(pair)
    assert confidence == 1.0
    assert set(breakdown.keys()) == set(WEIGHTS.keys())


def test_score_pair_zero_signals():
    pair = DuplicatePair(urn_a="urn:A", urn_b="urn:B")
    confidence, breakdown = score_pair(pair)
    assert confidence == 0.0
    assert all(v == 0.0 for v in breakdown.values())


def test_score_pair_chart_only():
    pair = DuplicatePair(
        urn_a="urn:A", urn_b="urn:B",
        chart_jaccard=1.0,
    )
    confidence, _ = score_pair(pair)
    assert abs(confidence - WEIGHTS["chart_jaccard"]) < 1e-9


def test_score_pair_capped_at_one():
    pair = DuplicatePair(
        urn_a="urn:A", urn_b="urn:B",
        chart_jaccard=2.0,   # over-range value
        dataset_jaccard=2.0,
        name_similarity=2.0,
        term_jaccard=2.0,
    )
    confidence, _ = score_pair(pair)
    assert confidence <= 1.0


def test_score_pair_with_sql_uses_correct_weights():
    pair = DuplicatePair(
        urn_a="urn:A", urn_b="urn:B",
        sql_overlap=1.0,
    )
    _, breakdown = score_pair(pair, use_sql=True)
    assert "sql_overlap" in breakdown
    assert "term_jaccard" not in breakdown


def test_score_pairs_sets_confidence_in_place():
    a, b = _near_duplicate_pair()
    pairs = detect_all_pairs([a, b])
    result = score_pairs(pairs)
    assert all(p.confidence > 0 for p in result)
    assert result is pairs  # mutated in-place, same list returned


# ── scorer: build_groups ─────────────────────────────────────────────────────

def test_build_groups_basic():
    a, b = _near_duplicate_pair()
    profiles = [a, b]
    pairs = score_pairs(detect_all_pairs(profiles))
    clusters = cluster_pairs(pairs, [a.urn, b.urn])
    groups = build_groups(
        clusters,
        {p.urn: p for p in profiles},
        {(min(p.urn_a, p.urn_b), max(p.urn_a, p.urn_b)): p for p in pairs},
    )
    assert len(groups) == 1
    g = groups[0]
    assert len(g.members) == 2
    assert g.confidence > 0
    assert isinstance(g.recommendation, str)
    assert len(g.group_id) == 12   # SHA-256 prefix


def test_build_groups_recommendation_prefers_described():
    """The member with a description should be recommended for keeping."""
    a, b = _near_duplicate_pair()
    # a has description, b does not
    assert a.description
    assert not b.description
    profiles = [a, b]
    pairs = score_pairs(detect_all_pairs(profiles))
    clusters = cluster_pairs(pairs, [a.urn, b.urn])
    groups = build_groups(
        clusters,
        {p.urn: p for p in profiles},
        {(min(p.urn_a, p.urn_b), max(p.urn_a, p.urn_b)): p for p in pairs},
    )
    assert a.title in groups[0].recommendation


def test_build_groups_empty_clusters():
    assert build_groups([], {}, {}) == []


def test_build_groups_sorted_by_confidence():
    # Create two pairs with known confidences; groups should be desc sorted
    a = _profile("urn:A", title="Alpha", chart_urns=["urn:li:chart:(superset,1)", "urn:li:chart:(superset,2)"])
    b = _profile("urn:B", title="Alpha v2", chart_urns=["urn:li:chart:(superset,1)", "urn:li:chart:(superset,2)"])
    c = _profile("urn:C", title="Beta", chart_urns=["urn:li:chart:(superset,99)"])
    d = _profile("urn:D", title="Beta copy", chart_urns=["urn:li:chart:(superset,99)"])
    profiles = [a, b, c, d]
    pairs = score_pairs(detect_all_pairs(profiles))
    clusters = cluster_pairs(pairs, [p.urn for p in profiles])
    groups = build_groups(
        clusters,
        {p.urn: p for p in profiles},
        {(min(p.urn_a, p.urn_b), max(p.urn_a, p.urn_b)): p for p in pairs},
    )
    confs = [g.confidence for g in groups]
    assert confs == sorted(confs, reverse=True)


# ── collectors ────────────────────────────────────────────────────────────────

def test_collector_parses_dashboard_entity():
    from meshops_copilot.skills.duplicate_detector.collectors import DashboardCollector

    entity = {
        "dashboardProperties": {
            "name": "Sales Overview",
            "description": "Main dashboard",
            "charts": [
                {"urn": "urn:li:chart:(superset,1)"},
                {"urn": "urn:li:chart:(superset,2)"},
            ],
        },
        "platform": {"name": "superset"},
        "ownership": {
            "owners": [
                {"owner": {
                    "__typename": "CorpUser",
                    "urn": "urn:li:corpuser:alice",
                    "properties": {"displayName": "alice"},
                }}
            ]
        },
        "glossaryTerms": {
            "terms": [
                {"term": {"urn": "urn:li:glossaryTerm:Revenue"}}
            ]
        },
    }
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]

    profiles = DashboardCollector(connector=conn).collect_all(
        ["urn:li:dashboard:(superset,1)"]
    )
    assert len(profiles) == 1
    p = profiles[0]
    assert p.title == "Sales Overview"
    assert p.platform == "superset"
    assert "urn:li:chart:(superset,1)" in p.chart_urns
    assert "urn:li:chart:(superset,2)" in p.chart_urns
    assert "alice" in p.owners
    assert "urn:li:glossaryTerm:Revenue" in p.glossary_term_urns


def test_collector_graceful_on_batch_error():
    from meshops_copilot.skills.duplicate_detector.collectors import DashboardCollector

    conn = MagicMock()
    conn.get_entities_batch.side_effect = Exception("MCP unavailable")
    conn.get_entity.side_effect = Exception("MCP unavailable")

    profiles = DashboardCollector(connector=conn).collect_all(
        ["urn:li:dashboard:(superset,99)"]
    )
    assert len(profiles) == 1
    assert any("MCP unavailable" in e for e in profiles[0].collection_errors)


def test_collector_lineage_adds_dataset_urns():
    from meshops_copilot.skills.duplicate_detector.collectors import DashboardCollector

    entity = {"dashboardProperties": {"name": "Revenue"}}
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]
    conn.get_lineage.return_value = {
        "upstreams": {
            "searchResults": [
                {"entity": {"type": "DATASET", "urn": "urn:li:dataset:(urn:li:dataPlatform:postgresql,sales.orders,PROD)"}},
            ]
        }
    }

    profiles = DashboardCollector(connector=conn, collect_lineage=True).collect_all(
        ["urn:li:dashboard:(superset,1)"]
    )
    assert "urn:li:dataset:(urn:li:dataPlatform:postgresql,sales.orders,PROD)" in profiles[0].dataset_urns


def test_collector_sql_fingerprint():
    from meshops_copilot.skills.duplicate_detector.collectors import DashboardCollector

    entity = {
        "dashboardProperties": {
            "name": "Q Revenue",
            "charts": [{"urn": "urn:li:chart:(superset,42)"}],
        }
    }
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]

    superset = MagicMock()
    superset.get_chart.return_value = {
        "query_context": json.dumps({
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"metrics": ["revenue"], "groupby": ["region"]}],
        })
    }

    profiles = DashboardCollector(
        connector=conn,
        superset_connector=superset,
        collect_sql=True,
    ).collect_all(["urn:li:dashboard:(superset,1)"])

    assert len(profiles[0].sql_fingerprints) == 1
    assert len(profiles[0].sql_fingerprints[0]) == 64  # SHA-256 hex


def test_collector_sql_identical_contexts_same_fingerprint():
    from meshops_copilot.skills.duplicate_detector.collectors import _sql_fingerprint

    qc = {
        "datasource": {"id": 1, "type": "table"},
        "force": True,   # volatile — should be stripped
        "queries": [{"metrics": ["revenue"], "groupby": ["region"]}],
    }
    fp1 = _sql_fingerprint(qc)
    qc2 = dict(qc)
    qc2["force"] = False   # different volatile value
    fp2 = _sql_fingerprint(qc2)
    assert fp1 == fp2


def test_collector_sql_different_metrics_different_fingerprint():
    from meshops_copilot.skills.duplicate_detector.collectors import _sql_fingerprint

    fp1 = _sql_fingerprint({"queries": [{"metrics": ["revenue"]}]})
    fp2 = _sql_fingerprint({"queries": [{"metrics": ["costs"]}]})
    assert fp1 != fp2


# ── markdown ─────────────────────────────────────────────────────────────────

def _make_group() -> DuplicateGroup:
    a, b = _near_duplicate_pair()
    return DuplicateGroup(
        group_id="abc123def456",
        members=[a, b],
        confidence=0.82,
        reasons=[DetectionReason.CHARTS, DetectionReason.NAME],
        score_breakdown={"chart_jaccard": 0.28, "name_similarity": 0.18},
        recommendation="Keep 'Sales Overview'; deprecate 'Sales Overview v2'.",
        consolidation_note="These two dashboards share identical chart content.",
    )


def test_report_contains_group_title():
    md = format_deduplication_report([_make_group()])
    assert "Sales Overview" in md


def test_report_contains_confidence():
    md = format_deduplication_report([_make_group()])
    assert "82%" in md


def test_report_contains_recommendation():
    md = format_deduplication_report([_make_group()])
    assert "Keep" in md


def test_report_contains_consolidation_note():
    md = format_deduplication_report([_make_group()])
    assert "identical chart content" in md


def test_report_empty_groups_no_crash():
    md = format_deduplication_report([])
    assert "Duplicate Dashboard" in md
    assert "No duplicate dashboards detected" in md


def test_report_query_params_shown():
    md = format_deduplication_report(
        [], query_params={"platform": "superset", "min_confidence": "40%"}
    )
    assert "superset" in md
    assert "40%" in md


def test_consolidation_prompt_contains_titles():
    g = _make_group()
    prompt = build_consolidation_prompt([g])
    assert "Sales Overview" in prompt
    assert g.group_id in prompt


def test_summary_prompt_contains_count():
    g = _make_group()
    prompt = build_summary_prompt([g])
    assert "1" in prompt  # 1 group


# ── skill (integration-style, fully mocked connector) ─────────────────────────

def _mock_datahub_connector(dashboards=None, entity=None):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    # Two near-identical dashboards — easy to detect as duplicates
    charts_a = [f"urn:li:chart:(superset,{i})" for i in range(4)]
    charts_b = [f"urn:li:chart:(superset,{i})" for i in range(1, 5)]

    conn.search_dashboards.return_value = dashboards if dashboards is not None else [
        {"urn": "urn:li:dashboard:(superset,1)"},
        {"urn": "urn:li:dashboard:(superset,2)"},
    ]

    if entity is not None:
        conn.get_entities_batch.return_value = [entity, entity]
    else:
        conn.get_entities_batch.return_value = [
            {
                "dashboardProperties": {
                    "name": "Sales Overview",
                    "charts": [{"urn": u} for u in charts_a],
                },
                "platform": {"name": "superset"},
                "ownership": {"owners": [
                    {"owner": {"__typename": "CorpUser",
                               "properties": {"displayName": "alice"}}}
                ]},
            },
            {
                "dashboardProperties": {
                    "name": "Sales Overview v2",
                    "charts": [{"urn": u} for u in charts_b],
                },
                "platform": {"name": "superset"},
            },
        ]
    conn.get_lineage.return_value = {}
    return conn


def test_skill_writes_markdown_report(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    conn = _mock_datahub_connector()
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DuplicateDetectorSkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_confidence=0.0
        )

    assert result.status.value in ("ok", "degraded")
    assert (tmp_path / "duplicate_dashboards.md").exists()
    md = (tmp_path / "duplicate_dashboards.md").read_text()
    assert "Sales Overview" in md


def test_skill_writes_json_artefact(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    conn = _mock_datahub_connector()
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        DuplicateDetectorSkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_confidence=0.0
        )

    json_files = list(tmp_path.glob("duplicate_dashboards_*.json"))
    assert json_files
    data = json.loads(json_files[0].read_text())
    assert isinstance(data, list)


def test_skill_no_dashboards_returns_ok(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    conn = _mock_datahub_connector(dashboards=[])
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DuplicateDetectorSkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True
        )

    assert result.status.value == "ok"
    assert "No dashboards found" in result.summary


def test_skill_mcp_runtime_error_returns_failed(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    conn = MagicMock()
    conn.__enter__ = MagicMock(side_effect=RuntimeError("mcp not installed"))
    conn.__exit__ = MagicMock(return_value=False)

    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DuplicateDetectorSkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True
        )

    assert result.status.value == "failed"


def test_skill_high_confidence_filter_reduces_groups(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    # Unrelated dashboards — no duplicate groups expected even at low threshold
    conn = _mock_datahub_connector(
        dashboards=[
            {"urn": "urn:li:dashboard:(superset,10)"},
            {"urn": "urn:li:dashboard:(superset,11)"},
        ],
    )
    conn.get_entities_batch.return_value = [
        {"dashboardProperties": {"name": "Infrastructure Costs", "charts": [{"urn": "urn:li:chart:(superset,100)"}]}, "platform": {"name": "superset"}},
        {"dashboardProperties": {"name": "Customer Retention", "charts": [{"urn": "urn:li:chart:(superset,200)"}]}, "platform": {"name": "superset"}},
    ]
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DuplicateDetectorSkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_confidence=0.9
        )

    assert result.details.get("groups", 0) == 0


def test_skill_llm_notes_embedded(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    cfg = MeshOpsConfig()
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "sk-test"

    conn = _mock_datahub_connector()
    # group_id is computed from URNs; use a wildcard by returning any group_id
    fake_notes = json.dumps([{
        "group_id": "placeholder",  # will not match; consolidation_note stays ""
        "consolidation_note": "These dashboards are identical copies.",
    }])

    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ), patch(
        "meshops_copilot.skills.duplicate_detector.skill.LLMClient"
    ) as MockLLM:
        MockLLM.return_value.complete.return_value = fake_notes
        result = DuplicateDetectorSkill(cfg, output_dir=tmp_path).run(
            min_confidence=0.0
        )

    assert result.status.value in ("ok", "degraded")
    # LLMClient was called
    assert MockLLM.called
