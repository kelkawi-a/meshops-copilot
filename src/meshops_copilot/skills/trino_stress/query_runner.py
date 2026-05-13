"""Low-level query execution against the Trino REST API.

Delegates HTTP to TrinoConnector; adds concurrency orchestration.
"""

from __future__ import annotations

import statistics
import threading
import time

from meshops_copilot.connectors.trino import TrinoConnector
from meshops_copilot.skills.trino_stress.models import QueryResult, RunResult


def _pct(lst: list[float], p: int) -> float | None:
    if not lst:
        return None
    s = sorted(lst)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


class QueryRunner:
    """Wraps a TrinoConnector and adds concurrent burst execution."""

    def __init__(self, connector: TrinoConnector) -> None:
        self._conn = connector

    # ── Single query ──────────────────────────────────────────────────────────

    def run(self, name: str, sql: str) -> QueryResult:
        elapsed, stats, error = self._conn.execute(sql)
        return QueryResult(name=name, elapsed=elapsed, stats=stats, error=error)

    # ── Concurrent burst ──────────────────────────────────────────────────────

    def run_concurrent(self, sql: str, workers: int) -> RunResult:
        """Fire ``workers`` copies of ``sql`` simultaneously and collect timings."""
        times: list[float] = []
        errors: list[str] = []
        peak_mems: list[float] = []
        rows: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(workers)

        def _worker() -> None:
            barrier.wait()
            elapsed, stats, err = self._conn.execute(sql)
            with lock:
                if err:
                    errors.append(err)
                else:
                    times.append(elapsed)  # type: ignore[arg-type]
                    peak_mems.append(stats.get("peakUserMemoryBytes", 0))
                    rows.append(stats.get("processedRows", 0))

        threads = [threading.Thread(target=_worker) for _ in range(workers)]
        t_wall = time.monotonic()
        for t in threads:
            t.start()

        # sample mid-run metrics
        time.sleep(2)
        docker_mid = self._conn.docker_stats()
        cluster_mid = self._conn.cluster_stats()

        for t in threads:
            t.join()
        wall = time.monotonic() - t_wall
        docker_end = self._conn.docker_stats()

        completed = len(times)
        return RunResult(
            workers=workers,
            completed=completed,
            errors=len(errors),
            error_msgs=errors[:5],
            wall=wall,
            qps=completed / wall if wall > 0 else 0.0,
            times=times,
            p50=_pct(times, 50),
            p95=_pct(times, 95),
            p99=_pct(times, 99),
            max=max(times) if times else None,
            peak_mem_mb=max(peak_mems) / 1024 / 1024 if peak_mems else 0.0,
            rows=max(rows) if rows else 0,
            docker_mid=docker_mid,
            docker_end=docker_end,
            cluster_mid=cluster_mid,
        )
