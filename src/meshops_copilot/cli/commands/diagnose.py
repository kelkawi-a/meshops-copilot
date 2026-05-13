"""``meshops diagnose`` command — Grafana / Prometheus diagnostics."""

from __future__ import annotations

import click

from meshops_copilot.core.config import load_config


@click.group()
def diagnose() -> None:
    """Diagnose observability signals (Grafana, Prometheus)."""


@diagnose.command("run")
@click.argument("query", nargs=-1)
@click.option("--output", default=None, metavar="PATH", help="Write JSON results to this file.")
@click.option("--namespace", default=None, metavar="REGEX",
              help="Kubernetes namespace regex to filter metrics (default: all).")
@click.option("--window", default=None, type=int, metavar="MINUTES",
              help="Analysis window in minutes (default: 60, or auto-detected from query).")
@click.option("--url", default=None, metavar="URL",
              help="Prometheus URL (overrides config / PROMETHEUS_URL).")
@click.pass_context
def diagnose_run(
    ctx: click.Context,
    query: tuple[str, ...],
    output: str | None,
    namespace: str | None,
    window: int | None,
    url: str | None,
) -> None:
    """Analyse Prometheus metrics for bottlenecks.

    Accepts an optional natural-language QUERY to focus the analysis:

    \b
        meshops diagnose run "why was superset slow between 10:30 and 11:00"
        meshops diagnose run "check CPU and memory pressure on trino pods"
        meshops diagnose run   # full diagnostic sweep (no LLM needed)
    """
    cfg = load_config()

    # CLI flag overrides
    if url:
        cfg.prometheus.url = url

    from meshops_copilot.skills.grafana_diagnostics.skill import GrafanaDiagnosticsSkill

    skill = GrafanaDiagnosticsSkill(
        prometheus_cfg=cfg.prometheus,
        grafana_cfg=cfg.grafana,
        llm_cfg=cfg.llm,
        output_file=output,
    )

    # Join tuple of words back into a single query string
    query_str = " ".join(query).strip() if query else None

    result = skill.run(query=query_str)

    if result.errors:
        for err in result.errors:
            click.echo(f"ERROR: {err}", err=True)
        raise SystemExit(1)


@diagnose.command("noisy-neighbor")
@click.option("--output", default=None, metavar="PATH",
              help="Write JSON results to this file (default: noisy_neighbor_results.json).")
@click.option("--lookback", default=168, type=int, show_default=True, metavar="HOURS",
              help="How many hours of history to analyze.")
@click.pass_context
def diagnose_noisy_neighbor(ctx: click.Context, output: str | None, lookback: int) -> None:
    """Detect Superset dashboards/users causing disproportionate Trino load.

    Correlates Superset activity logs and query history with Trino
    system.runtime.queries to find entities whose resource consumption
    far exceeds their share of traffic.

    \b
        meshops diagnose noisy-neighbor
        meshops diagnose noisy-neighbor --lookback 24
        meshops diagnose noisy-neighbor --output reports/noisy.json
    """
    cfg = load_config()

    from meshops_copilot.skills.noisy_neighbor.skill import NoisyNeighborSkill

    skill = NoisyNeighborSkill(cfg, output_file=output, lookback_hours=lookback)
    result = skill.run(lookback_hours=lookback)

    if result.errors:
        for err in result.errors:
            click.echo(f"ERROR: {err}", err=True)
        raise SystemExit(1)
