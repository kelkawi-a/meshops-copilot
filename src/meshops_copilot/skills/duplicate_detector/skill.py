"""DuplicateDetectorSkill — find redundant dashboards and metrics via DataHub MCP.

Workflow
--------
1. Open a DataHub MCP session.
2. Search for dashboards (filtered by platform / domain).
3. Collect signals per dashboard in a single batched entity fetch; optionally
   enrich with upstream lineage (``with_lineage=True``) or Superset SQL
   fingerprints (``with_sql=True``).
4. Run pairwise detectors: name similarity, chart-set Jaccard, dataset-set
   Jaccard, glossary-term Jaccard, optional SQL fingerprint overlap.
5. Score each pair; cluster high-confidence pairs into duplicate groups via
   union-find.
6. Optionally call the LLM for a one-sentence consolidation note per group and
   a 2–3 sentence executive summary.
7. Write a Markdown report and a JSON artefact to the output directory.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.core.config import MeshOpsConfig
from meshops_copilot.core.llm import LLMClient
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.duplicate_detector.collectors import DashboardCollector
from meshops_copilot.skills.duplicate_detector.detectors import (
    cluster_pairs,
    detect_all_pairs,
)
from meshops_copilot.skills.duplicate_detector.markdown import (
    build_consolidation_prompt,
    build_summary_prompt,
    format_audit_report,
    format_deduplication_report,
)
from meshops_copilot.skills.duplicate_detector.models import DuplicateGroup
from meshops_copilot.skills.duplicate_detector.scorer import (
    build_groups,
    score_pairs,
)

console = Console()

_SYSTEM_PROMPT = (
    "You are a senior data platform architect specialising in data mesh governance "
    "and dashboard consolidation. "
    "Identify consolidation opportunities clearly — reference actual dashboard names, "
    "team owners, and shared metric/dataset names from the data provided."
)


class DuplicateDetectorSkill(BaseSkill):
    """Detect duplicate dashboards and redundant metrics via DataHub MCP."""

    name = "duplicate_detector"

    def __init__(
        self,
        cfg: MeshOpsConfig,
        output_dir: str | Path = "./reports",
    ) -> None:
        self._cfg = cfg
        self._output_dir = Path(output_dir)

    def run(
        self,
        platform: str | None = None,
        domain: str | None = None,
        min_confidence: float = 0.4,
        top_n: int = 20,
        max_dashboards: int = 100,
        no_llm: bool = False,
        with_lineage: bool = False,
        with_sql: bool = False,
        **kwargs,
    ) -> SkillResult:
        """Detect duplicate dashboards and redundant metrics.

        Args:
            platform: Restrict to a data platform (e.g. ``"superset"``).
            domain: Restrict to a DataHub domain URN or name.
            min_confidence: Drop duplicate groups below this confidence (0.0–1.0).
            top_n: Cap the report at this many groups.
            max_dashboards: Maximum dashboards to scan per DataHub search.
            no_llm: Skip LLM consolidation notes.
            with_lineage: Fetch upstream dataset URNs per dashboard via lineage
                (adds one MCP call per dashboard).
            with_sql: Fetch Superset SQL fingerprints for chart comparison
                (requires Superset credentials in config).
        """
        from meshops_copilot.connectors.datahub_mcp import DataHubMCPConnector

        console.rule("[bold cyan]Duplicate Dashboard & Metric Detection[/bold cyan]")

        # ── Connect ────────────────────────────────────────────────────────────
        try:
            connector = DataHubMCPConnector(
                gms_url=self._cfg.datahub.gms_url,
                token=self._cfg.datahub.token,
            )
        except Exception as exc:
            return self._failed([f"Could not create DataHub MCP connector: {exc}"])

        # Superset connector (opt-in, graceful skip if unconfigured)
        superset_conn = None
        if with_sql:
            superset_conn = self._try_superset_connector()
            if superset_conn is None:
                console.print(
                    "  [yellow]--with-sql requested but Superset is not configured "
                    "or unreachable — skipping SQL fingerprinting.[/yellow]"
                )

        errors: list[str] = []
        profiles = []

        try:
            with connector:
                console.print(
                    f"  Connected to DataHub MCP at "
                    f"[cyan]{self._cfg.datahub.gms_url}[/cyan]"
                )

                # ── Search dashboards ──────────────────────────────────────────
                filter_desc = " ".join(filter(None, [platform, domain])) or "all"
                console.print(
                    f"  Searching up to {max_dashboards} dashboards "
                    f"(platform/domain: {filter_desc})…"
                )
                dashboards = connector.search_dashboards(
                    platform=platform,
                    domain=domain,
                    count=max_dashboards,
                )
                console.print(
                    f"  Found [green]{len(dashboards)}[/green] dashboards."
                )

                if not dashboards:
                    return self._ok(
                        summary="No dashboards found matching the search criteria.",
                        details={"groups": 0},
                    )

                # ── Collect signals ────────────────────────────────────────────
                urns = [
                    d.get("urn") or d.get("entityUrn", "")
                    for d in dashboards
                    if d.get("urn") or d.get("entityUrn")
                ]
                enrichment_flags = []
                if with_lineage:
                    enrichment_flags.append("lineage")
                if with_sql and superset_conn:
                    enrichment_flags.append("SQL fingerprints")
                enrich_str = (
                    f", {', '.join(enrichment_flags)}" if enrichment_flags else ""
                )
                console.print(
                    f"  Collecting signals for {len(urns)} dashboards "
                    f"(entity metadata{enrich_str})…"
                )
                collector = DashboardCollector(
                    connector=connector,
                    superset_connector=superset_conn,
                    max_workers=8,
                    collect_lineage=with_lineage,
                    collect_sql=with_sql and superset_conn is not None,
                )
                profiles = collector.collect_all(urns)

                # Surface per-dashboard errors
                for p in profiles:
                    for e in p.collection_errors:
                        errors.append(f"{p.urn}: {e}")

        except RuntimeError as exc:
            return self._failed([str(exc)])
        except Exception as exc:
            return self._failed([f"DataHub MCP error: {exc}"])

        # ── Detect + score + cluster ───────────────────────────────────────────
        console.print(
            f"  Running pairwise detection across "
            f"{len(profiles)} profiles…"
        )
        use_sql = with_sql and any(p.sql_fingerprints for p in profiles)
        pairs = detect_all_pairs(profiles)
        pairs = score_pairs(pairs, use_sql=use_sql)

        # Filter pairs by confidence before clustering
        confident_pairs = [p for p in pairs if p.confidence >= min_confidence]
        all_urns = [p.urn for p in profiles]
        clusters = cluster_pairs(confident_pairs, all_urns)

        profiles_by_urn = {p.urn: p for p in profiles}
        pairs_by_key = {
            (min(p.urn_a, p.urn_b), max(p.urn_a, p.urn_b)): p
            for p in pairs
        }
        groups = build_groups(clusters, profiles_by_urn, pairs_by_key, use_sql=use_sql)
        groups = groups[:top_n]

        console.print(
            f"  Found [green]{len(groups)}[/green] duplicate group(s) "
            f"above {min_confidence:.0%} confidence."
        )

        if not groups and profiles:
            max_conf = max((p.confidence for p in pairs), default=0.0)
            if max_conf < min_confidence:
                console.print(
                    f"  [yellow]No groups above {min_confidence:.0%} threshold. "
                    f"Highest pair confidence seen: {max_conf:.0%}. "
                    f"Re-run with --min-confidence 0 to see all pairs, "
                    f"or add --with-lineage / --with-sql for richer signals.[/yellow]"
                )

        # ── LLM consolidation notes + summary ─────────────────────────────────
        llm_summary = ""
        if not no_llm and groups:
            if self._cfg.llm.provider != "none" and self._cfg.llm.api_key:
                console.print(
                    f"  Calling {self._cfg.llm.provider} ({self._cfg.llm.model}) "
                    f"for consolidation notes…"
                )
                llm = LLMClient(self._cfg.llm)
                notes = self._generate_notes(llm, groups)
                for g in groups:
                    g.consolidation_note = notes.get(g.group_id, "")
                llm_summary = llm.complete(
                    prompt=build_summary_prompt(groups),
                    system=_SYSTEM_PROMPT,
                ) or ""
                console.print("  [green]LLM analysis complete.[/green]")
            elif self._cfg.llm.provider != "none":
                console.print(
                    "  [dim]No LLM API key — skipping consolidation notes. "
                    "Set OPENAI_API_KEY or OPENROUTER_API_KEY to enable.[/dim]"
                )

        # ── Write report ───────────────────────────────────────────────────────
        self._output_dir.mkdir(parents=True, exist_ok=True)
        query_params = {
            "platform": platform,
            "domain": domain,
            "min_confidence": f"{min_confidence:.0%}",
            "with_lineage": with_lineage,
            "with_sql": with_sql,
        }
        md = format_deduplication_report(groups, query_params, llm_summary)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        md_path = self._output_dir / f"duplicate_dashboards_{ts}.md"
        md_path.write_text(md)
        latest = self._output_dir / "duplicate_dashboards.md"
        latest.write_text(md)
        console.print(f"  Report  → [green]{md_path}[/green]")
        console.print(f"  Latest  → [green]{latest}[/green]")

        json_path = self._output_dir / f"duplicate_dashboards_{ts}.json"
        json_path.write_text(json.dumps(self._to_json(groups), indent=2))

        self._print_table(groups[:10])

        summary = (
            f"Detected {len(groups)} duplicate group(s) "
            f"across {len(profiles)} dashboards. "
            f"Report: {md_path}"
        )
        if errors:
            return self._degraded(
                summary=summary,
                errors=errors,
                details={"groups": len(groups), "output": str(md_path)},
            )
        return self._ok(
            summary=summary,
            details={"groups": len(groups), "output": str(md_path)},
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def audit(
        self,
        platform: str | None = "superset",
        domain: str | None = None,
        max_dashboards: int = 5000,
        output_path: str = "./superset_duplicate_dashboard_report.md",
        **kwargs,
    ) -> SkillResult:
        """Name-based dashboard audit: strip tokens, cluster by topic, classify.

        Fetches every dashboard from DataHub (paginating automatically), then
        applies deterministic naming rules to detect duplicates, legacy series,
        WIP copies, untitled placeholders, and personal playgrounds.

        No entity-detail or lineage calls are made — only DataHub search
        results (name + URN) are required, so this runs fast even against
        large catalogues.

        Args:
            platform: Platform filter for DataHub search (default ``"superset"``).
                      Pass ``None`` to audit all platforms.
            domain:   Optional DataHub domain URN or name filter.
            max_dashboards: Upper bound on dashboards to fetch (default 5 000).
            output_path: Where to write the Markdown report.
        """
        from meshops_copilot.connectors.datahub_mcp import DataHubMCPConnector
        from meshops_copilot.skills.duplicate_detector.auditor import build_audit

        console.rule("[bold cyan]Dashboard Duplicate Audit[/bold cyan]")

        try:
            connector = DataHubMCPConnector(
                gms_url=self._cfg.datahub.gms_url,
                token=self._cfg.datahub.token,
            )
        except Exception as exc:
            return self._failed([f"Could not create DataHub MCP connector: {exc}"])

        entities: list[dict] = []
        try:
            with connector:
                console.print(
                    f"  Connected to DataHub MCP at "
                    f"[cyan]{self._cfg.datahub.gms_url}[/cyan]"
                )
                filter_desc = " ".join(filter(None, [platform, domain])) or "all"
                console.print(
                    f"  Fetching all dashboards "
                    f"(platform/domain: {filter_desc}, max: {max_dashboards})…"
                )
                entities = connector.search_dashboards(
                    platform=platform,
                    domain=domain,
                    count=max_dashboards,
                )
                console.print(
                    f"  Fetched [green]{len(entities)}[/green] dashboards."
                )
        except RuntimeError as exc:
            return self._failed([str(exc)])
        except Exception as exc:
            return self._failed([f"DataHub MCP error: {exc}"])

        if not entities:
            return self._ok(
                summary="No dashboards found matching the search criteria.",
                details={"total": 0},
            )

        # ── Analyse ───────────────────────────────────────────────────────────
        console.print("  Analysing names — extracting tokens and clustering…")
        result = build_audit(entities)
        s = result.summary

        console.print(
            f"  [green]{s.total_deprecation_candidates}[/green] deprecation "
            f"candidates out of {s.total} dashboards."
        )

        # ── Write report ──────────────────────────────────────────────────────
        md = format_audit_report(result)
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md)
        console.print(f"  Report → [green]{out_path}[/green]")

        # ── Console summary table ─────────────────────────────────────────────
        from rich.table import Table as RichTable
        t = RichTable(title="Audit Summary", show_lines=False)
        t.add_column("Category", style="cyan")
        t.add_column("Count", justify="right")
        rows = [
            ("Exact name duplicates",        s.exact_duplicates),
            ("Deprecated / legacy",          s.deprecated_legacy),
            ("Unsupported / abandoned",      s.unsupported),
            ("Explicit copy tokens",         s.copy_tokens),
            ("Old / test / dev tokens",      s.old_test_dev),
            ("WIP / draft",                  s.wip_draft),
            ("Untitled",                     s.untitled),
            ("Personal / playground",        s.personal),
            ("Total candidates",             s.total_deprecation_candidates),
        ]
        for label, count in rows:
            style = "bold red" if label == "Total candidates" else ""
            t.add_row(label, str(count), style=style)
        console.print(t)

        return self._ok(
            summary=(
                f"Audited {s.total} dashboards; "
                f"{s.total_deprecation_candidates} deprecation candidates. "
                f"Report: {out_path}"
            ),
            details={
                "total": s.total,
                "deprecation_candidates": s.total_deprecation_candidates,
                "output": str(out_path),
            },
        )

    def _try_superset_connector(self):
        """Attempt to create and log into the Superset connector; return None on failure."""
        from meshops_copilot.connectors.superset import SupersetConnector
        from meshops_copilot.core.errors import ConnectorError

        cfg = self._cfg.superset
        if not cfg.url or cfg.url == "http://localhost:8088":
            # Default URL — treat as not configured unless user set it explicitly
            import os
            if not os.environ.get("SUPERSET_URL"):
                return None
        try:
            conn = SupersetConnector(
                url=cfg.url,
                user=cfg.user,
                password=cfg.password,
            )
            conn.login()
            return conn
        except (ConnectorError, Exception) as exc:
            console.print(
                f"  [yellow]Superset unavailable ({exc}) — "
                f"skipping SQL fingerprinting.[/yellow]"
            )
            return None

    def _generate_notes(
        self,
        llm: LLMClient,
        groups: list[DuplicateGroup],
    ) -> dict[str, str]:
        """Generate one consolidation note per group in a single LLM call."""
        raw = llm.complete(
            prompt=build_consolidation_prompt(groups),
            system=_SYSTEM_PROMPT,
        )
        if not raw:
            return {}
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = "\n".join(text.split("\n")[1:]).rstrip("`").strip()
            items = json.loads(text)
            return {
                item["group_id"]: item["consolidation_note"]
                for item in items
                if "group_id" in item and "consolidation_note" in item
            }
        except Exception as exc:
            console.print(
                f"  [yellow]Could not parse consolidation notes JSON: {exc}[/yellow]"
            )
            return {}

    def _print_table(self, groups: list[DuplicateGroup]) -> None:
        t = Table(title="Top Duplicate Groups", show_lines=False)
        t.add_column("#", style="dim", width=3)
        t.add_column("Dashboards", style="cyan")
        t.add_column("Confidence", justify="right")
        t.add_column("Reasons")
        t.add_column("Recommendation")
        for i, g in enumerate(groups, 1):
            titles = " / ".join(m.display_title for m in g.members[:3])
            if len(g.members) > 3:
                titles += f" (+{len(g.members) - 3} more)"
            reasons = ", ".join(r.value for r in g.reasons)
            rec = g.recommendation[:60] + "…" if len(g.recommendation) > 60 else g.recommendation
            t.add_row(str(i), titles, f"{g.confidence:.0%}", reasons, rec)
        console.print(t)

    @staticmethod
    def _to_json(groups: list[DuplicateGroup]) -> list[dict]:
        out = []
        for g in groups:
            out.append({
                "group_id": g.group_id,
                "confidence": g.confidence,
                "reasons": [r.value for r in g.reasons],
                "score_breakdown": g.score_breakdown,
                "recommendation": g.recommendation,
                "consolidation_note": g.consolidation_note,
                "members": [
                    {
                        "urn": m.urn,
                        "title": m.title,
                        "platform": m.platform,
                        "owners": m.owners,
                        "owner_teams": m.owner_teams,
                        "chart_count": len(m.chart_urns),
                        "dataset_count": len(m.dataset_urns),
                        "glossary_terms": m.glossary_term_urns,
                        "has_description": bool(m.description),
                    }
                    for m in g.members
                ],
            })
        return out
