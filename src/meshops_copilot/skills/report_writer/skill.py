"""ReportWriterSkill — compile stress results into a Markdown report.

Usage
-----
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill
    from meshops_copilot.core.config import load_config

    cfg = load_config()
    skill = ReportWriterSkill(cfg, output_dir="./reports")
    result = skill.run(results_files=["stress_results.json"])

The skill:
  1. Loads each results JSON file.
  2. Formats the metrics into a structured Markdown report.
  3. Optionally calls the configured LLM for a narrative analysis section.
  4. Writes ``<output_dir>/report.md`` (and ``report_data.json`` for auditing).

LLM is silently skipped when ``LLM_PROVIDER=none`` or no API key is set.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rich.console import Console

from meshops_copilot.core.config import MeshOpsConfig
from meshops_copilot.core.llm import LLMClient
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.report_writer.markdown import (
    build_llm_prompt,
    format_stress_report,
)

console = Console()

_SYSTEM_PROMPT = (
    "You are a senior Trino infrastructure engineer specialising in production "
    "deployment tuning. "
    "Analyse stress-test results and recommend concrete configuration changes to "
    "the Trino deployment — config.properties, jvm.config, node.properties, and "
    "connector-specific config files. "
    "Always cite the exact property name and a realistic value. "
    "Every recommendation must be grounded in the numbers provided; "
    "do not give advice that cannot be justified by the data."
)


class ReportWriterSkill(BaseSkill):
    """Compile skill results into a Markdown + JSON report."""

    name = "report_writer"

    def __init__(self, cfg: MeshOpsConfig, output_dir: str | Path = "./reports") -> None:
        self._cfg = cfg
        self._output_dir = Path(output_dir)

    def run(
        self,
        results_files: list[str] | None = None,
        no_llm: bool = False,
        **kwargs,
    ) -> SkillResult:
        """Generate the report.

        Args:
            results_files: Paths to JSON result files produced by the stress
                skill.  Defaults to ``["stress_results.json"]``.
            no_llm: Skip LLM analysis even if an API key is configured.
        """
        files = [Path(f) for f in (results_files or ["stress_results.json"])]
        missing = [f for f in files if not f.exists()]
        if missing:
            return self._failed([f"Results file not found: {f}" for f in missing])

        # ── Load results ───────────────────────────────────────────────────────
        all_data: list[dict] = []
        for f in files:
            try:
                all_data.append(json.loads(f.read_text()))
            except Exception as exc:
                return self._failed([f"Could not parse {f}: {exc}"])

        self._output_dir.mkdir(parents=True, exist_ok=True)

        reports: list[str] = []
        errors: list[str] = []

        for i, data in enumerate(all_data):
            label = files[i].stem
            console.rule(f"[bold cyan]Report — {label}[/bold cyan]")

            # ── LLM analysis ──────────────────────────────────────────────────
            narrative = ""
            if not no_llm and self._cfg.llm.provider != "none" and self._cfg.llm.api_key:
                console.print(
                    f"  [yellow]Calling {self._cfg.llm.provider} "
                    f"({self._cfg.llm.model}) for analysis…[/yellow]"
                )
                try:
                    llm = LLMClient(self._cfg.llm)
                    narrative = llm.complete(
                        prompt=build_llm_prompt(data),
                        system=_SYSTEM_PROMPT,
                    )
                    if narrative:
                        console.print("  [green]LLM analysis complete.[/green]")
                    else:
                        console.print(
                            "  [yellow]LLM returned no content — check the error above "
                            "or run with --no-llm to skip.[/yellow]"
                        )
                except Exception as exc:
                    msg = f"LLM call failed for {label}: {exc}"
                    console.print(f"  [yellow]{msg}[/yellow]")
                    errors.append(msg)
            elif not no_llm and self._cfg.llm.provider != "none":
                console.print(
                    "  [dim]No LLM API key configured — skipping analysis. "
                    "Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env to enable.[/dim]"
                )

            # ── Render Markdown ───────────────────────────────────────────────
            md = format_stress_report(data, llm_narrative=narrative)
            reports.append(md)

            # ── Write files ───────────────────────────────────────────────────
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            md_path = self._output_dir / f"{label}_{ts}.md"
            md_path.write_text(md)
            console.print(f"  Report  → [green]{md_path}[/green]")

            # Also write a symlink / copy as report.md for easy access
            latest = self._output_dir / "report.md"
            latest.write_text(md)
            console.print(f"  Latest  → [green]{latest}[/green]")

        summary = f"Generated {len(reports)} report(s) in {self._output_dir}."
        if errors:
            return self._degraded(
                summary=summary,
                errors=errors,
                details={"output_dir": str(self._output_dir)},
            )
        return self._ok(
            summary=summary,
            details={"output_dir": str(self._output_dir)},
        )
