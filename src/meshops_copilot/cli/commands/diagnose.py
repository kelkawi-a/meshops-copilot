"""``meshops diagnose`` command — Grafana / Prometheus diagnostics (stub)."""

from __future__ import annotations

import click


@click.group()
def diagnose() -> None:
    """Diagnose observability signals (Grafana, Prometheus)."""


@diagnose.command("run")
@click.pass_context
def diagnose_run(ctx: click.Context) -> None:
    """Analyse Grafana dashboards and Prometheus metrics for bottlenecks."""
    click.echo("diagnose: not yet implemented.")
