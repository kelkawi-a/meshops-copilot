"""``meshops stress`` command group."""

from __future__ import annotations

import click
import yaml

from meshops_copilot.core.config import load_config


def _load_scenario_raw(path: str) -> dict:
    """Return the raw scenario dict without triggering full skill machinery."""
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


@click.group()
def stress() -> None:
    """Run Trino or Superset stress tests."""


@stress.command("run")
@click.option("--scenario", required=True, metavar="PATH", help="Path to scenario YAML.")
@click.option("--output", default=None, metavar="PATH", help="Write JSON results to this file.")
@click.option("--url", default=None, metavar="URL", help="Trino coordinator URL (overrides config / TRINO_URL).")
@click.option("--user", default=None, metavar="USER", help="Trino username (overrides config / TRINO_USER).")
@click.option("--password", default=None, metavar="PASSWORD", envvar="TRINO_PASSWORD",
              help="Trino password for Basic Auth (or set TRINO_PASSWORD env var).")
@click.option("--no-verify-ssl", is_flag=True, default=False,
              help="Disable TLS certificate verification (useful for self-signed certs).")
@click.pass_context
def stress_run(
    ctx: click.Context,
    scenario: str,
    output: str | None,
    url: str | None,
    user: str | None,
    password: str | None,
    no_verify_ssl: bool,
) -> None:
    """Execute a stress-test scenario against Trino."""
    # Load the scenario YAML first so its trino: block can feed into load_config()
    # as the lowest-priority layer (env vars and --config YAML still win over it).
    scenario_raw = _load_scenario_raw(scenario)
    cfg = load_config(ctx.obj.get("config_path"), scenario_defaults=scenario_raw)

    # CLI flags take highest priority — overlay onto loaded config.
    if url:
        cfg.trino.url = url
    if user:
        cfg.trino.user = user
    if password:
        cfg.trino.password = password
    if no_verify_ssl:
        cfg.trino.verify_ssl = False

    from meshops_copilot.skills.trino_stress.skill import TrinoStressSkill

    skill = TrinoStressSkill(cfg.trino, output_file=output)
    result = skill.run(scenario_path=scenario)

    if result.errors:
        for err in result.errors:
            click.echo(f"ERROR: {err}", err=True)
        raise SystemExit(1)


@stress.command("superset")
@click.option("--scenario", required=True, metavar="PATH", help="Path to scenario YAML.")
@click.option("--output", default=None, metavar="PATH", help="Write JSON results to this file.")
@click.pass_context
def stress_superset(ctx: click.Context, scenario: str, output: str | None) -> None:
    """Execute a stress-test scenario against Superset."""
    scenario_raw = _load_scenario_raw(scenario)
    cfg = load_config(ctx.obj.get("config_path"), scenario_defaults=scenario_raw)

    from meshops_copilot.skills.superset_stress.skill import SupersetStressSkill

    skill = SupersetStressSkill(cfg.superset, output_file=output)
    result = skill.run(scenario_path=scenario)

    if result.errors:
        for err in result.errors:
            click.echo(f"ERROR: {err}", err=True)
        raise SystemExit(1)
