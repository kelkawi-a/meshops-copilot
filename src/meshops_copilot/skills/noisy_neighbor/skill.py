"""NoisyNeighborSkill — identifies Superset dashboards/users causing disproportionate load.

Correlates Superset activity (views, chart renders, SQL queries) with Trino
query cost (duration, planning time) to surface entities that consume far more
resources than their share of traffic would suggest.

Example finding:
    "Dashboard 'Sales Pipeline' accounts for 4% of views but 38% of Trino query time."
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.connectors.superset import SupersetConnector
from meshops_copilot.connectors.trino import TrinoConnector
from meshops_copilot.core.config import MeshOpsConfig
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.noisy_neighbor.analyzer import Analyzer
from meshops_copilot.skills.noisy_neighbor.collectors.superset import SupersetCollector
from meshops_copilot.skills.noisy_neighbor.collectors.trino import TrinoCollector
from meshops_copilot.skills.noisy_neighbor.correlator import Correlator
from meshops_copilot.skills.noisy_neighbor.models import (
    DimensionResult,
    NoisyNeighborReport,
    Severity,
)

console = Console()


class NoisyNeighborSkill(BaseSkill):
    """Detect dashboards, charts, and users causing disproportionate Trino load."""

    name = "noisy_neighbor"

    def __init__(
        self,
        cfg: MeshOpsConfig,
        output_file: str | None = None,
        lookback_hours: int = 168,
    ) -> None:
        self._cfg = cfg
        self._output_file = output_file or "noisy_neighbor_results.json"
        self._lookback_hours = lookback_hours

    def run(self, **kwargs) -> SkillResult:
        lookback = kwargs.get("lookback_hours", self._lookback_hours)

        console.rule("[bold cyan]Noisy Neighbor Detector[/bold cyan]")
        console.print(f"  Superset : {self._cfg.superset.url}")
        console.print(f"  Trino    : {self._cfg.trino.url}")
        console.print(f"  Lookback : {lookback}h")
        console.print(f"  Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        report = NoisyNeighborReport(
            target_superset=self._cfg.superset.url,
            target_trino=self._cfg.trino.url,
            lookback_hours=lookback,
        )

        # ── Connect ───────────────────────────────────────────────────────────
        superset_conn = SupersetConnector(
            url=self._cfg.superset.url,
            user=self._cfg.superset.user,
            password=self._cfg.superset.password,
        )
        trino_conn = TrinoConnector(
            url=self._cfg.trino.url,
            user=self._cfg.trino.user,
            password=self._cfg.trino.password,
            verify_ssl=self._cfg.trino.verify_ssl,
        )

        try:
            superset_conn.login()
        except Exception as exc:
            return self._failed([f"Superset login failed: {exc}"])

        # ── Collect data ──────────────────────────────────────────────────────
        console.print("\n[bold]── Collecting data…[/bold]")

        ss_collector = SupersetCollector(superset_conn, lookback_hours=lookback)
        tr_collector = TrinoCollector(trino_conn, lookback_hours=lookback)

        console.print("  Fetching Superset query history…")
        ss_queries = ss_collector.collect_queries()
        report.superset_queries_collected = len(ss_queries)
        console.print(f"    → {len(ss_queries)} queries")

        console.print("  Fetching Trino query history…")
        tr_queries = tr_collector.collect_queries()
        report.trino_queries_collected = len(tr_queries)
        console.print(f"    → {len(tr_queries)} queries")

        if not ss_queries and not tr_queries:
            return self._failed(["No query data collected from either source."])

        # ── Correlate ─────────────────────────────────────────────────────────
        console.print("\n[bold]── Correlating Superset ↔ Trino…[/bold]")
        correlator = Correlator()
        correlation = correlator.correlate(ss_queries, tr_queries)
        report.correlated_queries = len(correlation.correlated)

        console.print(
            f"  Direct matches (tracking_url): {len(correlation.correlated)}"
        )
        console.print(
            f"  Superset-sourced Trino (no Superset record): "
            f"{len(correlation.superset_source_trino)}"
        )
        console.print(
            f"  Unmatched Superset queries: {len(correlation.unmatched_superset)}"
        )

        # ── Resolve names ─────────────────────────────────────────────────────
        chart_names = self._resolve_chart_names(superset_conn)
        dashboard_names = self._resolve_dashboard_names(superset_conn)

        # ── Analyze ───────────────────────────────────────────────────────────
        console.print("\n[bold]── Analyzing dimensions…[/bold]")
        analyzer = Analyzer()
        report.dimensions = analyzer.analyze_all(
            correlation, [], chart_names, dashboard_names
        )

        # ── Generate findings ─────────────────────────────────────────────────
        report.findings = self._generate_findings(report.dimensions)

        # ── Print results ─────────────────────────────────────────────────────
        self._print_findings(report)
        self._print_dimensions(report.dimensions)
        self._save(report)

        if report.findings:
            return self._ok(
                summary=f"Found {len(report.findings)} noisy-neighbor pattern(s).",
                details=self._to_dict(report),
            )
        return self._ok(
            summary="No significant noisy-neighbor patterns detected.",
            details=self._to_dict(report),
        )

    # ── Name resolution ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_chart_names(conn: SupersetConnector) -> dict[int, str]:
        """Fetch chart id → name mapping."""
        try:
            charts = conn.list_charts(max_items=500)
            return {c["id"]: c.get("slice_name", f"chart_{c['id']}") for c in charts}
        except Exception:
            return {}

    @staticmethod
    def _resolve_dashboard_names(conn: SupersetConnector) -> dict[int, str]:
        """Fetch dashboard id → name mapping."""
        try:
            dashboards = conn.list_dashboards()
            return {
                d["id"]: d.get("dashboard_title", f"dashboard_{d['id']}")
                for d in dashboards
            }
        except Exception:
            return {}

    # ── Findings generation ────────────────────────────────────────────────────

    @staticmethod
    def _generate_findings(dimensions: dict[str, DimensionResult]) -> list[str]:
        """Generate human-readable finding sentences from top offenders."""
        findings: list[str] = []
        for dim_name, dim in dimensions.items():
            for entity in dim.top_offenders[:5]:
                if entity.severity == Severity.CRITICAL:
                    findings.append(entity.detail)
                elif entity.severity == Severity.MODERATE and len(findings) < 15:
                    findings.append(entity.detail)
        return findings

    # ── Output ─────────────────────────────────────────────────────────────────

    def _print_findings(self, report: NoisyNeighborReport) -> None:
        console.print()
        console.rule("[bold]Findings[/bold]")
        if not report.findings:
            console.print(
                "  [green]No significant noisy-neighbor patterns detected.[/green]"
            )
            return
        for i, f in enumerate(report.findings, 1):
            severity = "red" if "critical" in f.lower() or "ratio: " in f and self._ratio_from_finding(f) >= 3.0 else "yellow"
            console.print(f"  [{severity}]{i}. {f}[/{severity}]")

    @staticmethod
    def _ratio_from_finding(finding: str) -> float:
        """Extract noise ratio from a finding string."""
        import re
        m = re.search(r"ratio:\s*([\d.]+)x", finding)
        return float(m.group(1)) if m else 0.0

    def _print_dimensions(self, dimensions: dict[str, DimensionResult]) -> None:
        for dim_name, dim in dimensions.items():
            if not dim.entities:
                continue
            console.print()
            t = Table(
                title=f"Dimension: {dim_name}",
                show_header=True,
                header_style="bold cyan",
            )
            t.add_column("Entity", style="cyan")
            t.add_column("Activity", justify="right")
            t.add_column("Activity %", justify="right")
            t.add_column("Cost (s)", justify="right")
            t.add_column("Cost %", justify="right")
            t.add_column("Noise Ratio", justify="right")
            t.add_column("Severity")

            for entity in dim.entities[:15]:
                sev_style = {
                    Severity.CRITICAL: "bold red",
                    Severity.MODERATE: "yellow",
                    Severity.NORMAL: "dim",
                }[entity.severity]
                t.add_row(
                    entity.name[:40],
                    str(entity.activity_count),
                    f"{entity.activity_share * 100:.1f}%",
                    f"{entity.cost_ms / 1000:.1f}",
                    f"{entity.cost_share * 100:.1f}%",
                    f"[{sev_style}]{entity.noise_ratio:.1f}x[/{sev_style}]",
                    f"[{sev_style}]{entity.severity.value}[/{sev_style}]",
                )
            console.print(t)

    def _save(self, report: NoisyNeighborReport) -> None:
        out = Path(self._output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as fh:
            json.dump(self._to_dict(report), fh, indent=2, default=str)
        console.print(f"\n[green]Results saved to {out}[/green]")

    @staticmethod
    def _to_dict(report: NoisyNeighborReport) -> dict:
        return {
            "target_superset": report.target_superset,
            "target_trino": report.target_trino,
            "lookback_hours": report.lookback_hours,
            "superset_queries_collected": report.superset_queries_collected,
            "superset_logs_collected": report.superset_logs_collected,
            "trino_queries_collected": report.trino_queries_collected,
            "correlated_queries": report.correlated_queries,
            "findings": report.findings,
            "dimensions": {
                name: {
                    "dimension": dim.dimension,
                    "total_activity": dim.total_activity,
                    "total_cost_ms": dim.total_cost_ms,
                    "noisy_count": dim.noisy_count,
                    "entities": [asdict(e) for e in dim.entities[:20]],
                }
                for name, dim in report.dimensions.items()
            },
            "errors": report.errors,
        }
