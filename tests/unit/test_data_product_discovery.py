"""Unit tests for the data_product_discovery skill."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from meshops_copilot.skills.data_product_discovery.models import (
    DataProductCandidate,
    DatasetSignals,
)
from meshops_copilot.skills.data_product_discovery.scorer import (
    WEIGHTS,
    score,
    to_candidate,
)
from meshops_copilot.skills.data_product_discovery.markdown import (
    build_llm_prompt,
    build_summary_prompt,
    format_discovery_report,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _strong() -> DatasetSignals:
    """High-signal dataset — should score well above 0.5."""
    return DatasetSignals(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgresql,commercial.contracts,PROD)",
        name="commercial.contracts",
        platform="postgresql",
        description="Core contracts dataset used by Finance, Sales, and CS.",
        domain="Finance",
        query_count_30d=450,
        unique_users_30d=23,
        downstream_dashboard_count=12,
        downstream_dataset_count=5,
        owners=["alice"],
        owner_teams=["Finance", "Sales", "CustomerSuccess"],
        schema_field_count=35,
        has_description=True,
        tags=["pii", "contracts"],
    )


def _weak() -> DatasetSignals:
    """Low-signal dataset — should score well below 0.2."""
    return DatasetSignals(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgresql,staging.tmp_load,PROD)",
        name="staging.tmp_load",
        platform="postgresql",
        query_count_30d=2,
        unique_users_30d=1,
        downstream_dashboard_count=0,
        downstream_dataset_count=0,
    )


# ── scorer ────────────────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_strong_signals_score_above_half():
    total, _ = score(_strong())
    assert total > 0.5


def test_weak_signals_score_low():
    total, _ = score(_weak())
    assert total < 0.2


def test_breakdown_keys_match_weights():
    _, breakdown = score(_strong())
    assert set(breakdown.keys()) == set(WEIGHTS.keys())


def test_breakdown_values_non_negative():
    _, breakdown = score(_strong())
    for v in breakdown.values():
        assert v >= 0.0


def test_score_capped_at_one():
    s = _strong()
    s.query_count_30d = 999_999
    s.unique_users_30d = 999_999
    s.downstream_dashboard_count = 999_999
    total, _ = score(s)
    assert total <= 1.0


def test_has_owner_contributes_to_score():
    with_owner = _weak()
    with_owner.owners = ["alice"]
    without_owner = _weak()
    total_with, _ = score(with_owner)
    total_without, _ = score(without_owner)
    assert total_with > total_without


def test_to_candidate_wraps_signals():
    c = to_candidate(_strong())
    assert isinstance(c, DataProductCandidate)
    assert c.urn == _strong().urn
    assert c.score > 0
    assert c.justification == ""


def test_to_candidate_justification_passed_through():
    c = to_candidate(_strong(), justification="Powers 12 dashboards.")
    assert c.justification == "Powers 12 dashboards."


# ── markdown ──────────────────────────────────────────────────────────────────

def test_report_contains_dataset_name():
    md = format_discovery_report([to_candidate(_strong())])
    assert "commercial.contracts" in md


def test_report_contains_score_percentage():
    md = format_discovery_report([to_candidate(_strong())])
    assert "%" in md


def test_report_shows_justification():
    c = to_candidate(_strong())
    c.justification = "Drives quarterly reporting for Finance."
    md = format_discovery_report([c])
    assert "Drives quarterly reporting for Finance." in md


def test_report_empty_candidates_does_not_crash():
    md = format_discovery_report([])
    assert "Data Product Candidate Discovery Report" in md


def test_report_query_params_appear():
    md = format_discovery_report(
        [], query_params={"domain": "finance", "platform": "postgresql", "min_score": 0.3}
    )
    assert "finance" in md
    assert "postgresql" in md


def test_build_llm_prompt_contains_urn():
    c = to_candidate(_strong())
    prompt = build_llm_prompt([c])
    assert c.urn in prompt


def test_build_llm_prompt_contains_metrics():
    c = to_candidate(_strong())
    prompt = build_llm_prompt([c])
    assert "450" in prompt   # query_count_30d
    assert "23" in prompt    # unique_users_30d
    assert "12" in prompt    # downstream_dashboard_count


def test_build_summary_prompt_contains_name():
    c = to_candidate(_strong())
    prompt = build_summary_prompt([c])
    assert "commercial.contracts" in prompt


# ── collectors ────────────────────────────────────────────────────────────────

def test_collector_graceful_on_entity_error():
    from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector

    conn = MagicMock()
    conn.get_entities_batch.side_effect = Exception("connection refused")
    conn.get_entity.side_effect = Exception("connection refused")
    conn.get_usage_stats.return_value = {}
    conn.get_lineage.return_value = {}

    results = SignalCollector(connector=conn).collect_all(["urn:li:dataset:test"])
    assert len(results) == 1
    assert any("connection refused" in e for e in results[0].collection_errors)


def test_collector_parses_usage_stats():
    from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector

    entity = {"name": "sales.orders", "platform": "postgresql"}
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]
    conn.get_usage_stats.return_value = {"query_count": 120, "unique_users": 8}
    conn.get_lineage.return_value = {}

    results = SignalCollector(connector=conn, collect_usage=True).collect_all(
        ["urn:li:dataset:test"]
    )
    assert results[0].query_count_30d == 120
    assert results[0].unique_users_30d == 8


def test_collector_falls_back_to_totalSqlQueries():
    from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector

    entity = {"name": "t", "platform": "snowflake"}
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]
    conn.get_usage_stats.return_value = {"totalSqlQueries": 77, "uniqueUserCount": 4}
    conn.get_lineage.return_value = {}

    results = SignalCollector(connector=conn, collect_usage=True).collect_all(
        ["urn:li:dataset:t"]
    )
    assert results[0].query_count_30d == 77
    assert results[0].unique_users_30d == 4


def test_collector_parses_lineage_types():
    from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector

    entity = {"name": "t", "platform": "postgresql"}
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]
    conn.get_usage_stats.return_value = {}
    conn.get_lineage.return_value = {
        "entities": [
            {"type": "DASHBOARD"},
            {"type": "DASHBOARD"},
            {"type": "CHART"},
            {"type": "DATASET"},
        ]
    }

    results = SignalCollector(connector=conn, collect_lineage=True).collect_all(
        ["urn:li:dataset:t"]
    )
    assert results[0].downstream_dashboard_count == 2
    assert results[0].downstream_chart_count == 1
    assert results[0].downstream_dataset_count == 1


def test_collector_parses_ownership():
    from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector

    # Uses actual DataHub GQL structure: CorpUser and CorpGroup are separate owner entries.
    entity = {
        "name": "t",
        "platform": "postgresql",
        "ownership": {
            "owners": [
                # CorpUser — contributes to owners list
                {"owner": {
                    "__typename": "CorpUser",
                    "urn": "urn:li:corpuser:alice",
                    "properties": {"displayName": "alice", "email": "alice@example.com"},
                }},
                # CorpGroup — contributes to both owners and owner_teams
                {"owner": {
                    "__typename": "CorpGroup",
                    "urn": "urn:li:corpGroup:Finance",
                    "name": "Finance",
                    "properties": {"displayName": "Finance Team"},
                }},
                # Another CorpGroup
                {"owner": {
                    "__typename": "CorpGroup",
                    "urn": "urn:li:corpGroup:Sales",
                    "name": "Sales",
                }},
            ]
        },
    }
    conn = MagicMock()
    conn.get_entities_batch.return_value = [entity]
    conn.get_usage_stats.return_value = {}
    conn.get_lineage.return_value = {}

    results = SignalCollector(connector=conn).collect_all(["urn:li:dataset:t"])
    assert "alice" in results[0].owners
    assert "Finance" in results[0].owner_teams
    assert "Sales" in results[0].owner_teams


# ── skill (integration-style, fully mocked connector) ─────────────────────────

def _mock_connector(datasets=None, entity=None, usage=None, lineage=None):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.search_datasets.return_value = (
        datasets if datasets is not None
        else [{"urn": "urn:li:dataset:(urn:li:dataPlatform:postgresql,commercial.contracts,PROD)"}]
    )
    _entity = entity or {
        "name": "commercial.contracts",
        "platform": "postgresql",
        "description": "Core contracts dataset.",
    }
    conn.get_entity.return_value = _entity
    conn.get_entities_batch.return_value = [_entity]
    conn.get_usage_stats.return_value = usage or {
        "query_count": 450, "unique_users": 23
    }
    conn.get_lineage.return_value = lineage or {
        "entities": [{"type": "DASHBOARD"}, {"type": "DASHBOARD"}]
    }
    return conn


def test_skill_writes_markdown_report(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    conn = _mock_connector()
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DataProductDiscoverySkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_score=0.0
        )

    assert result.status.value in ("ok", "degraded")
    assert (tmp_path / "data_products.md").exists()
    md = (tmp_path / "data_products.md").read_text()
    assert "commercial.contracts" in md


def test_skill_writes_json_artefact(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    conn = _mock_connector()
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        DataProductDiscoverySkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_score=0.0
        )

    json_files = list(tmp_path.glob("data_products_*.json"))
    assert json_files
    data = json.loads(json_files[0].read_text())
    assert isinstance(data, list)
    assert data[0]["urn"] == (
        "urn:li:dataset:(urn:li:dataPlatform:postgresql,commercial.contracts,PROD)"
    )


def test_skill_no_datasets_returns_ok(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    conn = _mock_connector(datasets=[])
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DataProductDiscoverySkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True
        )

    assert result.status.value == "ok"
    assert "No datasets found" in result.summary


def test_skill_mcp_runtime_error_returns_failed(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    conn = MagicMock()
    conn.__enter__ = MagicMock(side_effect=RuntimeError("mcp not installed"))
    conn.__exit__ = MagicMock(return_value=False)

    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DataProductDiscoverySkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True
        )

    assert result.status.value == "failed"


def test_skill_min_score_filters_candidates(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    # Weak dataset — zero signals → score ~0
    conn = _mock_connector(
        usage={"query_count": 0, "unique_users": 0},
        lineage={"entities": []},
        entity={"name": "staging.tmp", "platform": "postgresql"},
    )
    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ):
        result = DataProductDiscoverySkill(MeshOpsConfig(), output_dir=tmp_path).run(
            no_llm=True, min_score=0.5
        )

    assert result.details.get("candidates", 0) == 0


def test_skill_llm_justifications_embedded(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    cfg = MeshOpsConfig()
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "sk-test"

    conn = _mock_connector()
    fake_justifications = json.dumps([{
        "urn": "urn:li:dataset:(urn:li:dataPlatform:postgresql,commercial.contracts,PROD)",
        "justification": "Powers 12 dashboards across Finance and Sales.",
    }])

    with patch(
        "meshops_copilot.connectors.datahub_mcp.DataHubMCPConnector",
        return_value=conn,
    ), patch(
        "meshops_copilot.skills.data_product_discovery.skill.LLMClient"
    ) as MockLLM:
        MockLLM.return_value.complete.return_value = fake_justifications
        DataProductDiscoverySkill(cfg, output_dir=tmp_path).run(min_score=0.0)

    md = (tmp_path / "data_products.md").read_text()
    assert "Powers 12 dashboards across Finance and Sales." in md
