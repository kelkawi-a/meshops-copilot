"""Markdown report and LLM prompt builders for data_product_discovery."""

from __future__ import annotations

import json
from datetime import datetime

from meshops_copilot.skills.data_product_discovery.models import DataProductCandidate
from meshops_copilot.skills.data_product_discovery.scorer import WEIGHTS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(score: float, width: int = 10) -> str:
    """Render a Unicode block progress bar."""
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def _pct(v: float) -> str:
    return f"{v:.0%}"


# ── Public API ────────────────────────────────────────────────────────────────

def format_discovery_report(
    candidates: list[DataProductCandidate],
    query_params: dict | None = None,
    llm_summary: str = "",
) -> str:
    """Return a complete Markdown report string for the discovery results."""
    params = query_params or {}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections: list[str] = []

    # ── Header ─────────────────────────────────────────────────────────────────
    header = [
        "# Data Product Candidate Discovery Report",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Generated | {ts} |",
        f"| Candidates found | {len(candidates)} |",
    ]
    if params.get("domain"):
        header.append(f"| Domain filter | `{params['domain']}` |")
    if params.get("platform"):
        header.append(f"| Platform filter | `{params['platform']}` |")
    if params.get("min_score") is not None:
        header.append(f"| Min score threshold | {_pct(params['min_score'])} |")
    sections.append("\n".join(header))

    # ── Executive summary (LLM) ────────────────────────────────────────────────
    if llm_summary:
        sections.append(f"## Executive Summary\n\n{llm_summary.strip()}")

    # ── Top candidates table ───────────────────────────────────────────────────
    if candidates:
        rows = [
            "| # | Dataset | Platform | Score | Queries/30d"
            " | Users/30d | Dashboards | Owner |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for i, c in enumerate(candidates, 1):
            s = c.signals
            owner = (
                s.owners[0]
                if s.owners
                else (s.owner_teams[0] if s.owner_teams else "—")
            )
            rows.append(
                f"| {i} | `{c.display_name}` | {c.platform or '—'} "
                f"| {_pct(c.score)} "
                f"| {s.query_count_30d} | {s.unique_users_30d} "
                f"| {s.downstream_dashboard_count} | {owner} |"
            )
        sections.append("## Top Candidates\n\n" + "\n".join(rows))

    # ── Per-candidate detail cards ─────────────────────────────────────────────
    cards: list[str] = []
    for i, c in enumerate(candidates, 1):
        s = c.signals
        lines: list[str] = [f"### {i}. `{c.display_name}`", ""]
        if c.justification:
            lines += [f"> {c.justification}", ""]
        lines += [
            f"**Score:** {_pct(c.score)}  {_bar(c.score)}",
            "",
            "| Signal | Value | Contribution |",
            "| --- | --- | --- |",
            f"| Queries (30 d) | {s.query_count_30d:,} | {_pct(c.score_breakdown.get('query_count_30d', 0))} |",
            f"| Unique users (30 d) | {s.unique_users_30d:,} | {_pct(c.score_breakdown.get('unique_users_30d', 0))} |",
            f"| Downstream dashboards | {s.downstream_dashboard_count} | {_pct(c.score_breakdown.get('downstream_dashboard_count', 0))} |",
            f"| Downstream datasets | {s.downstream_dataset_count} | {_pct(c.score_breakdown.get('downstream_dataset_count', 0))} |",
            f"| Has owner | {'Yes' if s.has_owner else 'No'} | {_pct(c.score_breakdown.get('has_owner', 0))} |",
            f"| Owner teams | {', '.join(s.owner_teams) or '—'} | {_pct(c.score_breakdown.get('owner_teams', 0))} |",
            f"| Has description | {'Yes' if s.has_description else 'No'} | {_pct(c.score_breakdown.get('has_description', 0))} |",
            f"| Schema fields | {s.schema_field_count} | {_pct(c.score_breakdown.get('schema_field_count', 0))} |",
        ]
        if s.domain:
            lines += ["", f"**Domain:** {s.domain}"]
        if s.tags:
            lines.append(f"**Tags:** {', '.join(s.tags)}")
        if s.description:
            excerpt = s.description[:200] + ("…" if len(s.description) > 200 else "")
            lines += ["", f"**Description:** {excerpt}"]
        if s.collection_errors:
            lines += ["", f"⚠ Partial data ({', '.join(s.collection_errors)})"]
        cards.append("\n".join(lines))

    if cards:
        sections.append("## Candidate Details\n\n" + "\n\n---\n\n".join(cards))

    return "\n\n---\n\n".join(sections) + "\n"


def build_llm_prompt(candidates: list[DataProductCandidate]) -> str:
    """Build a single prompt that requests one justification per candidate.

    Asks the LLM to return a JSON array so all justifications are generated
    in one API call rather than one call per candidate.

    Expected LLM response::

        [{"urn": "...", "justification": "...sentence..."}, ...]
    """
    lines = [
        "You are analysing data product candidates sourced from a DataHub metadata catalogue.",
        "For each dataset below, write a single justification sentence (max 35 words) that",
        "explains concisely why it is a strong data product candidate.",
        "Reference the actual numbers. Be specific. Do not be generic.",
        "",
        'Return a JSON array: [{"urn": "...", "justification": "..."}, ...]',
        "",
        "Datasets:",
    ]
    for c in candidates:
        s = c.signals
        lines.append(
            f"- urn={c.urn!r} name={c.display_name!r} platform={c.platform!r}"
            f" queries_30d={s.query_count_30d} unique_users={s.unique_users_30d}"
            f" dashboards={s.downstream_dashboard_count}"
            f" downstream_datasets={s.downstream_dataset_count}"
            f" owners={s.owners} teams={s.owner_teams}"
            f" has_description={s.has_description} domain={s.domain!r}"
        )
    return "\n".join(lines)


def build_summary_prompt(candidates: list[DataProductCandidate]) -> str:
    """Build a prompt for a 2-3 sentence executive summary of the top candidates."""
    lines = [
        "Given the top data product candidates below from a DataHub metadata scan,",
        "write a 2-3 sentence executive summary covering:",
        "  1. Which domains or platforms dominate the top candidates.",
        "  2. Which signal (query frequency, dashboard reuse, or lineage depth) is"
        " the strongest driver.",
        "  3. Which single dataset is the strongest candidate and why.",
        "Be concise and reference actual numbers.",
        "",
    ]
    for c in candidates[:10]:
        s = c.signals
        lines.append(
            f"- {c.display_name} ({c.platform}): score={_pct(c.score)},"
            f" queries={s.query_count_30d}, users={s.unique_users_30d},"
            f" dashboards={s.downstream_dashboard_count},"
            f" domain={s.domain or 'unknown'}"
        )
    return "\n".join(lines)
