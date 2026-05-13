"""Root Click application — registers all sub-command groups."""

from __future__ import annotations

import click

from meshops_copilot.cli.commands.stress import stress
from meshops_copilot.cli.commands.diagnose import diagnose
from meshops_copilot.cli.commands.discover import discover
from meshops_copilot.cli.commands.report import report
from meshops_copilot.core.logging import setup_logging


@click.group()
@click.option("--config", default=None, metavar="PATH", help="Path to config YAML.")
@click.option("--log-level", default="INFO", show_default=True, help="Logging verbosity.")
@click.pass_context
def cli(ctx: click.Context, config: str | None, log_level: str) -> None:
    """MeshOps Copilot — AI-assisted data mesh operations."""
    setup_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


cli.add_command(stress)
cli.add_command(diagnose)
cli.add_command(discover)
cli.add_command(report)
