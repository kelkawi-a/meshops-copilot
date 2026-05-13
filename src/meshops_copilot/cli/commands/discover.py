"""``meshops discover`` command — DataHub discovery (stub)."""

from __future__ import annotations

import click


@click.group()
def discover() -> None:
    """Discover data products and golden reports via DataHub."""


@discover.command("run")
@click.pass_context
def discover_run(ctx: click.Context) -> None:
    """Search DataHub for data products, golden reports, and duplicate dashboards."""
    click.echo("discover: not yet implemented.")
