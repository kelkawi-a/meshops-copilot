"""Low-level chart execution against the Superset REST API.

Delegates HTTP to SupersetConnector; adds concurrency orchestration,
following the same pattern as trino_stress.query_runner.
"""

from __future__ import annotations

import threading
import time

from meshops_copilot.connectors.superset import SupersetConnector
from meshops_copilot.skills.superset_stress.models import ChartResult, DashboardRunResult


def _pct(lst: list[float], p: int) -> float | None:
    if not lst:
        return None
    s = sorted(lst)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


class DashboardRunner:
    """Wraps a SupersetConnector and adds concurrent burst execution."""

    def __init__(self, connector: SupersetConnector) -> None:
        self._conn = connector
        # Ensure we are authenticated before any timed work begins.
        self._conn.login()

    # ── Single chart ──────────────────────────────────────────────────────────

    def run(self, name: str, chart_id: int, query_context: dict) -> ChartResult:
        """Fire a single chart/data request and return a ChartResult."""
        elapsed, stats, error = self._conn.chart_data(query_context)
        return ChartResult(
            chart_id=chart_id,
            name=name,
            elapsed=elapsed,
            stats=stats,
            error=error,
        )

    # ── Concurrent burst ──────────────────────────────────────────────────────

    def run_concurrent(
        self,
        name: str,
        chart_id: int,
        query_context: dict,
        workers: int,
    ) -> DashboardRunResult:
        """Fire ``workers`` simultaneous copies of the same chart request.

        A ``threading.Barrier`` is used to ensure all threads start at the
        same instant, giving a true concurrent burst rather than a staggered
        ramp.
        """
        times: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(workers)

        def _worker() -> None:
            barrier.wait()  # synchronised start
            elapsed, _stats, err = self._conn.chart_data(query_context)
            with lock:
                if err:
                    errors.append(err)
                elif elapsed is not None:
                    times.append(elapsed)

        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(workers)]
        t_wall = time.monotonic()
        for t in threads:
            t.start()

        # Sample mid-run container metrics (2 s into the burst).
        time.sleep(2)
        docker_mid = self._conn.docker_stats()

        for t in threads:
            t.join()
        wall = time.monotonic() - t_wall
        docker_end = self._conn.docker_stats()

        completed = len(times)
        return DashboardRunResult(
            workers=workers,
            completed=completed,
            errors=len(errors),
            error_msgs=errors[:5],
            wall=wall,
            rps=completed / wall if wall > 0 else 0.0,
            times=times,
            p50=_pct(times, 50),
            p95=_pct(times, 95),
            p99=_pct(times, 99),
            max=max(times) if times else None,
            docker_mid=docker_mid,
            docker_end=docker_end,
        )
