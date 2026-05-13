"""Unit tests for the name-based dashboard auditor (duplicate_detector/auditor.py)."""

from __future__ import annotations

import pytest

from meshops_copilot.skills.duplicate_detector.auditor import (
    # Data models
    AuditResult,
    AuditSummary,
    DashboardCluster,
    DashboardClassification,
    DashboardAction,
    DashboardRecord,
    # Private helpers (tested directly for thorough coverage)
    _extract_superset_id,
    _extract_team_prefix,
    _detect_status_tokens,
    _extract_core_topic,
    _is_untitled,
    _is_unsupported_ownership,
    _looks_like_personal_name,
    _is_personal_dashboard,
    _parse_entity,
    _classify_record,
    _mark_exact_duplicates,
    _topic_similarity,
    _cluster_records,
    _build_summary,
    # Public entry point
    build_audit,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _record(
    name: str = "Sales Overview",
    urn: str = "urn:li:dashboard:(superset,1)",
    superset_id: int | None = 1,
    team_prefix: str = "",
    status_tokens: list[str] | None = None,
    classification: DashboardClassification = DashboardClassification.GOLDEN_CANDIDATE,
    action: DashboardAction = DashboardAction.KEEP,
    replacement_id: int | None = None,
) -> DashboardRecord:
    core_topic = _extract_core_topic(name)
    return DashboardRecord(
        urn=urn,
        superset_id=superset_id,
        name=name,
        team_prefix=team_prefix,
        core_topic=core_topic,
        status_tokens=status_tokens if status_tokens is not None else [],
        classification=classification,
        action=action,
        replacement_id=replacement_id,
    )


def _entity(
    urn: str = "urn:li:dashboard:(superset,1)",
    name: str = "Sales Overview",
    via_dash_props: bool = True,
) -> dict:
    """Build a minimal DataHub entity dict."""
    if via_dash_props:
        return {"urn": urn, "dashboardProperties": {"name": name}}
    return {"urn": urn, "properties": {"name": name}}


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_superset_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractSupersetId:
    def test_standard_urn(self):
        assert _extract_superset_id("urn:li:dashboard:(superset,42)") == 42

    def test_large_id(self):
        assert _extract_superset_id("urn:li:dashboard:(superset,99999)") == 99999

    def test_non_superset_urn_falls_back_to_last_digits(self):
        result = _extract_superset_id("urn:li:dashboard:(looker,7)")
        assert result == 7

    def test_urn_with_no_digits_returns_none(self):
        result = _extract_superset_id("urn:li:dashboard:(looker,abc)")
        assert result is None

    def test_empty_string_returns_none(self):
        assert _extract_superset_id("") is None


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_team_prefix
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTeamPrefix:
    def test_simple_bracket_prefix(self):
        team, rest = _extract_team_prefix("[Data] Sales Overview")
        assert team == "Data"
        assert rest == "Sales Overview"

    def test_prefix_with_spaces(self):
        team, rest = _extract_team_prefix("[ Marketing ] Revenue Dashboard")
        assert team == "Marketing"
        assert rest == "Revenue Dashboard"

    def test_no_prefix(self):
        team, rest = _extract_team_prefix("Sales Overview")
        assert team == ""
        assert rest == "Sales Overview"

    def test_empty_brackets(self):
        # Regex requires at least one char inside brackets — empty [] is not treated as a prefix.
        team, rest = _extract_team_prefix("[] Some Name")
        assert team == ""
        assert rest == "[] Some Name"

    def test_prefix_only(self):
        team, rest = _extract_team_prefix("[Finance]")
        assert team == "Finance"
        assert rest == ""

    def test_no_space_after_bracket(self):
        team, rest = _extract_team_prefix("[Engineering]Dashboard")
        assert team == "Engineering"
        assert rest == "Dashboard"


# ═══════════════════════════════════════════════════════════════════════════════
# _detect_status_tokens
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectStatusTokens:
    def test_copy_token(self):
        assert "copy" in _detect_status_tokens("Sales Overview Copy")

    def test_copy_of_variant(self):
        assert "copy" in _detect_status_tokens("Copy of Sales Overview")

    def test_version_token_v2(self):
        assert "version" in _detect_status_tokens("Sales Overview v2")

    def test_version_token_spelled_out(self):
        assert "version" in _detect_status_tokens("Sales Overview Version 3")

    def test_date_token_year(self):
        assert "date" in _detect_status_tokens("Revenue 2023")

    def test_date_token_quarter(self):
        assert "date" in _detect_status_tokens("Q1 2024 Revenue")

    def test_date_token_full_date(self):
        assert "date" in _detect_status_tokens("Report 2024-01-15")

    def test_env_token_dev(self):
        assert "env" in _detect_status_tokens("Sales Overview Dev")

    def test_env_token_staging(self):
        assert "env" in _detect_status_tokens("Sales Overview staging")

    def test_wip_token(self):
        assert "wip" in _detect_status_tokens("Sales WIP")

    def test_draft_token(self):
        assert "wip" in _detect_status_tokens("Draft Sales Overview")

    def test_deprecated_token(self):
        assert "deprecated" in _detect_status_tokens("Sales Overview - deprecated")

    def test_legacy_token(self):
        assert "deprecated" in _detect_status_tokens("[Legacy] Sales Overview")

    def test_old_token(self):
        assert "deprecated" in _detect_status_tokens("Old Sales Overview")

    def test_unsupported_token(self):
        assert "unsupported" in _detect_status_tokens("Sales Overview [Unsupported]")

    def test_broken_token(self):
        assert "broken" in _detect_status_tokens("Sales Overview - broken")

    def test_backup_token(self):
        assert "broken" in _detect_status_tokens("Sales Backup")

    def test_personal_token(self):
        assert "personal" in _detect_status_tokens("Personal Sales Dashboard")

    def test_no_tokens_clean_name(self):
        assert _detect_status_tokens("Sales Overview") == []

    def test_multiple_tokens(self):
        tokens = _detect_status_tokens("Old Sales Overview Copy v2")
        assert "deprecated" in tokens
        assert "copy" in tokens
        assert "version" in tokens


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_core_topic
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractCoreTopic:
    def test_clean_name(self):
        assert _extract_core_topic("Sales Overview") == "sales overview"

    def test_strips_team_prefix(self):
        assert _extract_core_topic("[Data] Sales Overview") == "sales overview"

    def test_strips_status_keywords(self):
        topic = _extract_core_topic("Old Sales Overview")
        assert "old" not in topic
        assert "sales" in topic
        assert "overview" in topic

    def test_strips_version_marker(self):
        topic = _extract_core_topic("Sales Overview v2")
        assert "v2" not in topic

    def test_strips_year(self):
        topic = _extract_core_topic("Revenue 2023")
        assert "2023" not in topic

    def test_strips_parenthesised_content(self):
        topic = _extract_core_topic("Sales Overview (WIP)")
        assert "wip" not in topic

    def test_strips_bracketed_content_in_name(self):
        # Status keywords in square brackets (not at start) should be stripped
        topic = _extract_core_topic("Sales Overview [Draft]")
        assert "draft" not in topic

    def test_normalises_case_and_whitespace(self):
        topic = _extract_core_topic("  Sales   Overview  ")
        assert topic == "sales overview"

    def test_removes_punctuation(self):
        topic = _extract_core_topic("Sales/Revenue Overview")
        assert "/" not in topic

    def test_empty_string_gives_empty(self):
        assert _extract_core_topic("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# _is_untitled
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsUntitled:
    def test_empty_string(self):
        assert _is_untitled("") is True

    def test_whitespace_only(self):
        assert _is_untitled("   ") is True

    def test_untitled_keyword(self):
        assert _is_untitled("Untitled") is True

    def test_new_dashboard(self):
        assert _is_untitled("New Dashboard") is True

    def test_dashboard_with_number(self):
        assert _is_untitled("Dashboard 3") is True

    def test_unnamed(self):
        assert _is_untitled("Unnamed") is True

    def test_na(self):
        assert _is_untitled("N/A") is True

    def test_dashes(self):
        assert _is_untitled("---") is True

    def test_normal_name_is_not_untitled(self):
        assert _is_untitled("Sales Overview") is False

    def test_team_prefixed_name_is_not_untitled(self):
        assert _is_untitled("[Data] Sales Overview") is False


# ═══════════════════════════════════════════════════════════════════════════════
# _is_unsupported_ownership
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsUnsupportedOwnership:
    def test_unsupported_prefix(self):
        assert _is_unsupported_ownership("unsupported") is True

    def test_deprecated_prefix(self):
        assert _is_unsupported_ownership("Deprecated") is True

    def test_legacy_prefix(self):
        assert _is_unsupported_ownership("legacy") is True

    def test_archived_prefix(self):
        assert _is_unsupported_ownership("archived") is True

    def test_abandoned_prefix(self):
        assert _is_unsupported_ownership("abandoned") is True

    def test_normal_team_prefix_is_not_unsupported(self):
        assert _is_unsupported_ownership("Data") is False

    def test_sales_prefix_is_not_unsupported(self):
        assert _is_unsupported_ownership("Sales") is False

    def test_empty_string_is_not_unsupported(self):
        assert _is_unsupported_ownership("") is False


# ═══════════════════════════════════════════════════════════════════════════════
# _looks_like_personal_name
# ═══════════════════════════════════════════════════════════════════════════════

class TestLooksLikePersonalName:
    def test_first_name_candidate(self):
        assert _looks_like_personal_name("Alice") is True

    def test_capitalised_non_dept_word(self):
        assert _looks_like_personal_name("John") is True

    def test_dept_word_is_not_personal(self):
        assert _looks_like_personal_name("Sales") is False

    def test_all_caps_acronym_is_not_personal(self):
        assert _looks_like_personal_name("BI") is False

    def test_single_char_is_not_personal(self):
        assert _looks_like_personal_name("A") is False

    def test_lowercase_is_not_personal(self):
        assert _looks_like_personal_name("alice") is False


# ═══════════════════════════════════════════════════════════════════════════════
# _is_personal_dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsPersonalDashboard:
    def test_personal_keyword_no_team(self):
        assert _is_personal_dashboard("Personal Sales Dashboard", "") is True

    def test_playground_keyword_no_team(self):
        assert _is_personal_dashboard("playground stuff", "") is True

    def test_mine_keyword(self):
        assert _is_personal_dashboard("mine overview", "") is True

    def test_team_owned_is_not_personal(self):
        assert _is_personal_dashboard("Alice Bob Sales", "Data") is False

    def test_two_capitalised_non_dept_words_look_personal(self):
        # "Alice Bob" → personal heuristic
        assert _is_personal_dashboard("Alice Bob Sales Overview", "") is True

    def test_dept_prefix_words_are_not_personal(self):
        # "Sales Overview" — only one non-dept capitalised word
        assert _is_personal_dashboard("Sales Overview", "") is False

    def test_clean_team_dashboard_is_not_personal(self):
        assert _is_personal_dashboard("Revenue Dashboard", "") is False


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_entity
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseEntity:
    def test_parses_dash_props_name(self):
        entity = {"urn": "urn:li:dashboard:(superset,5)", "dashboardProperties": {"name": "Revenue"}}
        rec = _parse_entity(entity)
        assert rec is not None
        assert rec.name == "Revenue"
        assert rec.superset_id == 5

    def test_parses_properties_name_fallback(self):
        entity = {"urn": "urn:li:dashboard:(superset,7)", "properties": {"name": "Costs"}}
        rec = _parse_entity(entity)
        assert rec is not None
        assert rec.name == "Costs"

    def test_parses_top_level_name(self):
        entity = {"urn": "urn:li:dashboard:(superset,3)", "name": "Top Level"}
        rec = _parse_entity(entity)
        assert rec is not None
        assert rec.name == "Top Level"

    def test_parses_title_fallback(self):
        entity = {"urn": "urn:li:dashboard:(superset,9)", "title": "Title Field"}
        rec = _parse_entity(entity)
        assert rec is not None
        assert rec.name == "Title Field"

    def test_returns_none_for_missing_urn(self):
        entity = {"dashboardProperties": {"name": "No URN"}}
        assert _parse_entity(entity) is None

    def test_returns_none_for_non_dict(self):
        assert _parse_entity("not a dict") is None  # type: ignore[arg-type]
        assert _parse_entity(None) is None           # type: ignore[arg-type]

    def test_extracts_team_prefix(self):
        entity = {"urn": "urn:li:dashboard:(superset,1)", "dashboardProperties": {"name": "[Sales] Revenue"}}
        rec = _parse_entity(entity)
        assert rec is not None
        assert rec.team_prefix == "Sales"

    def test_detects_status_tokens(self):
        entity = {"urn": "urn:li:dashboard:(superset,1)", "dashboardProperties": {"name": "Revenue v2"}}
        rec = _parse_entity(entity)
        assert rec is not None
        assert "version" in rec.status_tokens


# ═══════════════════════════════════════════════════════════════════════════════
# _classify_record
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyRecord:
    DC = DashboardClassification
    DA = DashboardAction

    def test_clean_name_is_golden(self):
        rec = _record(name="Sales Overview")
        cls, act = _classify_record(rec)
        assert cls == self.DC.GOLDEN_CANDIDATE
        assert act == self.DA.KEEP

    def test_untitled_is_deprecate(self):
        rec = _record(name="Untitled", status_tokens=[])
        cls, act = _classify_record(rec)
        assert cls == self.DC.UNTITLED
        assert act == self.DA.DEPRECATE

    def test_unsupported_ownership_prefix(self):
        rec = _record(name="[Legacy] Sales", team_prefix="Legacy")
        cls, act = _classify_record(rec)
        assert cls == self.DC.UNSUPPORTED_OWNERSHIP
        assert act == self.DA.DEPRECATE

    def test_personal_dashboard(self):
        rec = _record(name="Alice Bob Overview", team_prefix="")
        cls, act = _classify_record(rec)
        assert cls == self.DC.PERSONAL
        assert act == self.DA.REVIEW

    def test_unsupported_token(self):
        rec = _record(name="Unsupported Sales", status_tokens=["unsupported"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.LEGACY
        assert act == self.DA.DEPRECATE

    def test_deprecated_token(self):
        rec = _record(name="Old Sales Overview", status_tokens=["deprecated"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.LEGACY
        assert act == self.DA.DEPRECATE

    def test_broken_token(self):
        rec = _record(name="Sales Backup", status_tokens=["broken"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.BROKEN_BACKUP
        assert act == self.DA.DEPRECATE

    def test_copy_token(self):
        rec = _record(name="Sales Copy", status_tokens=["copy"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.PERSONAL_COPY
        assert act == self.DA.DEPRECATE

    def test_wip_token(self):
        rec = _record(name="Sales WIP", status_tokens=["wip"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.WIP_DRAFT
        assert act == self.DA.REVIEW

    def test_personal_token(self):
        rec = _record(name="Personal Sales", status_tokens=["personal"])
        cls, act = _classify_record(rec)
        assert cls == self.DC.PERSONAL
        assert act == self.DA.REVIEW

    def test_version_no_clean_sibling_is_active_sub_cut(self):
        rec = _record(name="Sales v2", status_tokens=["version"])
        cls, act = _classify_record(rec, cluster_members=None)
        assert cls == self.DC.ACTIVE_SUB_CUT
        assert act == self.DA.REVIEW

    def test_version_with_clean_sibling_is_superseded(self):
        golden = _record(
            name="Sales Overview",
            urn="urn:li:dashboard:(superset,10)",
            superset_id=10,
            status_tokens=[],
            classification=DashboardClassification.GOLDEN_CANDIDATE,
            action=DashboardAction.KEEP,
        )
        versioned = _record(name="Sales v2", status_tokens=["version"])
        cls, act = _classify_record(versioned, cluster_members=[golden, versioned])
        assert cls == self.DC.SUPERSEDED_VERSION
        assert act == self.DA.DEPRECATE

    def test_date_token_no_sibling_is_active_sub_cut(self):
        rec = _record(name="Revenue 2023", status_tokens=["date"])
        cls, act = _classify_record(rec, cluster_members=None)
        assert cls == self.DC.ACTIVE_SUB_CUT


# ═══════════════════════════════════════════════════════════════════════════════
# _mark_exact_duplicates
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarkExactDuplicates:
    def test_highest_id_kept_lower_deprecated(self):
        r1 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,5)", superset_id=5)
        _mark_exact_duplicates([r1, r2])
        assert r2.classification == DashboardClassification.GOLDEN_CANDIDATE
        assert r2.action == DashboardAction.KEEP
        assert r1.classification == DashboardClassification.EXACT_DUPLICATE
        assert r1.action == DashboardAction.DEPRECATE
        assert r1.replacement_id == 5

    def test_three_exact_duplicates(self):
        r1 = _record(name="Revenue", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Revenue", urn="urn:li:dashboard:(superset,3)", superset_id=3)
        r3 = _record(name="Revenue", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        _mark_exact_duplicates([r1, r2, r3])
        # r2 (id=3) is highest → kept
        assert r2.classification == DashboardClassification.GOLDEN_CANDIDATE
        assert r1.classification == DashboardClassification.EXACT_DUPLICATE
        assert r3.classification == DashboardClassification.EXACT_DUPLICATE

    def test_case_insensitive_matching(self):
        r1 = _record(name="sales overview", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        _mark_exact_duplicates([r1, r2])
        assert r1.classification == DashboardClassification.EXACT_DUPLICATE
        assert r2.classification == DashboardClassification.GOLDEN_CANDIDATE

    def test_unique_names_not_touched(self):
        r1 = _record(name="Sales", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Revenue", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        _mark_exact_duplicates([r1, r2])
        assert r1.classification == DashboardClassification.GOLDEN_CANDIDATE
        assert r2.classification == DashboardClassification.GOLDEN_CANDIDATE

    def test_replacement_id_points_to_golden(self):
        r1 = _record(name="Costs", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Costs", urn="urn:li:dashboard:(superset,99)", superset_id=99)
        _mark_exact_duplicates([r1, r2])
        assert r1.replacement_id == 99


# ═══════════════════════════════════════════════════════════════════════════════
# _topic_similarity
# ═══════════════════════════════════════════════════════════════════════════════

class TestTopicSimilarity:
    def test_identical_strings(self):
        assert _topic_similarity("sales overview", "sales overview") == 1.0

    def test_completely_different_strings(self):
        score = _topic_similarity("sales overview", "infrastructure costs")
        assert score < 0.5

    def test_empty_strings_return_zero(self):
        assert _topic_similarity("", "sales") == 0.0
        assert _topic_similarity("sales", "") == 0.0
        assert _topic_similarity("", "") == 0.0

    def test_similar_strings_above_threshold(self):
        # "sales overview" vs "sales overvieww" — very close
        score = _topic_similarity("sales overview", "sales overvieww")
        assert score > 0.82

    def test_dissimilar_strings_below_threshold(self):
        score = _topic_similarity("revenue", "completely different topic here")
        assert score < 0.82


# ═══════════════════════════════════════════════════════════════════════════════
# _cluster_records
# ═══════════════════════════════════════════════════════════════════════════════

class TestClusterRecords:
    def test_identical_topic_grouped(self):
        r1 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Sales Overview Copy", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        # Both strip to "sales overview"
        r1.core_topic = "sales overview"
        r2.core_topic = "sales overview"
        clusters = _cluster_records([r1, r2])
        topic_cluster = next(c for c in clusters if "sales" in c.topic)
        assert len(topic_cluster.members) == 2

    def test_different_topics_become_separate_clusters(self):
        r1 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Infrastructure Costs", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        r1.core_topic = "sales overview"
        r2.core_topic = "infrastructure costs"
        clusters = _cluster_records([r1, r2])
        assert len(clusters) == 2

    def test_clusters_sorted_descending_by_member_count(self):
        records = []
        for i in range(3):
            r = _record(
                name="Sales Overview",
                urn=f"urn:li:dashboard:(superset,{i})",
                superset_id=i,
            )
            r.core_topic = "sales overview"
            records.append(r)
        lone = _record(name="Revenue", urn="urn:li:dashboard:(superset,99)", superset_id=99)
        lone.core_topic = "revenue"
        clusters = _cluster_records(records + [lone])
        assert len(clusters[0].members) >= len(clusters[-1].members)

    def test_empty_topic_becomes_singleton_with_name(self):
        r = _record(name="n/a", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r.core_topic = ""  # too short for grouping
        clusters = _cluster_records([r])
        assert len(clusters) == 1
        assert clusters[0].members[0] is r

    def test_fuzzy_similar_topics_merged(self):
        r1 = _record(name="Sales Overview", urn="urn:li:dashboard:(superset,1)", superset_id=1)
        r2 = _record(name="Sales Overvieww", urn="urn:li:dashboard:(superset,2)", superset_id=2)
        r1.core_topic = "sales overview"
        r2.core_topic = "sales overvieww"  # one extra char — similarity > 0.82
        clusters = _cluster_records([r1, r2])
        # Should be merged into one cluster
        multi = [c for c in clusters if len(c.members) > 1]
        assert len(multi) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _build_summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildSummary:
    DC = DashboardClassification
    DA = DashboardAction

    def _make(self, classification: DashboardClassification, tokens: list[str] = (), team: str = "") -> DashboardRecord:
        r = _record(
            name="X",
            classification=classification,
            action=DashboardAction.DEPRECATE,
            team_prefix=team,
            status_tokens=list(tokens),
        )
        return r

    def test_total_count(self):
        records = [self._make(self.DC.GOLDEN_CANDIDATE) for _ in range(4)]
        summary = _build_summary(records)
        assert summary.total == 4

    def test_exact_duplicate_counted(self):
        r = self._make(self.DC.EXACT_DUPLICATE)
        summary = _build_summary([r])
        assert summary.exact_duplicates == 1

    def test_legacy_deprecated_counted(self):
        r = self._make(self.DC.LEGACY, tokens=["deprecated"])
        summary = _build_summary([r])
        assert summary.deprecated_legacy == 1

    def test_legacy_unsupported_token_counted(self):
        r = self._make(self.DC.LEGACY, tokens=["unsupported"])
        summary = _build_summary([r])
        assert summary.unsupported == 1

    def test_unsupported_ownership_counted(self):
        r = self._make(self.DC.UNSUPPORTED_OWNERSHIP, team="legacy")
        summary = _build_summary([r])
        assert summary.unsupported == 1

    def test_personal_copy_counted(self):
        r = self._make(self.DC.PERSONAL_COPY)
        summary = _build_summary([r])
        assert summary.copy_tokens == 1

    def test_superseded_version_counted_as_old_test_dev(self):
        r = self._make(self.DC.SUPERSEDED_VERSION)
        summary = _build_summary([r])
        assert summary.old_test_dev == 1

    def test_active_sub_cut_counted_as_old_test_dev(self):
        r = self._make(self.DC.ACTIVE_SUB_CUT)
        summary = _build_summary([r])
        assert summary.old_test_dev == 1

    def test_wip_draft_counted(self):
        r = self._make(self.DC.WIP_DRAFT)
        summary = _build_summary([r])
        assert summary.wip_draft == 1

    def test_untitled_counted(self):
        r = self._make(self.DC.UNTITLED)
        summary = _build_summary([r])
        assert summary.untitled == 1

    def test_personal_counted(self):
        r = self._make(self.DC.PERSONAL)
        summary = _build_summary([r])
        assert summary.personal == 1

    def test_broken_backup_counted_as_personal(self):
        r = self._make(self.DC.BROKEN_BACKUP)
        summary = _build_summary([r])
        assert summary.personal == 1

    def test_total_deprecation_candidates_sums_correctly(self):
        records = [
            self._make(self.DC.EXACT_DUPLICATE),
            self._make(self.DC.LEGACY, tokens=["deprecated"]),
            self._make(self.DC.PERSONAL_COPY),
            self._make(self.DC.WIP_DRAFT),
            self._make(self.DC.UNTITLED),
        ]
        summary = _build_summary(records)
        assert summary.total_deprecation_candidates == 5

    def test_golden_candidate_not_counted_in_any_bucket(self):
        r = self._make(self.DC.GOLDEN_CANDIDATE)
        summary = _build_summary([r])
        assert summary.total_deprecation_candidates == 0


# ═══════════════════════════════════════════════════════════════════════════════
# AuditSummary.total_deprecation_candidates property
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditSummaryProperty:
    def test_empty_summary_gives_zero(self):
        s = AuditSummary(total=0)
        assert s.total_deprecation_candidates == 0

    def test_all_buckets_summed(self):
        s = AuditSummary(
            total=20,
            exact_duplicates=2,
            deprecated_legacy=3,
            unsupported=1,
            copy_tokens=2,
            old_test_dev=4,
            wip_draft=1,
            untitled=2,
            personal=5,
        )
        assert s.total_deprecation_candidates == 20


# ═══════════════════════════════════════════════════════════════════════════════
# build_audit (integration / entry-point tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildAudit:
    def test_returns_audit_result(self):
        entities = [_entity("urn:li:dashboard:(superset,1)", "Sales Overview")]
        result = build_audit(entities)
        assert isinstance(result, AuditResult)

    def test_skips_invalid_entities(self):
        entities = [
            {"dashboardProperties": {"name": "No URN"}},  # missing urn
            "not a dict",                                   # wrong type
            None,                                           # None
            _entity("urn:li:dashboard:(superset,1)", "Valid"),
        ]
        result = build_audit(entities)
        assert len(result.all_records) == 1

    def test_single_clean_dashboard_is_golden(self):
        result = build_audit([_entity("urn:li:dashboard:(superset,10)", "Revenue Overview")])
        assert len(result.all_records) == 1
        assert result.all_records[0].classification == DashboardClassification.GOLDEN_CANDIDATE

    def test_exact_duplicates_marked(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Sales Overview"),
            _entity("urn:li:dashboard:(superset,5)", "Sales Overview"),
        ]
        result = build_audit(entities)
        # One kept, one duplicate
        classes = {r.classification for r in result.all_records}
        assert DashboardClassification.EXACT_DUPLICATE in classes
        assert DashboardClassification.GOLDEN_CANDIDATE in classes

    def test_deprecated_name_classified(self):
        entities = [_entity("urn:li:dashboard:(superset,1)", "Old Sales Overview")]
        result = build_audit(entities)
        assert result.all_records[0].classification == DashboardClassification.LEGACY

    def test_untitled_classified(self):
        entities = [_entity("urn:li:dashboard:(superset,1)", "Untitled")]
        result = build_audit(entities)
        assert result.all_records[0].classification == DashboardClassification.UNTITLED

    def test_wip_classified(self):
        entities = [_entity("urn:li:dashboard:(superset,1)", "Sales WIP")]
        result = build_audit(entities)
        assert result.all_records[0].classification == DashboardClassification.WIP_DRAFT

    def test_version_with_golden_sibling_superseded(self):
        entities = [
            _entity("urn:li:dashboard:(superset,10)", "Sales Overview"),
            _entity("urn:li:dashboard:(superset,2)", "Sales Overview v2"),
        ]
        result = build_audit(entities)
        v2_record = next(r for r in result.all_records if "v2" in r.name.lower())
        # After second-pass, v2 should be SUPERSEDED_VERSION because golden exists
        assert v2_record.classification == DashboardClassification.SUPERSEDED_VERSION
        assert v2_record.action == DashboardAction.DEPRECATE

    def test_clusters_created(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Sales Overview"),
            _entity("urn:li:dashboard:(superset,2)", "Sales Overview Copy"),
        ]
        result = build_audit(entities)
        assert len(result.clusters) >= 1

    def test_summary_counts_total(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Sales Overview"),
            _entity("urn:li:dashboard:(superset,2)", "Revenue Dashboard"),
        ]
        result = build_audit(entities)
        assert result.summary.total == 2

    def test_summary_counts_untitled(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Untitled"),
            _entity("urn:li:dashboard:(superset,2)", "Sales Overview"),
        ]
        result = build_audit(entities)
        assert result.summary.untitled == 1

    def test_summary_counts_exact_duplicates(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Sales"),
            _entity("urn:li:dashboard:(superset,2)", "Sales"),
            _entity("urn:li:dashboard:(superset,3)", "Sales"),
        ]
        result = build_audit(entities)
        assert result.summary.exact_duplicates == 2

    def test_empty_entities_list(self):
        result = build_audit([])
        assert result.all_records == []
        assert result.summary.total == 0

    def test_unsupported_ownership_prefix(self):
        entities = [
            {
                "urn": "urn:li:dashboard:(superset,1)",
                "dashboardProperties": {"name": "[Legacy] Sales Overview"},
            }
        ]
        result = build_audit(entities)
        assert result.all_records[0].classification == DashboardClassification.UNSUPPORTED_OWNERSHIP

    def test_replacement_id_set_for_duplicate(self):
        entities = [
            _entity("urn:li:dashboard:(superset,1)", "Revenue"),
            _entity("urn:li:dashboard:(superset,99)", "Revenue"),
        ]
        result = build_audit(entities)
        dup = next(r for r in result.all_records if r.classification == DashboardClassification.EXACT_DUPLICATE)
        assert dup.replacement_id == 99

    def test_properties_name_fallback_parsed(self):
        entities = [{"urn": "urn:li:dashboard:(superset,7)", "properties": {"name": "Costs Dashboard"}}]
        result = build_audit(entities)
        assert result.all_records[0].name == "Costs Dashboard"

    def test_copy_token_classified(self):
        entities = [_entity("urn:li:dashboard:(superset,1)", "Sales Copy")]
        result = build_audit(entities)
        assert result.all_records[0].classification == DashboardClassification.PERSONAL_COPY
