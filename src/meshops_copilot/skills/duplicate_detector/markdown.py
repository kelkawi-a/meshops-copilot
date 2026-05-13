"""Markdown report generation and LLM prompt builders for duplicate_detector."""

from __future__ import annotations

from datetime import datetime

from meshops_copilot.skills.duplicate_detector.auditor import (
    AuditResult,
    DashboardAction,
    DashboardClassification,
)

from meshops_copilot.skills.duplicate_detector.models import (
    DetectionReason,
    DuplicateGroup,
)

# ── Report formatting ──────────────────────────────────────────────────────────

_REASON_LABELS: dict[DetectionReason, str] = {
    DetectionReason.CHARTS: "shared charts",
    DetectionReason.DATASETS: "shared datasets",
    DetectionReason.NAME: "similar name",
    DetectionReason.TERMS: "shared glossary terms",
    DetectionReason.SQL: "identical SQL patterns",
}


def format_deduplication_report(
    groups: list[DuplicateGroup],
    query_params: dict | None = None,
    llm_summary: str = "",
) -> str:
    """Render duplicate groups as a Markdown report."""
    params = query_params or {}
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append("# Duplicate Dashboard & Metric Detection Report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")

    # ── Query parameters ──────────────────────────────────────────────────────
    if any(params.values()):
        lines.append("## Scan Parameters")
        lines.append("")
        for k, v in params.items():
            if v is not None and v != "":
                lines.append(f"- **{k}**: `{v}`")
        lines.append("")

    # ── Executive summary (LLM) ───────────────────────────────────────────────
    if llm_summary:
        lines.append("## Executive Summary")
        lines.append("")
        lines.append(llm_summary.strip())
        lines.append("")

    # ── Statistics ────────────────────────────────────────────────────────────
    total_duplicated = sum(len(g.members) for g in groups)
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Duplicate groups found**: {len(groups)}")
    lines.append(f"- **Dashboards involved**: {total_duplicated}")
    if groups:
        lines.append(f"- **Highest confidence**: {groups[0].confidence:.0%}")
    lines.append("")

    if not groups:
        lines.append(
            "_No duplicate dashboards detected above the confidence threshold. "
            "Try lowering `--min-confidence` or adding `--with-lineage` / `--with-sql` "
            "for richer signals._"
        )
        return "\n".join(lines)

    # ── Per-group details ─────────────────────────────────────────────────────
    lines.append("## Duplicate Groups")
    lines.append("")

    for idx, group in enumerate(groups, 1):
        reason_str = ", ".join(
            _REASON_LABELS.get(r, r.value) for r in group.reasons
        ) or "unknown"
        lines.append(
            f"### Group {idx} — confidence {group.confidence:.0%} "
            f"({reason_str})"
        )
        lines.append("")

        # Member table
        lines.append("| Dashboard | Platform | Owner | Charts | Datasets |")
        lines.append("|---|---|---|---|---|")
        for m in group.members:
            owner = (
                m.owners[0] if m.owners
                else (m.owner_teams[0] if m.owner_teams else "—")
            )
            lines.append(
                f"| {m.display_title} "
                f"| {m.platform or '—'} "
                f"| {owner} "
                f"| {len(m.chart_urns)} "
                f"| {len(m.dataset_urns)} |"
            )
        lines.append("")

        # Score breakdown
        if group.score_breakdown:
            lines.append("**Signal breakdown:**")
            lines.append("")
            for signal, contribution in sorted(
                group.score_breakdown.items(), key=lambda x: x[1], reverse=True
            ):
                if contribution > 0:
                    lines.append(f"- `{signal}`: {contribution:.2%}")
            lines.append("")

        # Recommendation
        if group.recommendation:
            lines.append(f"**Recommendation:** {group.recommendation}")
            lines.append("")

        # LLM consolidation note
        if group.consolidation_note:
            lines.append(f"> **Consolidation note:** {group.consolidation_note}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── LLM prompt builders ───────────────────────────────────────────────────────

_CONSOLIDATION_PROMPT_TEMPLATE = """\
You are a senior data platform architect specialising in data mesh governance.

The following groups of dashboards have been automatically detected as likely \
duplicates based on structural signals (shared charts, datasets, glossary terms, \
name similarity).

For each group, write one concise sentence (max 25 words) explaining WHY these \
dashboards are duplicates and what the consolidation impact would be.

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "group_id": "<group_id>",
    "consolidation_note": "<your sentence>"
  }},
  ...
]

GROUPS:
{groups_json}
"""


def build_consolidation_prompt(groups: list[DuplicateGroup]) -> str:
    """Build the LLM prompt for one-sentence consolidation notes per group."""
    import json

    groups_data = []
    for g in groups:
        groups_data.append({
            "group_id": g.group_id,
            "dashboards": [
                {
                    "title": m.display_title,
                    "platform": m.platform,
                    "chart_count": len(m.chart_urns),
                    "dataset_count": len(m.dataset_urns),
                    "owners": m.owners[:3],
                    "owner_teams": m.owner_teams[:3],
                    "glossary_terms": m.glossary_term_urns[:5],
                    "description": m.description[:120] if m.description else "",
                }
                for m in g.members
            ],
            "reasons": [r.value for r in g.reasons],
            "confidence": g.confidence,
        })
    return _CONSOLIDATION_PROMPT_TEMPLATE.format(
        groups_json=json.dumps(groups_data, indent=2)
    )


def build_summary_prompt(groups: list[DuplicateGroup]) -> str:
    """Build the LLM prompt for an executive summary paragraph."""
    total = sum(len(g.members) for g in groups)
    top_titles = [
        " / ".join(m.display_title for m in g.members[:2])
        for g in groups[:5]
    ]
    return (
        f"There are {len(groups)} groups of duplicate dashboards involving "
        f"{total} dashboards total. "
        f"Top examples: {'; '.join(top_titles)}. "
        "Write a 2–3 sentence executive summary describing the scope of the "
        "duplication problem and the recommended consolidation action. "
        "Be direct and reference actual dashboard names."
    )


# ── Audit report ──────────────────────────────────────────────────────────────

def format_audit_report(result: AuditResult, generated_date: str | None = None) -> str:
    """Render an ``AuditResult`` as the structured Markdown report.

    Follows exactly the format specified in the duplicate-dashboard audit prompt:
    summary table → overlap clusters → immediate deprecation list →
    untitled dashboards → personal/playground dashboards.
    """
    date_str = generated_date or datetime.now().strftime("%Y-%m-%d")
    s = result.summary
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        "# Superset Dashboard Duplicate & Overlap Report",
        f"**Generated:** {date_str}",
        f"**Total dashboards audited:** {s.total}",
        "",
        "---",
        "",
    ]

    # ── Summary table ─────────────────────────────────────────────────────────
    lines += [
        "## Summary",
        "| Category | Count |",
        "|----------|-------|",
        f"| Confirmed exact name duplicates | {s.exact_duplicates} |",
        f"| Deprecated / legacy series | {s.deprecated_legacy} |",
        f"| Unsupported / abandoned series | {s.unsupported} |",
        f"| Explicit copy tokens | {s.copy_tokens} |",
        f"| Old / test / dev version tokens | {s.old_test_dev} |",
        f"| WIP / draft / staging | {s.wip_draft} |",
        f"| Untitled dashboards | {s.untitled} |",
        f"| Personal / playground | {s.personal} |",
        f"| **Total immediate deprecation candidates** | **{s.total_deprecation_candidates}** |",
        "",
        "---",
        "",
    ]

    # ── Overlap clusters (2+ members only) ────────────────────────────────────
    multi_clusters = [c for c in result.clusters if len(c.members) >= 2]

    lines += ["## Overlap Clusters", ""]
    if not multi_clusters:
        lines += [
            "_No clusters with 2 or more dashboards found above the similarity threshold._",
            "",
        ]
    else:
        for idx, cluster in enumerate(multi_clusters, 1):
            topic_title = cluster.topic.title() if cluster.topic else "Unknown Topic"
            lines += [
                f"### Cluster {idx} — {topic_title}",
                "",
                "| Superset ID | Dashboard Name | Classification | Action |",
                "|-------------|----------------|----------------|--------|",
            ]
            for m in sorted(cluster.members, key=lambda r: r.superset_id or 0):
                sid   = str(m.superset_id) if m.superset_id is not None else "—"
                name  = m.name.replace("|", "\\|")
                cls   = m.classification.value
                act   = m.action.value
                lines.append(f"| {sid} | {name} | {cls} | {act} |")
            lines += ["", "---", ""]

    # ── Immediate deprecation list ────────────────────────────────────────────
    deprecate_records = [
        r for r in result.all_records
        if r.action == DashboardAction.DEPRECATE
        and r.classification != DashboardClassification.UNTITLED
        and r.classification != DashboardClassification.PERSONAL
    ]

    lines += ["## Immediate Deprecation List", ""]
    if not deprecate_records:
        lines += ["_No immediate deprecation candidates identified._", ""]
    else:
        lines += [
            "| Superset ID | Name | Reason |",
            "|-------------|------|--------|",
        ]
        for r in sorted(deprecate_records, key=lambda r: r.superset_id or 0):
            sid    = str(r.superset_id) if r.superset_id is not None else "—"
            name   = r.name.replace("|", "\\|")
            reason = _deprecation_reason(r)
            lines.append(f"| {sid} | {name} | {reason} |")
        lines += [""]

    lines += ["---", ""]

    # ── Untitled dashboards ────────────────────────────────────────────────────
    untitled = [
        r for r in result.all_records
        if r.classification == DashboardClassification.UNTITLED
    ]
    lines += ["## Untitled Dashboards (Bulk Cleanup)", ""]
    if not untitled:
        lines += ["_None found._", ""]
    else:
        lines += [
            "| Superset ID | URN |",
            "|-------------|-----|",
        ]
        for r in sorted(untitled, key=lambda r: r.superset_id or 0):
            sid = str(r.superset_id) if r.superset_id is not None else "—"
            lines.append(f"| {sid} | {r.urn} |")
        lines += [""]

    lines += ["---", ""]

    # ── Personal / playground dashboards ─────────────────────────────────────
    personal = [
        r for r in result.all_records
        if r.classification == DashboardClassification.PERSONAL
    ]
    lines += ["## Personal / Playground Dashboards (Review Required)", ""]
    if not personal:
        lines += ["_None found._", ""]
    else:
        lines += [
            "| Superset ID | Name | Action |",
            "|-------------|------|--------|",
        ]
        for r in sorted(personal, key=lambda r: r.superset_id or 0):
            sid  = str(r.superset_id) if r.superset_id is not None else "—"
            name = r.name.replace("|", "\\|")
            lines.append(f"| {sid} | {name} | {r.action.value} |")

    return "\n".join(lines) + "\n"


def _deprecation_reason(r) -> str:
    """Build a concise reason string for the deprecation list."""
    DC = DashboardClassification
    cls = r.classification
    rep = f" — active replacement exists (ID {r.replacement_id})" if r.replacement_id else ""

    if cls == DC.EXACT_DUPLICATE:
        return f"Exact name duplicate{rep}"
    if cls == DC.LEGACY:
        tokens = ", ".join(r.status_tokens)
        return f"Legacy/deprecated token ({tokens}){rep}"
    if cls == DC.UNSUPPORTED_OWNERSHIP:
        return f"Unsupported ownership prefix [{r.team_prefix}]"
    if cls == DC.PERSONAL_COPY:
        return f"Explicit copy token{rep}"
    if cls == DC.SUPERSEDED_VERSION:
        return f"Superseded version token ({', '.join(r.status_tokens)}){rep}"
    if cls == DC.WIP_DRAFT:
        return f"WIP/draft token ({', '.join(r.status_tokens)})"
    if cls == DC.BROKEN_BACKUP:
        return f"Broken/backup token ({', '.join(r.status_tokens)})"
    return cls.value
