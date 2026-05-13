"""TrinoStressSkill — orchestrates all stress-test phases from a scenario YAML."""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.connectors.trino import TrinoConnector
from meshops_copilot.core.config import TrinoConfig
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.trino_stress.models import StressReport
from meshops_copilot.skills.trino_stress.query_runner import QueryRunner
from meshops_copilot.skills.trino_stress.scenarios import (
    run_baseline,
    run_breaking_point,
    run_concurrency_ramp,
    run_memory_pressure,
    run_mixed,
    run_warmup,
)
from meshops_copilot.skills.trino_stress.workload import load_scenario, resolve_queries

console = Console()

# ── Default phase config (used when scenario YAML omits a section) ────────────

_DEFAULTS: dict = {
    "warmup": {"enabled": True, "queries": ["light_count", "heavy_join", "cross_catalog"]},
    "baseline": {"enabled": True, "runs": 5},
    "concurrency_ramp": {"enabled": True, "query": "heavy_join", "levels": [1, 2, 4, 8, 16, 32]},
    "mixed_workload": {"enabled": True, "workers_per_query": 2},
    "memory_pressure": {"enabled": True, "queries": ["cross_catalog", "window_functions"], "workers": 8},
    "breaking_point": {
        "enabled": True,
        "query": "heavy_join",
        "levels": [32, 48, 64, 96],
        "stop_at_error_rate": 50,
    },
}


