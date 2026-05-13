"""``meshops discover`` command — data product candidate discovery via DataHub MCP."""

from __future__ import annotations

import click
from rich.console import Console

from meshops_copilot.core.config import load_config

console = Console()


@click.group()
def discover() -> None:
    """Discover data products and golden reports via DataHub."""


@discover.command("run")
@click.option(
    "--domain", default=None,
    help="Restrict to a DataHub domain (e.g. finance, sales).",
)
@click.option(
    "--platform", default=None,
    help="Restrict to a data platform (e.g. postgresql, snowflake, bigquery).",
)
@click.option(
    "--min-score", default=0.2, show_default=True, type=float,
    help="Minimum candidate score threshold (0.0–1.0).",
)
@click.option(
    "--top", "top_n", default=20, show_default=True, type=int,
    help="Maximum number of candidates to include in the report.",
)
@click.option(
    "--max-datasets", default=50, show_default=True, type=int,
    help="Maximum datasets to scan per DataHub search.",
)
@click.option(
    "--output", default="./reports", show_default=True,
    help="Directory to write the Markdown and JSON reports into.",
)
@click.option(
    "--no-llm", is_flag=True, default=False,
    help="Skip LLM justifications even if an API key is configured.",
)
@click.option(
    "--with-usage", is_flag=True, default=False,
    help=(
        "Fetch per-dataset query counts via get_dataset_queries. "
        "Adds one MCP call per dataset — slower but improves scoring."
    ),
)
@click.option(
    "--with-lineage", is_flag=True, default=False,
    help=(
        "Fetch downstream lineage per dataset. "
        "Adds one MCP call per dataset — slower but improves scoring."
    ),
)
@click.pass_context
def discover_run(
    ctx: click.Context,
    domain: str | None,
    platform: str | None,
    min_score: float,
    top_n: int,
    max_datasets: int,
    output: str,
    no_llm: bool,
    with_usage: bool,
    with_lineage: bool,
) -> None:
    """Score and rank DataHub datasets as data product candidates.

    Entity metadata is fetched in a single batched MCP call.
    Usage and lineage signals require --with-usage / --with-lineage and add
    one extra MCP call per dataset.

    \b
    Signals collected:
      - Ownership (individual owners and teams)
      - Schema field count and description presence
      - Tags and domain membership
      - Query counts (--with-usage)
      - Downstream dashboard / dataset count (--with-lineage)

    Examples:

    \b
      # Fast scan — entity metadata only
      meshops discover run --no-llm

    \b
      # Include usage and lineage signals (slower)
      meshops discover run --no-llm --with-usage --with-lineage

    \b
      # Filter to the finance domain, return top 10
      meshops discover run --domain finance --top 10 --no-llm

    \b
      # Filter by platform, skip LLM justifications
      meshops discover run --platform postgresql --no-llm
    """
    cfg = load_config()

    from meshops_copilot.skills.data_product_discovery.skill import (
        DataProductDiscoverySkill,
    )

    skill = DataProductDiscoverySkill(cfg, output_dir=output)
    result = skill.run(
        domain=domain,
        platform=platform,
        min_score=min_score,
        top_n=top_n,
        max_datasets=max_datasets,
        no_llm=no_llm,
        with_usage=with_usage,
        with_lineage=with_lineage,
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
