"""Golden Report Candidate skill — orchestration and output.

Collects signals from Superset, scores every dashboard, detects
duplicates, and categorises results into four buckets:

1. **Golden candidates** — high-quality, well-used, ready for certification
2. **Needs work** — medium score with fixable gaps
3. **Duplicates to merge** — dashboards with high chart overlap
4. **Anti-golden** — stale, expensive, or error-prone reports
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.connectors.superset import SupersetConnector
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.golden_report.collectors import (
    GoldenReportCollector,
)
from meshops_copilot.skills.golden_report.duplicates import find_duplicates
from meshops_copilot.skills.golden_report.models import (
    Category,
    DashboardSignals,
    GoldenCandidate,
)
from meshops_copilot.skills.golden_report.scorer import to_candidate

console = Console()


class GoldenReportSkill(BaseSkill):
    """Assess Superset dashboards for golden report candidacy."""

    name = "golden_report"

    def __init__(
        self,
        cfg,                           # SupersetConfig
        output_file: str | None = None,
        lookback_days: int = 30,
        max_log_records: int = 50_000,
        duplicate_threshold: float = 0.50,
    ) -> None:
        self._cfg = cfg
        self._output_file = output_file
        self._lookback_days = lookback_days
        self._max_log_records = max_log_records
        self._dup_threshold = duplicate_threshold

    # ── BaseSkill interface ────────────────────────────────────────────────

    def run(self, **kwargs) -> "SkillResult":  # noqa: F821
        connector = SupersetConnector(
            url=self._cfg.url,
            user=self._cfg.user,
            password=self._cfg.password,
            verify_ssl=False,
        )
        connector.login()

        collector = GoldenReportCollector(
            connector, lookback_days=self._lookback_days,
        )

        # ── Phase 1: collect raw data ──────────────────────────────────
        console.print("\n[bold]Phase 1:[/bold] Collecting dashboard metadata …")
        dashboards = collector.collect_dashboards()
        console.print(f"  Found {len(dashboards)} dashboards")

        console.print("[bold]Phase 2:[/bold] Collecting activity logs …")
        views_by_dash = collector.collect_dashboard_views(
            max_records=self._max_log_records,
        )
        if collector.warnings:
            for w in collector.warnings:
                console.print(f"  [yellow]Warning:[/yellow] {w}")
        total_views = sum(len(v) for v in views_by_dash.values())
        console.print(
            f"  {total_views} view events across "
            f"{len(views_by_dash)} dashboards"
        )

        console.print("[bold]Phase 3:[/bold] Mapping charts → dashboards …")
        chart_mapping, datasets_by_chart = collector.collect_chart_mapping()
        total_charts = sum(len(c) for c in chart_mapping.values())
        console.print(f"  {total_charts} chart assignments")

        # Gather unique dataset ids across all dashboards
        all_dataset_ids: list[int] = []
        for charts in chart_mapping.values():
            for cid in charts:
                all_dataset_ids.extend(datasets_by_chart.get(cid, []))

        console.print("[bold]Phase 4:[/bold] Checking dataset certification …")
        cert_map = collector.collect_dataset_certification(
            list(set(all_dataset_ids)),
        )
        console.print(
            f"  {sum(cert_map.values())}/{len(cert_map)} datasets certified"
        )

        # ── Assemble signals & score ───────────────────────────────────
        console.print("\n[bold]Scoring dashboards …[/bold]")
        candidates: list[GoldenCandidate] = []

        for dash in dashboards:
            did = dash.get("id")
            if did is None:
                continue

            signals = self._build_signals(
                dash, did, views_by_dash, chart_mapping,
                datasets_by_chart, cert_map,
                collector,
            )
            candidates.append(to_candidate(signals))

        # Sort by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)

        # ── Phase 3: duplicate detection ───────────────────────────────
        console.print("[bold]Detecting duplicates …[/bold]")
        dash_titles = {c.dashboard_id: c.title for c in candidates}
        dash_views = {
            c.dashboard_id: c.signals.view_count_30d for c in candidates
        }
        duplicates = find_duplicates(
            chart_mapping, dash_titles, dash_views,
            min_jaccard=self._dup_threshold,
        )

        # ── Phase 4: output ────────────────────────────────────────────
        golden = [c for c in candidates if c.category == Category.GOLDEN]
        needs_work = [c for c in candidates if c.category == Category.NEEDS_WORK]
        anti = [c for c in candidates if c.category == Category.ANTI_GOLDEN]

        self._print_results(golden, needs_work, anti, duplicates)

        report = self._build_report(golden, needs_work, anti, duplicates)
        if self._output_file:
            Path(self._output_file).write_text(
                json.dumps(report, indent=2, default=str),
            )
            console.print(f"\n[dim]Report saved to {self._output_file}[/dim]")

        summary = (
            f"{len(golden)} golden candidates, "
            f"{len(needs_work)} need work, "
            f"{len(anti)} anti-golden, "
            f"{len(duplicates)} duplicate pairs"
        )
        if collector.warnings:
            return self._degraded(
                summary=summary,
                errors=collector.warnings,
                details=report,
            )
        return self._ok(summary=summary, details=report)

    # ── Signal assembly ────────────────────────────────────────────────────

    def _build_signals(
        self,
        dash: dict,
        did: int,
        views_by_dash: dict,
        chart_mapping: dict,
        datasets_by_chart: dict,
        cert_map: dict,
        collector: GoldenReportCollector,
    ) -> DashboardSignals:
        """Assemble a :class:`DashboardSignals` for one dashboard."""
        # Usage
        view_records = views_by_dash.get(did, [])
        view_count, unique_viewers, active_weeks = (
            collector.compute_usage_signals(view_records)
        )

        # Ownership
        owners_raw = dash.get("owners", [])
        owners = [
            o.get("username", o.get("first_name", str(o.get("id", ""))))
            for o in owners_raw
            if isinstance(o, dict)
        ] if isinstance(owners_raw, list) else []

        # Stability
        changed_on = dash.get("changed_on", dash.get("changed_on_delta_humanized", ""))
        days_since = 0
        if changed_on:
            try:
                dt = datetime.fromisoformat(
                    str(changed_on).replace("Z", "+00:00")
                )
                days_since = (datetime.now(tz=timezone.utc) - dt).days
            except (ValueError, TypeError):
                pass

        # Charts & datasets for this dashboard
        chart_ids = chart_mapping.get(did, [])
        ds_ids: list[int] = []
        for cid in chart_ids:
            ds_ids.extend(datasets_by_chart.get(cid, []))
        unique_ds = list(set(ds_ids))

        # Dataset certification fraction
        if unique_ds:
            certified_count = sum(1 for d in unique_ds if cert_map.get(d, False))
            cert_frac = certified_count / len(unique_ds)
        else:
            cert_frac = 0.0

        return DashboardSignals(
            dashboard_id=did,
            title=dash.get("dashboard_title", f"Dashboard {did}"),
            url=f"{self._cfg.url}/superset/dashboard/{did}/",
            view_count_30d=view_count,
            unique_viewers_30d=unique_viewers,
            active_weeks_30d=active_weeks,
            owners=owners,
            has_description=bool(dash.get("description")),
            tags=[
                t.get("name", str(t)) for t in dash.get("tags", [])
                if isinstance(t, dict)
            ],
            published=bool(dash.get("published", False)),
            certified=bool(dash.get("certified_by")),
            certified_by=dash.get("certified_by", ""),
            changed_on=str(changed_on),
            days_since_change=days_since,
            chart_count=len(chart_ids),
            chart_ids=chart_ids,
            median_query_duration_ms=0.0,
            p95_query_duration_ms=0.0,
            error_rate=0.0,
            dataset_ids=unique_ds,
            certified_dataset_fraction=round(cert_frac, 4),
        )

    # ── Output formatting ──────────────────────────────────────────────────

    @staticmethod
    def _print_results(
        golden: list[GoldenCandidate],
        needs_work: list[GoldenCandidate],
        anti: list[GoldenCandidate],
        duplicates: list,
    ) -> None:
        console.print()

        # ── Golden candidates ──────────────────────────────────────────
        if golden:
            t = Table(
                title="Golden Report Candidates",
                title_style="bold green",
                show_lines=True,
            )
            t.add_column("Dashboard", style="green")
            t.add_column("Score", justify="right")
            t.add_column("Views (30d)", justify="right")
            t.add_column("Viewers", justify="right")
            t.add_column("Recurring", justify="center")
            t.add_column("Owner", style="dim")
            t.add_column("Certified DS%", justify="right")

            for c in golden:
                s = c.signals
                t.add_row(
                    s.display_name,
                    f"{c.score:.2f}",
                    str(s.view_count_30d),
                    str(s.unique_viewers_30d),
                    f"{s.active_weeks_30d}/4",
                    ", ".join(s.owners[:2]) or "\u2014",
                    f"{s.certified_dataset_fraction * 100:.0f}%",
                )
            console.print(t)
        else:
            console.print("[yellow]No golden report candidates found.[/yellow]")

        # ── Needs work ─────────────────────────────────────────────────
        if needs_work:
            t = Table(
                title="Dashboards Needing Certification Work",
                title_style="bold yellow",
                show_lines=True,
            )
            t.add_column("Dashboard", style="yellow")
            t.add_column("Score", justify="right")
            t.add_column("Gaps")

            for c in needs_work:
                t.add_row(
                    c.signals.display_name,
                    f"{c.score:.2f}",
                    "; ".join(c.gaps) or "—",
                )
            console.print(t)

        # ── Anti-golden ────────────────────────────────────────────────
        if anti:
            t = Table(
                title="Anti-Golden (Stale / Low Score)",
                title_style="bold red",
                show_lines=True,
            )
            t.add_column("Dashboard", style="red")
            t.add_column("Score", justify="right")
            t.add_column("Views (30d)", justify="right")
            t.add_column("Reason")

            for c in anti[:20]:  # cap display at 20
                s = c.signals
                reasons = []
                if s.view_count_30d == 0:
                    reasons.append("stale")
                if not reasons:
                    reasons.append("low score")
                t.add_row(
                    s.display_name,
                    f"{c.score:.2f}",
                    str(s.view_count_30d),
                    ", ".join(reasons),
                )
            t.add_column("Dashboard", style="red")
            t.add_column("Score", justify="right")
            t.add_column("Views (30d)", justify="right")
            t.add_column("Error%", justify="right")
            t.add_column("p95 (ms)", justify="right")
            t.add_column("Reason")

            for c in anti[:20]:  # cap display at 20
                s = c.signals
                reasons = []
                if s.view_count_30d == 0:
                    reasons.append("stale")
                if s.error_rate > 0.30:
                    reasons.append("unreliable")
                if s.p95_query_duration_ms > 60_000:
                    reasons.append("expensive")
                if not reasons:
                    reasons.append("low score")
                t.add_row(
                    s.display_name,
                    f"{c.score:.2f}",
                    str(s.view_count_30d),
                    f"{s.error_rate * 100:.0f}%",
                    f"{s.p95_query_duration_ms:.0f}" if s.p95_query_duration_ms else "—",
                    ", ".join(reasons),
                )
            if len(anti) > 20:
                console.print(
                    f"  [dim]… and {len(anti) - 20} more anti-golden dashboards[/dim]"
                )
            console.print(t)

        # ── Duplicates ─────────────────────────────────────────────────
        if duplicates:
            t = Table(
                title="Duplicate Dashboards to Merge",
                title_style="bold cyan",
                show_lines=True,
            )
            t.add_column("Dashboard A", style="cyan")
            t.add_column("Dashboard B", style="cyan")
            t.add_column("Similarity", justify="right")
            t.add_column("Shared Charts", justify="right")
            t.add_column("Recommendation")

            for d in duplicates:
                t.add_row(
                    d.dashboard_a_title,
                    d.dashboard_b_title,
                    f"{d.jaccard_similarity:.0%}",
                    str(len(d.shared_charts)),
                    d.recommendation,
                )
            console.print(t)

        # ── Summary line ───────────────────────────────────────────────
        console.print(
            f"\n[bold]Summary:[/bold] "
            f"[green]{len(golden)}[/green] golden · "
            f"[yellow]{len(needs_work)}[/yellow] needs work · "
            f"[red]{len(anti)}[/red] anti-golden · "
            f"[cyan]{len(duplicates)}[/cyan] duplicate pairs"
        )

    @staticmethod
    def _build_report(
        golden: list[GoldenCandidate],
        needs_work: list[GoldenCandidate],
        anti: list[GoldenCandidate],
        duplicates: list,
    ) -> dict:
        def _candidate_dict(c: GoldenCandidate) -> dict:
            return {
                "dashboard_id": c.dashboard_id,
                "title": c.title,
                "score": c.score,
                "category": c.category.value,
                "gaps": c.gaps,
                "score_breakdown": c.score_breakdown,
                "signals": {
                    "view_count_30d": c.signals.view_count_30d,
                    "unique_viewers_30d": c.signals.unique_viewers_30d,
                    "active_weeks_30d": c.signals.active_weeks_30d,
                    "owners": c.signals.owners,
                    "has_description": c.signals.has_description,
                    "published": c.signals.published,
                    "certified": c.signals.certified,
                    "days_since_change": c.signals.days_since_change,
                    "chart_count": c.signals.chart_count,
                    "certified_dataset_fraction": c.signals.certified_dataset_fraction,
                },
            }

        def _dup_dict(d) -> dict:
            return {
                "dashboard_a": {"id": d.dashboard_a_id, "title": d.dashboard_a_title},
                "dashboard_b": {"id": d.dashboard_b_id, "title": d.dashboard_b_title},
                "jaccard_similarity": d.jaccard_similarity,
                "shared_chart_count": len(d.shared_charts),
                "recommendation": d.recommendation,
            }

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "golden_candidates": [_candidate_dict(c) for c in golden],
            "needs_work": [_candidate_dict(c) for c in needs_work],
            "anti_golden": [_candidate_dict(c) for c in anti],
            "duplicates": [_dup_dict(d) for d in duplicates],
        }