class TrinoStressSkill(BaseSkill):
    """Run a multi-phase Trino stress test defined by a scenario YAML."""

    name = "trino_stress"

    def __init__(self, cfg: TrinoConfig, output_file: str | None = None) -> None:
        self._cfg = cfg
        self._output_file = output_file or cfg.results_file

    def run(self, scenario_path: str | None = None, **kwargs) -> SkillResult:
        scenario: dict = {}
        scenario_name = "default"

        if scenario_path:
            scenario = load_scenario(scenario_path)
            scenario_name = scenario.get("name", Path(scenario_path).stem)

        # Connection details are fully resolved by load_config() before the skill
        # is constructed (priority: CLI flags > env vars > config YAML > scenario YAML).
        # Use self._cfg directly — do not re-read them from the scenario here.
        connector = TrinoConnector(
            url=self._cfg.url,
            user=self._cfg.user,
            password=self._cfg.password,
            timeout=self._cfg.timeout,
            verify_ssl=self._cfg.verify_ssl,
        )
        runner = QueryRunner(connector)

        console.rule(f"[bold cyan]Trino Stress Test — {scenario_name}[/bold cyan]")
        console.print(f"  Target  : {self._cfg.url}")
        console.print(f"  User    : {self._cfg.user}  ({'password auth' if self._cfg.password else 'no auth'})")
        console.print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # ── Query resolution: explicit → discovery → built-in fallback ─────────
        queries, discovery_meta = self._resolve_queries(scenario, connector)
        if not queries:
            return self._failed(
                ["Schema discovery found no usable tables and no built-in queries apply."]
            )

        phases = scenario.get("phases", _DEFAULTS)

        report = StressReport(
            target=self._cfg.url,
            scenario=scenario_name,
            query_source=discovery_meta.get("source", "unknown"),
            discovered_tables=discovery_meta.get("tables", []),
            generated_queries=queries if discovery_meta.get("source") == "discovered" else {},
        )
        errors: list[str] = []

        # ── Warmup ─────────────────────────────────────────────────────────────
        w_cfg = {**_DEFAULTS["warmup"], **phases.get("warmup", {})}
        if w_cfg.get("enabled", True):
            run_warmup(runner, queries, w_cfg.get("queries"))

        # ── Baseline ───────────────────────────────────────────────────────────
        b_cfg = {**_DEFAULTS["baseline"], **phases.get("baseline", {})}
        if b_cfg.get("enabled", True):
            report.baseline = run_baseline(runner, queries, runs=int(b_cfg.get("runs", 5)))

        # ── Concurrency ramp ───────────────────────────────────────────────────
        cr_cfg = {**_DEFAULTS["concurrency_ramp"], **phases.get("concurrency_ramp", {})}
        if cr_cfg.get("enabled", True):
            ramp_query = queries.get(cr_cfg["query"], "")
            if ramp_query:
                report.concurrency = run_concurrency_ramp(
                    runner,
                    ramp_query,
                    levels=cr_cfg["levels"],
                )
            else:
                errors.append(f"concurrency_ramp: query '{cr_cfg['query']}' not found in workload")

        # ── Mixed workload ─────────────────────────────────────────────────────
        mx_cfg = {**_DEFAULTS["mixed_workload"], **phases.get("mixed_workload", {})}
        if mx_cfg.get("enabled", True):
            report.mixed = run_mixed(runner, queries, workers_per_query=int(mx_cfg.get("workers_per_query", 2)))

        # ── Memory pressure ────────────────────────────────────────────────────
        mp_cfg = {**_DEFAULTS["memory_pressure"], **phases.get("memory_pressure", {})}
        if mp_cfg.get("enabled", True):
            mem_queries = {n: queries[n] for n in mp_cfg.get("queries", []) if n in queries}
            if mem_queries:
                report.memory = run_memory_pressure(runner, mem_queries, workers=int(mp_cfg.get("workers", 8)))

        # ── Breaking point ─────────────────────────────────────────────────────
        bp_cfg = {**_DEFAULTS["breaking_point"], **phases.get("breaking_point", {})}
        if bp_cfg.get("enabled", True):
            bp_query = queries.get(bp_cfg["query"], "")
            if bp_query:
                report.breaking = run_breaking_point(
                    runner,
                    bp_query,
                    levels=bp_cfg["levels"],
                    stop_at_error_rate=int(bp_cfg.get("stop_at_error_rate", 50)),
                )
            else:
                errors.append(f"breaking_point: query '{bp_cfg['query']}' not found in workload")

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

    # ── Query resolution ───────────────────────────────────────────────────────

    def _resolve_queries(
        self,
        scenario: dict,
        connector: TrinoConnector,
    ) -> tuple[dict[str, str], dict]:
        """Return (queries_dict, discovery_meta).

        Priority:
          1. Explicit ``queries:`` block in the scenario YAML.
          2. Schema discovery (default when ``queries:`` is absent).
          3. All built-in queries (fallback if discovery yields nothing).
        """
        from meshops_copilot.skills.trino_stress.discovery import SchemaDiscovery
        from meshops_copilot.skills.trino_stress.query_builder import QueryBuilder

        # ── Explicit queries ───────────────────────────────────────────────────
        if "queries" in scenario:
            return resolve_queries(scenario), {"source": "explicit"}

        # ── Discovery ─────────────────────────────────────────────────────────
        disc_cfg = scenario.get("discovery", {})
        if disc_cfg.get("enabled", True):
            console.print("\n[bold yellow]No queries specified — running schema discovery…[/bold yellow]")
            try:
                disc = SchemaDiscovery(
                    connector,
                    include_catalogs=disc_cfg.get("include_catalogs"),
                    exclude_catalogs=disc_cfg.get("exclude_catalogs"),
                    exclude_schemas=disc_cfg.get("exclude_schemas"),
                    max_tables=int(disc_cfg.get("max_tables", 50)),
                    max_workers=int(disc_cfg.get("max_workers", 8)),
                    table_timeout=int(disc_cfg.get("table_timeout", 15)),
                    catalog_timeout=int(disc_cfg.get("catalog_timeout", 30)),
                )
                result = disc.run()

                self._print_discovery(result)

                queries = QueryBuilder(result).build()
                if queries:
                    meta = {
                        "source": "discovered",
                        "catalogs": result.catalogs,
                        "tables": [t.full_name for t in result.tables],
                        "joins_found": len(result.joins),
                    }
                    return queries, meta

                console.print(
                    "[yellow]Discovery found no usable tables — falling back to built-in queries.[/yellow]"
                )
            except Exception as exc:
                console.print(f"[yellow]Discovery failed ({exc}) — falling back to built-in queries.[/yellow]")

        # ── Built-in fallback ──────────────────────────────────────────────────
        return resolve_queries({}), {"source": "builtin"}

    @staticmethod
    def _print_discovery(result) -> None:
        from meshops_copilot.skills.trino_stress.discovery import DiscoveryResult
        t = Table(title="Discovered Schema", show_header=True, header_style="bold cyan")
        t.add_column("Table", style="cyan")
        t.add_column("Rows (est.)", justify="right")
        t.add_column("Cols", justify="right")
        t.add_column("Numeric", justify="right")
        t.add_column("Categorical", justify="right")
        t.add_column("FKs", justify="right")
        for tbl in sorted(result.tables, key=lambda x: x.estimated_size, reverse=True)[:15]:
            t.add_row(
                tbl.full_name,
                f"{tbl.row_count:,}" if tbl.row_count else "?",
                str(len(tbl.columns)),
                str(len(tbl.numeric_columns)),
                str(len(tbl.categorical_columns)),
                str(len(tbl.fk_columns)),
            )
        console.print(t)
        if result.joins:
            console.print(f"  [green]Detected {len(result.joins)} join path(s)[/green]")
            for jp in result.joins[:6]:
                console.print(
                    f"    {jp.from_table.full_name}.{jp.from_column}"
                    f" → {jp.to_table.full_name}.{jp.to_column}"
                )

    # ── Reporting ──────────────────────────────────────────────────────────────

    def _print_summary(self, report: StressReport) -> None:
        console.rule("[bold]Summary[/bold]")

        # Baseline table
        if report.baseline:
            t = Table(title="Baseline (serial)", show_header=True, header_style="bold")
            t.add_column("Query", style="cyan")
            t.add_column("Runs", justify="right")
            t.add_column("Errors", justify="right")
            t.add_column("Min", justify="right")
            t.add_column("Median", justify="right")
            t.add_column("Max", justify="right")
            t.add_column("PeakMem", justify="right")
            for name, r in report.baseline.items():
                if r.times:
                    t.add_row(
                        name,
                        str(len(r.times)),
                        str(len(r.errors)),
                        f"{min(r.times):.2f}s",
                        f"{statistics.median(r.times):.2f}s",
                        f"{max(r.times):.2f}s",
                        f"{r.peak_mem_mb:.1f}MB",
                    )
                else:
                    t.add_row(name, "0", str(len(r.errors)), "—", "—", "—", "—")
            console.print(t)

        # Concurrency ramp table
        if report.concurrency:
            t = Table(title="Concurrency Ramp", show_header=True, header_style="bold")
            for col in ["Workers", "Done", "Err", "QPS", "p50", "p95", "p99", "CPU", "Mem%"]:
                t.add_column(col, justify="right")
            for w, r in sorted(report.concurrency.items()):
                t.add_row(
                    str(w), str(r.completed), str(r.errors),
                    f"{r.qps:.2f}",
                    f"{r.p50:.2f}s" if r.p50 else "—",
                    f"{r.p95:.2f}s" if r.p95 else "—",
                    f"{r.p99:.2f}s" if r.p99 else "—",
                    r.docker_mid.get("cpu", "?"),
                    r.docker_mid.get("mem_perc", "?"),
                )
            console.print(t)

    def _save(self, report: StressReport) -> None:
        out = Path(self._output_file)
        with open(out, "w") as fh:
            json.dump(self._report_to_dict(report), fh, indent=2, default=str)
        console.print(f"\n[green]Results saved to {out}[/green]")

    @staticmethod
    def _report_to_dict(report: StressReport) -> dict:
        from dataclasses import asdict
        return {
            "target": report.target,
            "scenario": report.scenario,
            "query_source": report.query_source,
            "discovered_tables": report.discovered_tables,
            "generated_queries": report.generated_queries,
            "baseline": {k: asdict(v) for k, v in report.baseline.items()},
            "concurrency": {str(k): asdict(v) for k, v in report.concurrency.items()},
            "mixed": report.mixed,
            "memory": {k: asdict(v) for k, v in report.memory.items()},
            "breaking": {str(k): asdict(v) for k, v in report.breaking.items()},
        }
