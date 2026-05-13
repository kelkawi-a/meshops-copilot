"""SupersetStressSkill — orchestrates all stress-test phases from a scenario YAML."""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.connectors.superset import SupersetConnector
from meshops_copilot.core.config import SupersetConfig
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.superset_stress.dashboard_runner import DashboardRunner
from meshops_copilot.skills.superset_stress.models import SupersetStressReport
from meshops_copilot.skills.superset_stress.scenarios import (
    run_baseline,
    run_breaking_point,
    run_concurrency_ramp,
    run_warmup,
)
from meshops_copilot.skills.superset_stress.workload import (
    BUILTIN_CHARTS,
    load_scenario,
    resolve_charts,
)

console = Console()

# ── Default phase config (used when scenario YAML omits a section) ────────────

# When discovery is used, the benchmark chart for concurrency/breaking-point
# phases is chosen as the chart with the longest baseline median latency.
# These defaults apply when the built-in catalogue is used instead.

_DEFAULTS: dict = {
    "warmup": {
        "enabled": True,
        # Omit chart names here so warmup uses all discovered/resolved charts.
        # (The scenarios.run_warmup default is to fire every chart once.)
        "charts": None,
    },
    "baseline": {"enabled": True, "runs": 3},
    "concurrency_ramp": {
        "enabled": True,
        # "chart" left unset — auto-selected as slowest chart after baseline.
        "chart": None,
        "levels": [1, 2, 4, 8, 16],
    },
    "breaking_point": {
        "enabled": True,
        "chart": None,
        "levels": [16, 24, 32, 48],
        "stop_at_error_rate": 50,
    },
}

# Hardcoded defaults for the built-in workshop catalogue
# (used only when falling back to BUILTIN_CHARTS).
_BUILTIN_DEFAULTS: dict = {
    "warmup": {"enabled": True, "charts": [
        "events_over_time", "daily_active_sessions", "top_pages_by_traffic"
    ]},
    "concurrency_ramp": {"enabled": True, "chart": "daily_active_sessions",
                         "levels": [1, 2, 4, 8, 16]},
    "breaking_point": {"enabled": True, "chart": "daily_active_sessions",
                       "levels": [16, 24, 32, 48], "stop_at_error_rate": 50},
}


