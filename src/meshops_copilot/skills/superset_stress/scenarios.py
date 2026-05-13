"""Phase implementations for the superset_stress skill.

Each function maps directly to one scenario phase and returns
structured data that ends up in SupersetStressReport.
"""

from __future__ import annotations

import statistics

from rich.console import Console
from rich.table import Table

from meshops_copilot.skills.superset_stress.models import (
    BaselineChartResult,
    DashboardRunResult,
)
from meshops_copilot.skills.superset_stress.dashboard_runner import DashboardRunner

console = Console()


# ── Warmup ────────────────────────────────────────────────────────────────────

def run_warmup(
    runner: DashboardRunner,
    charts: dict[str, dict],
    names: list[str] | None = None,
) -> None:
    """Fire each nominated chart once; results are discarded.

    ``charts`` is a dict keyed by chart name with values
    ``{"chart_id": int, "query_context": dict, ...}``.
    ``names`` optionally restricts which charts are fired; defaults to all.
    """
    warmup_names = names or list(charts.keys())
    console.print("\n[bold]── Warmup (results discarded)[/bold]")
    for name in warmup_names:
        entry = charts.get(name)
        if not entry:
            continue
        result = runner.run(name, entry["chart_id"], entry["query_context"])
        if result.ok:
            console.print(f"  {name}: {result.elapsed:.2f}s")
        else:
            console.print(f"  {name}: [red]ERROR[/red] — {result.error}")


# ── Baseline ──────────────────────────────────────────────────────────────────

def run_baseline(
    runner: DashboardRunner,
    charts: dict[str, dict],
    runs: int = 3,
) -> dict[str, BaselineChartResult]:
    """Run each chart serially ``runs`` times and collect per-chart latencies."""
    console.print(f"\n[bold]── Phase 1: Baseline (serial, {runs} runs each)[/bold]")
    results: dict[str, BaselineChartResult] = {}

    for name, entry in charts.items():
        chart_id = entry["chart_id"]
        qc = entry["query_context"]
        times: list[float] = []
        errors: list[str] = []

        for i in range(runs):
            r = runner.run(name, chart_id, qc)
            if r.error:
                errors.append(r.error)
                console.print(
                    f"  {name:40s} run {i + 1}: [red]ERROR[/red] — {r.error}"
                )
            else:
                times.append(r.elapsed)  # type: ignore[arg-type]
                console.print(
                    f"  {name:40s} run {i + 1}: {r.elapsed:.2f}s  "
                    f"rows={r.stats.get('row_count', '?')}"
                )

        results[name] = BaselineChartResult(
            chart_id=chart_id,
            name=name,
            times=times,
            errors=errors,
        )

    return results


# ── Concurrency ramp ──────────────────────────────────────────────────────────

def run_concurrency_ramp(
    runner: DashboardRunner,
    name: str,
    chart_id: int,
    query_context: dict,
    levels: list[int],
    stop_at_error_rate: int = 100,
) -> dict[int, DashboardRunResult]:
    """Ramp concurrent users firing the same chart and record timing per level."""
    console.print(f"\n[bold]── Phase 2: Concurrency ramp (chart={name}, levels={levels})[/bold]")
    results: dict[int, DashboardRunResult] = {}

    for workers in levels:
        r = runner.run_concurrent(name, chart_id, query_context, workers)
        results[workers] = r
        console.print(
            f"  workers={workers:2d}  done={r.completed:2d}  err={r.errors}  "
            f"wall={r.wall:.1f}s  rps={r.rps:.2f}  "
            f"p50={'—' if r.p50 is None else f'{r.p50:.2f}s'}  "
            f"p95={'—' if r.p95 is None else f'{r.p95:.2f}s'}  "
            f"cpu={r.docker_mid.get('cpu', '?')}  "
            f"mem={r.docker_mid.get('mem_usage', '?')} ({r.docker_mid.get('mem_perc', '?')})"
        )
        if r.errors:
            console.print(f"    Errors: {r.error_msgs}")
        error_rate = (r.errors / workers * 100) if workers else 0
        if error_rate >= stop_at_error_rate:
            console.print(
                f"  [yellow]!! ≥{stop_at_error_rate}% error rate at "
                f"concurrency={workers}. Stopping ramp.[/yellow]"
            )
            break

    return results


# ── Breaking point ────────────────────────────────────────────────────────────

def run_breaking_point(
    runner: DashboardRunner,
    name: str,
    chart_id: int,
    query_context: dict,
    levels: list[int],
    stop_at_error_rate: int = 50,
) -> dict[int, DashboardRunResult]:
    """Push concurrency to progressively higher levels until errors dominate."""
    console.print(f"\n[bold]── Phase 3: Breaking point (chart={name}, levels={levels})[/bold]")
    results: dict[int, DashboardRunResult] = {}

    for workers in levels:
        r = runner.run_concurrent(name, chart_id, query_context, workers)
        results[workers] = r
        error_rate = r.errors / workers * 100 if workers else 0
        console.print(
            f"  workers={workers:3d}  done={r.completed:3d}  "
            f"err={r.errors} ({error_rate:.0f}%)  "
            f"wall={r.wall:.1f}s  rps={r.rps:.2f}  "
            f"p95={'—' if r.p95 is None else f'{r.p95:.2f}s'}  "
            f"cpu={r.docker_end.get('cpu', '?')}  "
            f"mem={r.docker_end.get('mem_usage', '?')} ({r.docker_end.get('mem_perc', '?')})"
        )
        if r.errors:
            console.print(f"    Sample errors: {r.error_msgs[:2]}")
        if error_rate >= stop_at_error_rate:
            console.print(
                f"  [yellow]!! ≥{stop_at_error_rate}% error rate at "
                f"concurrency={workers}. Stopping.[/yellow]"
            )
            break

    return results
