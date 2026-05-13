"""``meshops dedupe`` command — duplicate dashboard and metric detection via DataHub MCP."""

from __future__ import annotations

import click
from rich.console import Console

from meshops_copilot.core.config import load_config

console = Console()


@click.group()
def dedupe() -> None:
    """Detect duplicate dashboards and redundant metrics via DataHub."""


@dedupe.command("run")
@click.option(
    "--platform", default=None,
    help="Restrict to a data platform (e.g. superset, looker, tableau).",
)
@click.option(
    "--domain", default=None,
    help="Restrict to a DataHub domain URN or name.",
)
@click.option(
    "--min-confidence", default=0.4, show_default=True, type=float,
    help=(
        "Minimum duplicate confidence threshold (0.0–1.0). "
        "Lower values surface more (possibly noisy) candidates."
    ),
)
@click.option(
    "--top", "top_n", default=20, show_default=True, type=int,
    help="Maximum number of duplicate groups to include in the report.",
)
@click.option(
    "--max-dashboards", default=100, show_default=True, type=int,
    help="Maximum dashboards to scan per DataHub search.",
)
@click.option(
    "--output", default="./reports", show_default=True,
    help="Directory to write the Markdown and JSON reports into.",
)
@click.option(
    "--no-llm", is_flag=True, default=False,
    help="Skip LLM consolidation notes even if an API key is configured.",
)
@click.option(
    "--with-lineage", is_flag=True, default=False,
    help=(
        "Fetch upstream dataset URNs per dashboard via DataHub lineage. "
        "Adds one MCP call per dashboard — slower but improves dataset-overlap detection."
    ),
)
@click.option(
    "--with-sql", is_flag=True, default=False,
    help=(
        "Fetch Superset SQL fingerprints for chart-level comparison. "
        "Requires SUPERSET_URL / SUPERSET_USER / SUPERSET_PASSWORD in config. "
        "Gracefully skipped if Superset is unreachable."
    ),
)
@click.pass_context
def dedupe_run(
    ctx: click.Context,
    platform: str | None,
    domain: str | None,
    min_confidence: float,
    top_n: int,
    max_dashboards: int,
    output: str,
    no_llm: bool,
    with_lineage: bool,
    with_sql: bool,
) -> None:
    """Detect duplicate dashboards and redundant metrics across your data platform.

    Compares dashboards pairwise across four signals:

    \b
      - Name similarity   (catches "Sales Overview v2", "Sales Overview (copy)")
      - Chart-set overlap (same Jaccard-similar set of embedded visualisations)
      - Dataset overlap   (same upstream data lineage, requires --with-lineage)
      - Glossary terms    (same business KPIs / metric definitions)

    Optionally adds SQL-level fingerprint comparison (--with-sql).
    Overlapping pairs are clustered into consolidation groups via union-find
    so transitive duplicates (A~B, B~C) surface as a single group {A,B,C}.

    \b
    Examples:

    \b
      # Fast scan — entity metadata only, all platforms
      meshops dedupe run --no-llm

    \b
      # Scan Superset dashboards with dataset lineage signals
      meshops dedupe run --platform superset --with-lineage --no-llm

    \b
      # Full scan with SQL fingerprints and LLM consolidation notes
      meshops dedupe run --platform superset --with-lineage --with-sql

    \b
      # Lower threshold to surface more candidates
      meshops dedupe run --min-confidence 0.2 --no-llm
    """
    cfg = load_config()

    from meshops_copilot.skills.duplicate_detector.skill import DuplicateDetectorSkill

    skill = DuplicateDetectorSkill(cfg, output_dir=output)
    result = skill.run(
        platform=platform,
        domain=domain,
        min_confidence=min_confidence,
        top_n=top_n,
        max_dashboards=max_dashboards,
        no_llm=no_llm,
        with_lineage=with_lineage,
        with_sql=with_sql,
    )

    if result.status.value == "failed":
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")
        raise SystemExit(1)
    elif result.status.value == "degraded":
        console.print(f"[yellow]Completed with warnings:[/yellow] {result.summary}")
        for err in result.errors[:5]:
            console.print(f"  [yellow]⚠[/yellow] {err}")
        if len(result.errors) > 5:
            console.print(f"  [yellow]… and {len(result.errors) - 5} more[/yellow]")
    else:
        console.print(f"[green]Done:[/green] {result.summary}")
