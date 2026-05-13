"""``meshops report`` command — generate the MeshOps Copilot Report."""

from __future__ import annotations

import click

from meshops_copilot.core.config import load_config


@click.group()
def report() -> None:
    """Generate a consolidated MeshOps Copilot Report."""


@report.command("run")
@click.option(
    "--results",
    "results_files",
    multiple=True,
    default=["stress_results.json"],
    show_default=True,
    metavar="FILE",
    help="Path to a stress results JSON file. Repeat to include multiple files.",
)
@click.option(
    "--output",
    default="./reports",
    show_default=True,
    metavar="DIR",
    help="Directory to write the Markdown report into.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help="Skip LLM analysis even if an API key is configured.",
)
@click.pass_context
def report_run(
    ctx: click.Context,
    results_files: tuple[str, ...],
    output: str,
    no_llm: bool,
) -> None:
    """Compile stress results into a Markdown report, optionally with LLM analysis.

    \b
    Examples:
      # Default — reads stress_results.json, writes reports/report.md
      meshops report run

      # Custom results file
      meshops report run --results reports/my_run.json

      # Multiple files
      meshops report run --results run1.json --results run2.json

      # Skip LLM even if OPENAI_API_KEY is set
      meshops report run --no-llm
    """
    cfg = load_config()

    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    skill = ReportWriterSkill(cfg, output_dir=output)
    result = skill.run(results_files=list(results_files), no_llm=no_llm)

    if result.errors:
        for err in result.errors:
            click.echo(f"ERROR: {err}", err=True)
        raise SystemExit(1)
