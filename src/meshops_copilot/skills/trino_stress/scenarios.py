"""Phase implementations for the trino_stress skill.

Each function maps directly to one scenario phase and returns
structured data that ends up in StressReport.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict

from rich.console import Console
from rich.table import Table

from meshops_copilot.skills.trino_stress.models import BaselineResult, RunResult
from meshops_copilot.skills.trino_stress.query_runner import QueryRunner

console = Console()


# ── Warmup ────────────────────────────────────────────────────────────────────

def run_warmup(runner: QueryRunner, queries: dict[str, str], names: list[str] | None = None) -> None:
    warmup_names = names or ["light_count", "heavy_join", "cross_catalog"]
    console.print("\n[bold]── Warmup (results discarded)[/bold]")
    for name in warmup_names:
        sql = queries.get(name)
        if not sql:
            continue
        result = runner.run(name, sql)
        if result.ok:
            console.print(f"  {name}: {result.elapsed:.2f}s")
        else:
            console.print(f"  {name}: [red]ERROR[/red] — {result.error}")


# ── Baseline ──────────────────────────────────────────────────────────────────

def run_baseline(runner: QueryRunner, queries: dict[str, str], runs: int = 5) -> dict[str, BaselineResult]:
    console.print(f"\n[bold]── Phase 1: Baseline (serial, {runs} runs each)[/bold]")
    results: dict[str, BaselineResult] = {}

    for name, sql in queries.items():
        times: list[float] = []
        errors: list[str] = []
        mems: list[float] = []

        for i in range(runs):
            r = runner.run(name, sql)
            if r.error:
                errors.append(r.error)
                console.print(f"  {name:22s} run {i + 1}: [red]ERROR[/red] — {r.error}")
            else:
                times.append(r.elapsed)  # type: ignore[arg-type]
                mem_mb = r.stats.get("peakUserMemoryBytes", 0) / 1024 / 1024
                mems.append(mem_mb)
                console.print(
                    f"  {name:22s} run {i + 1}: {r.elapsed:.2f}s  "
                    f"mem={mem_mb:.1f}MB  rows={r.stats.get('processedRows', '?')}"
                )

        results[name] = BaselineResult(
            name=name,
            times=times,
            errors=errors,
            peak_mem_mb=max(mems) if mems else 0.0,
        )
    return results


# ── Concurrency ramp ──────────────────────────────────────────────────────────

def run_concurrency_ramp(
    runner: QueryRunner,
    sql: str,
    levels: list[int],
    stop_at_error_rate: int = 100,
) -> dict[int, RunResult]:
    console.print(f"\n[bold]── Phase 2: Concurrency ramp (levels: {levels})[/bold]")
    results: dict[int, RunResult] = {}

    for workers in levels:
        r = runner.run_concurrent(sql, workers)
        results[workers] = r
        console.print(
            f"  workers={workers:2d}  done={r.completed:2d}  err={r.errors}  "
            f"wall={r.wall:.1f}s  qps={r.qps:.2f}  "
            f"p50={r.p50:.2f}s  p95={r.p95:.2f}s  "
            f"cpu={r.docker_mid.get('cpu', '?')}  "
            f"mem={r.docker_mid.get('mem_usage', '?')} ({r.docker_mid.get('mem_perc', '?')})"
        )
        if r.errors:
            console.print(f"    Errors: {r.error_msgs}")
        error_rate = (r.errors / workers * 100) if workers else 0
        if error_rate >= stop_at_error_rate:
            console.print(f"  [yellow]!! ≥{stop_at_error_rate}% error rate at concurrency={workers}. Stopping ramp.[/yellow]")
            break

    return results


# ── Mixed workload ────────────────────────────────────────────────────────────

def run_mixed(runner: QueryRunner, queries: dict[str, str], workers_per_query: int = 2) -> dict:
    console.print(f"\n[bold]── Phase 3: Mixed workload ({workers_per_query} workers per query type)[/bold]")
    lock = threading.Lock()
    timings: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, list[str]] = defaultdict(list)
    total_threads = len(queries) * workers_per_query
    barrier = threading.Barrier(total_threads)

    def _worker(name: str, sql: str) -> None:
        barrier.wait()
        r = runner.run(name, sql)
        with lock:
            if r.error:
                errors[name].append(r.error)
            else:
                timings[name].append(r.elapsed)  # type: ignore[arg-type]

    threads = [
        threading.Thread(target=_worker, args=(name, sql))
        for name, sql in queries.items()
        for _ in range(workers_per_query)
    ]

    t_wall = time.monotonic()
    for t in threads:
        t.start()

    time.sleep(3)
    docker_mid = runner._conn.docker_stats()
    cluster_mid = runner._conn.cluster_stats()

    for t in threads:
        t.join()
    wall = time.monotonic() - t_wall
    docker_end = runner._conn.docker_stats()

    console.print(
        f"  Wall: {wall:.1f}s  "
        f"cpu={docker_mid.get('cpu', '?')}  "
        f"mem={docker_mid.get('mem_usage', '?')} ({docker_mid.get('mem_perc', '?')})"
    )
    for name in queries:
        ts = timings[name]
        err = len(errors[name])
        if ts:
            console.print(f"    {name:22s}  median={statistics.median(ts):.2f}s  max={max(ts):.2f}s  errors={err}")
        else:
            console.print(f"    {name:22s}  ALL FAILED  errors={err}")

    return {
        "wall": wall,
        "timings": dict(timings),
        "errors": dict(errors),
        "docker_mid": docker_mid,
        "docker_end": docker_end,
        "cluster": cluster_mid,
    }


# ── Memory pressure ───────────────────────────────────────────────────────────

def run_memory_pressure(
    runner: QueryRunner,
    queries: dict[str, str],
    workers: int = 8,
) -> dict[str, RunResult]:
    console.print(f"\n[bold]── Phase 4: Memory pressure ({workers}× concurrent)[/bold]")
    results: dict[str, RunResult] = {}

    for name, sql in queries.items():
        r = runner.run_concurrent(sql, workers)
        results[name] = r
        console.print(
            f"  {name:28s}  done={r.completed}/{workers}  err={r.errors}  "
            f"wall={r.wall:.1f}s  "
            f"p50={r.p50:.2f}s  max={r.max:.2f}s  "
            f"peak_mem={r.peak_mem_mb:.0f}MB/query  "
            f"cpu={r.docker_mid.get('cpu', '?')}  "
            f"mem={r.docker_mid.get('mem_usage', '?')} ({r.docker_mid.get('mem_perc', '?')})"
        )
        if r.errors:
            console.print(f"    Errors: {r.error_msgs}")
    return results


# ── Breaking point ────────────────────────────────────────────────────────────

def run_breaking_point(
    runner: QueryRunner,
    sql: str,
    levels: list[int],
    stop_at_error_rate: int = 50,
) -> dict[int, RunResult]:
    console.print(f"\n[bold]── Phase 5: Breaking point (levels: {levels})[/bold]")
    results: dict[int, RunResult] = {}

    for workers in levels:
        r = runner.run_concurrent(sql, workers)
        results[workers] = r
        error_rate = r.errors / workers * 100 if workers else 0
        console.print(
            f"  workers={workers:3d}  done={r.completed:3d}  "
            f"err={r.errors} ({error_rate:.0f}%)  "
            f"wall={r.wall:.1f}s  qps={r.qps:.2f}  "
            f"p95={r.p95:.2f}s  "
            f"cpu={r.docker_end.get('cpu', '?')}  "
            f"mem={r.docker_end.get('mem_usage', '?')} ({r.docker_end.get('mem_perc', '?')})"
        )
        if r.errors:
            console.print(f"    Sample errors: {r.error_msgs[:2]}")
        if error_rate >= stop_at_error_rate:
            console.print(f"  [yellow]!! ≥{stop_at_error_rate}% error rate at concurrency={workers}. Stopping.[/yellow]")
            break

    return results