class SupersetStressSkill(BaseSkill):
    """Run a multi-phase Superset stress test defined by a scenario YAML."""

    name = "superset_stress"

    def __init__(self, cfg: SupersetConfig, output_file: str | None = None) -> None:
        self._cfg = cfg
        self._output_file = output_file or "superset_stress_results.json"

    def run(self, scenario_path: str | None = None, **kwargs) -> SkillResult:
        scenario: dict = {}
        scenario_name = "default"

        if scenario_path:
            scenario = load_scenario(scenario_path)
            scenario_name = scenario.get("name", Path(scenario_path).stem)

        # Connection details are fully resolved by load_config() before the skill
        # is constructed (priority: CLI flags > env vars > config YAML > scenario YAML).
        # Use self._cfg directly — do not re-read them from the scenario here.
        connector = SupersetConnector(
            url=self._cfg.url,
            user=self._cfg.user,
            password=self._cfg.password,
        )

        console.rule(f"[bold cyan]Superset Stress Test — {scenario_name}[/bold cyan]")
        console.print(f"  Target  : {self._cfg.url}")
        console.print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # ── Authenticate ──────────────────────────────────────────────────────
        try:
            connector.login()
        except Exception as exc:
            return self._failed([f"Could not authenticate with Superset: {exc}"])

        runner = DashboardRunner(connector)

        # ── Chart resolution: explicit → discovery → built-in fallback ────────
        charts, chart_source = self._resolve_charts(scenario, connector)

        if not charts:
            return self._failed(["No charts resolved — check scenario YAML."])

        console.print(f"  Charts  : {len(charts)} ({chart_source})")

        # ── Phase defaults: adapt to discovered vs built-in catalogue ──────────
        phases = scenario.get("phases", {})
        is_builtin = chart_source == "builtin"
        defaults = _BUILTIN_DEFAULTS if is_builtin else _DEFAULTS

        report = SupersetStressReport(
            target=self._cfg.url,
            scenario=scenario_name,
            chart_source=chart_source,
        )
        errors: list[str] = []

        # ── Warmup ────────────────────────────────────────────────────────────
        w_cfg = {**defaults.get("warmup", _DEFAULTS["warmup"]),
                 **phases.get("warmup", {})}
        if w_cfg.get("enabled", True):
            run_warmup(runner, charts, w_cfg.get("charts"))

        # ── Baseline ──────────────────────────────────────────────────────────
        b_cfg = {**_DEFAULTS["baseline"], **phases.get("baseline", {})}
        if b_cfg.get("enabled", True):
            report.baseline = run_baseline(
                runner, charts, runs=int(b_cfg.get("runs", 3))
            )

        # ── Auto-select benchmark chart (slowest by baseline median) ──────────
        auto_bench = self._slowest_chart(report.baseline) if report.baseline else None

        # ── Concurrency ramp ──────────────────────────────────────────────────
        cr_cfg = {**defaults.get("concurrency_ramp", _DEFAULTS["concurrency_ramp"]),
                  **phases.get("concurrency_ramp", {})}
        if cr_cfg.get("enabled", True):
            bench_name = cr_cfg.get("chart") or auto_bench
            if bench_name is None:
                bench_name = next(iter(charts))  # last resort: first chart
            bench_entry = charts.get(bench_name)
            if bench_entry:
                console.print(
                    f"\n  [dim]Benchmark chart for concurrency: {bench_name}[/dim]"
                )
                report.concurrency = run_concurrency_ramp(
                    runner,
                    name=bench_name,
                    chart_id=bench_entry["chart_id"],
                    query_context=bench_entry["query_context"],
                    levels=cr_cfg["levels"],
                )
            else:
                errors.append(
                    f"concurrency_ramp: chart '{bench_name}' not found in workload"
                )

        # ── Breaking point ────────────────────────────────────────────────────
        bp_cfg = {**defaults.get("breaking_point", _DEFAULTS["breaking_point"]),
                  **phases.get("breaking_point", {})}
        if bp_cfg.get("enabled", True):
            bench_name = bp_cfg.get("chart") or auto_bench
            if bench_name is None:
                bench_name = next(iter(charts))
            bench_entry = charts.get(bench_name)
            if bench_entry:
                report.breaking = run_breaking_point(
                    runner,
                    name=bench_name,
                    chart_id=bench_entry["chart_id"],
                    query_context=bench_entry["query_context"],
                    levels=bp_cfg["levels"],
                    stop_at_error_rate=int(bp_cfg.get("stop_at_error_rate", 50)),
                )
            else:
                errors.append(
                    f"breaking_point: chart '{bench_name}' not found in workload"
                )

        self._print_summary(report)
        self._save(report)

        if errors:
            return self._degraded(
                summary=f"Completed with {len(errors)} configuration warning(s).",
                errors=errors,
                details=self._report_to_dict(report),
            )
        return self._ok(
            summary=f"Stress test '{scenario_name}' completed against {self._cfg.url}.",
            details=self._report_to_dict(report),
        )

    # ── Chart resolution ───────────────────────────────────────────────────────

    def _resolve_charts(
        self,
        scenario: dict,
        connector: SupersetConnector,
    ) -> tuple[dict[str, dict], str]:
        """Return (charts_catalogue, source_label).

        Priority (mirrors trino_stress._resolve_queries):
          1. Explicit ``charts:`` / ``charts_file:`` / ``dashboard_id:`` in scenario.
          2. Live discovery via Superset REST API.
          3. Built-in workshop catalogue (BUILTIN_CHARTS) as last resort.
        """
        # ── 1. Explicit ───────────────────────────────────────────────────────
        if any(k in scenario for k in ("charts", "charts_file", "dashboard_id")):
            return resolve_charts(scenario), self._explicit_source(scenario)

        # ── 2. Discovery ──────────────────────────────────────────────────────
        # Priority for each setting: scenario YAML > env/config > built-in default
        disc_cfg = scenario.get("discovery", {})
        disc_enabled = disc_cfg.get("enabled", self._cfg.discovery_enabled)
        disc_max = int(disc_cfg.get("max_charts", self._cfg.discovery_max_charts))
        if disc_enabled:
            console.print(
                "\n[bold yellow]No charts specified — running chart discovery…[/bold yellow]"
            )
            try:
                from meshops_copilot.skills.superset_stress.discovery import (
                    SupersetDiscovery,
                )

                disc = SupersetDiscovery(
                    connector,
                    max_charts=disc_max,
                )
                result = disc.run()
                self._print_discovery(result)

                if result.catalogue:
                    return result.catalogue, "discovered"

                console.print(
                    "[yellow]Discovery found no usable charts — "
                    "falling back to built-in catalogue.[/yellow]"
                )
            except Exception as exc:
                console.print(
                    f"[yellow]Discovery failed ({exc}) — "
                    "falling back to built-in catalogue.[/yellow]"
                )

        # ── 3. Built-in fallback ──────────────────────────────────────────────
        return dict(BUILTIN_CHARTS), "builtin"

    @staticmethod
    def _explicit_source(scenario: dict) -> str:
        if scenario.get("charts_file"):
            return "file"
        if scenario.get("dashboard_id"):
            return "dashboard"
        return "scenario"

    @staticmethod
    def _print_discovery(result) -> None:
        from meshops_copilot.skills.superset_stress.discovery import DiscoveryResult

        console.print(
            f"  Found  {result.total_found} chart(s) on Superset instance  "
            f"→  {result.built} usable  /  {result.skipped} skipped  "
            f"({result.dashboard_count} dashboard(s))"
        )
        if result.skipped_names:
            console.print(
                f"  Skipped (no metrics): "
                + ", ".join(result.skipped_names[:10])
                + ("…" if len(result.skipped_names) > 10 else "")
            )

        t = Table(
            title="Discovered Charts",
            show_header=True,
            header_style="bold cyan",
        )
        t.add_column("Key", style="cyan")
        t.add_column("Name")
        t.add_column("Viz", style="dim")
        t.add_column("Dashboard", justify="right")
        t.add_column("QC source", style="dim")

        for key, entry in list(result.catalogue.items())[:20]:
            # Show whether QC came from stored context or was built from params.
            qc_source = (
                "stored" if entry["query_context"].get("_source") == "stored"
                else "built"
            )
            t.add_row(
                key,
                entry["name"],
                entry.get("viz_type", ""),
                str(entry["dashboard_id"]) if entry["dashboard_id"] else "—",
                qc_source,
            )

        if len(result.catalogue) > 20:
            t.add_row(f"… {len(result.catalogue) - 20} more …", "", "", "", "")

        console.print(t)

    # ── Auto-benchmark selection ───────────────────────────────────────────────

    @staticmethod
    def _slowest_chart(baseline: dict) -> str | None:
        """Return the chart key with the highest median latency in the baseline."""
        best_name: str | None = None
        best_median = 0.0
        for name, r in baseline.items():
            if r.times:
                med = statistics.median(r.times)
                if med > best_median:
                    best_median = med
                    best_name = name
        return best_name

    # ── Reporting ──────────────────────────────────────────────────────────────

    def _print_summary(self, report: SupersetStressReport) -> None:
        console.rule("[bold]Summary[/bold]")

        if report.baseline:
            t = Table(title="Baseline (serial)", show_header=True, header_style="bold")
            t.add_column("Chart", style="cyan")
            t.add_column("Runs", justify="right")
            t.add_column("Errors", justify="right")
            t.add_column("Min", justify="right")
            t.add_column("Median", justify="right")
            t.add_column("Max", justify="right")
            # Sort by median desc so slowest charts appear first.
            rows = sorted(
                report.baseline.items(),
                key=lambda kv: statistics.median(kv[1].times) if kv[1].times else 0,
                reverse=True,
            )
            for name, r in rows:
                if r.times:
                    t.add_row(
                        name,
                        str(len(r.times)),
                        str(len(r.errors)),
                        f"{min(r.times):.2f}s",
                        f"{statistics.median(r.times):.2f}s",
                        f"{max(r.times):.2f}s",
                    )
                else:
                    t.add_row(name, "0", str(len(r.errors)), "—", "—", "—")
            console.print(t)

        if report.concurrency:
            t = Table(
                title="Concurrency Ramp", show_header=True, header_style="bold"
            )
            for col in ["Workers", "Done", "Err", "RPS", "p50", "p95", "p99", "CPU", "Mem%"]:
                t.add_column(col, justify="right")
            for w, r in sorted(report.concurrency.items()):
                t.add_row(
                    str(w),
                    str(r.completed),
                    str(r.errors),
                    f"{r.rps:.2f}",
                    f"{r.p50:.2f}s" if r.p50 else "—",
                    f"{r.p95:.2f}s" if r.p95 else "—",
                    f"{r.p99:.2f}s" if r.p99 else "—",
                    r.docker_mid.get("cpu", "?"),
                    r.docker_mid.get("mem_perc", "?"),
                )
            console.print(t)

        if report.breaking:
            t = Table(
                title="Breaking Point", show_header=True, header_style="bold"
            )
            for col in ["Workers", "Done", "Err%", "RPS", "p95", "CPU", "Mem%"]:
                t.add_column(col, justify="right")
            for w, r in sorted(report.breaking.items()):
                err_pct = f"{r.errors / w * 100:.0f}%" if w else "?"
                t.add_row(
                    str(w),
                    str(r.completed),
                    err_pct,
                    f"{r.rps:.2f}",
                    f"{r.p95:.2f}s" if r.p95 else "—",
                    r.docker_end.get("cpu", "?"),
                    r.docker_end.get("mem_perc", "?"),
                )
            console.print(t)

    def _save(self, report: SupersetStressReport) -> None:
        out = Path(self._output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as fh:
            json.dump(self._report_to_dict(report), fh, indent=2, default=str)
        console.print(f"\n[green]Results saved to {out}[/green]")

    @staticmethod
    def _report_to_dict(report: SupersetStressReport) -> dict:
        return {
            "target": report.target,
            "scenario": report.scenario,
            "chart_source": report.chart_source,
            "baseline": {k: asdict(v) for k, v in report.baseline.items()},
            "concurrency": {str(k): asdict(v) for k, v in report.concurrency.items()},
            "breaking": {str(k): asdict(v) for k, v in report.breaking.items()},
        }
