"""``meshops assess`` command group — assessment skills."""

from __future__ import annotations

import click
from rich.console import Console

from meshops_copilot.core.config import load_config

console = Console()


@click.group()
def assess() -> None:
    """Assess data assets for quality and certification readiness."""


@assess.command("golden-reports")
@click.option(
    "--lookback", default=30, show_default=True, type=int,
    help="Rolling window in days for usage and performance signals.",
)
@click.option(
    "--max-log-records", default=50_000, show_default=True, type=int,
    help="Maximum activity-log records to fetch from Superset.",
)
@click.option(
    "--duplicate-threshold", default=0.50, show_default=True, type=float,
    help="Minimum Jaccard similarity to flag a duplicate pair (0.0–1.0).",
)
@click.option(
    "--output", default=None, metavar="PATH",
    help="Write JSON report to this file.",
)
@click.pass_context
def golden_reports(
    ctx: click.Context,
    lookback: int,
    max_log_records: int,
    duplicate_threshold: float,
    output: str | None,
) -> None:
    """Assess Superset dashboards for golden report candidacy.

    Collects usage, ownership, performance, and dataset quality signals
    from Superset to score and categorise every dashboard.

    \b
    Output buckets:
      - Golden candidates      — high-quality, ready for certification
      - Needs work             — medium score, fixable gaps listed
      - Anti-golden            — stale, expensive, or unreliable
      - Duplicates to merge    — dashboards with high chart overlap

    \b
    Examples:

    \b
      # Default 30-day assessment
      meshops assess golden-reports

    \b
      # Wider window, save report
      meshops assess golden-reports --lookback 90 --output golden.json

    \b
      # Stricter duplicate detection
      meshops assess golden-reports --duplicate-threshold 0.3
    """
    cfg = load_config()

    from meshops_copilot.skills.golden_report.skill import GoldenReportSkill

    skill = GoldenReportSkill(
        cfg=cfg.superset,
        output_file=output,
        lookback_days=lookback,
        max_log_records=max_log_records,
        duplicate_threshold=duplicate_threshold,
    )
    result = skill.run()

    if result.status.value == "failed":
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")
        raise SystemExit(1)
    elif result.status.value == "degraded":
        console.print(f"\n[yellow]Completed with warnings:[/yellow] {result.summary}")
        for err in result.errors[:5]:
            console.print(f"  [yellow]![/yellow] {err}")
    else:
        console.print(f"\n[green]Done:[/green] {result.summary}")
