"""DataProductDiscoverySkill — rank datasets from DataHub as data product candidates.

Workflow
--------
1. Open a DataHub MCP session.
2. Search for datasets (optionally filtered by domain / platform).
3. Collect signals per dataset in parallel: usage stats, lineage, ownership,
   schema metadata.
4. Score and rank candidates using the weighted formula in ``scorer.py``.
5. Optionally call the LLM to generate one justification sentence per candidate
   (all in a single API call) plus a 2-3 sentence executive summary.
6. Write a Markdown report and a JSON artefact to the output directory.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.core.config import MeshOpsConfig
from meshops_copilot.core.llm import LLMClient
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.data_product_discovery.collectors import SignalCollector
from meshops_copilot.skills.data_product_discovery.markdown import (
    build_llm_prompt,
    build_summary_prompt,
    format_discovery_report,
)
from meshops_copilot.skills.data_product_discovery.models import DataProductCandidate
from meshops_copilot.skills.data_product_discovery.scorer import to_candidate

console = Console()

_SYSTEM_PROMPT = (
    "You are a senior data platform architect specialising in data mesh and "
    "data product design. "
    "Evaluate dataset metadata and explain concisely why specific datasets are "
    "strong candidates to be productised as data products. "
    "Be precise — reference actual numbers, team names, and domain names from "
    "the data provided."
)


class DataProductDiscoverySkill(BaseSkill):
    """Discover and rank data product candidates from DataHub metadata."""

    name = "data_product_discovery"

    def __init__(
        self,
        cfg: MeshOpsConfig,
        output_dir: str | Path = "./reports",
    ) -> None:
        self._cfg = cfg
        self._output_dir = Path(output_dir)

    def run(
        self,
        domain: str | None = None,
        platform: str | None = None,
        min_score: float = 0.0,
        top_n: int = 20,
        usage_window_days: int = 30,
        max_datasets: int = 50,
        no_llm: bool = False,
        with_usage: bool = False,
        with_lineage: bool = False,
        **kwargs,
    ) -> SkillResult:
        """Discover and rank data product candidates.

        Args:
            domain: Restrict search to a DataHub domain (e.g. ``"finance"``).
            platform: Restrict to a specific platform (e.g. ``"postgresql"``).
            min_score: Drop candidates below this score threshold (0.0–1.0).
            top_n: Cap the report at this many candidates.
            usage_window_days: Lookback window for usage statistics.
            max_datasets: Maximum datasets to scan per search (default 50).
            no_llm: Skip LLM justifications even if an API key is configured.
            with_usage: Fetch per-dataset query counts via ``get_dataset_queries``
                (adds one MCP call per dataset — slow for large result sets).
            with_lineage: Fetch downstream lineage per dataset
                (adds one MCP call per dataset — slow for large result sets).
        """
        from meshops_copilot.connectors.datahub_mcp import DataHubMCPConnector

        console.rule("[bold cyan]Data Product Candidate Discovery[/bold cyan]")

        # ── Connect ────────────────────────────────────────────────────────────
        try:
            connector = DataHubMCPConnector(
                gms_url=self._cfg.datahub.gms_url,
                token=self._cfg.datahub.token,
            )
        except Exception as exc:
            return self._failed([f"Could not create DataHub MCP connector: {exc}"])

        errors: list[str] = []
        all_signals = []

        try:
            with connector:
                console.print(
                    f"  Connected to DataHub MCP at "
                    f"[cyan]{self._cfg.datahub.gms_url}[/cyan]"
                )

                # ── Search ─────────────────────────────────────────────────────
                filter_parts = []
                if domain:
                    filter_parts.append(f"domain={domain}")
                if platform:
                    filter_parts.append(f"platform={platform}")
                filter_str = " ".join(filter_parts) or "all domains/platforms"
                console.print(
                    f"  Searching up to {max_datasets} datasets "
                    f"({filter_str})…"
                )
                datasets = connector.search_datasets(
                    domain=domain,
                    platform=platform,
                    count=max_datasets,
                )
                console.print(f"  Found [green]{len(datasets)}[/green] datasets.")

                if not datasets:
                    return self._ok(
                        summary="No datasets found matching the search criteria.",
                        details={"candidates": 0},
                    )

                # ── Collect signals ────────────────────────────────────────────
                urns = [
                    d.get("urn") or d.get("entityUrn", "")
                    for d in datasets
                    if d.get("urn") or d.get("entityUrn")
                ]
                console.print(
                    f"  Collecting signals for {len(urns)} datasets "
                    f"(entity metadata"
                    + (", usage" if with_usage else "")
                    + (", lineage" if with_lineage else "")
                    + ")…"
                )
                collector = SignalCollector(
                    connector=connector,
                    max_workers=8,
                    usage_window_days=usage_window_days,
                    collect_usage=with_usage,
                    collect_lineage=with_lineage,
                )
                all_signals = collector.collect_all(urns)

                # Surface per-dataset errors as degraded warnings.
                for s in all_signals:
                    for e in s.collection_errors:
                        errors.append(f"{s.urn}: {e}")

        except RuntimeError as exc:
            return self._failed([str(exc)])
        except Exception as exc:
            return self._failed([f"DataHub MCP error: {exc}"])

        # ── Score and rank ─────────────────────────────────────────────────────
        candidates = [to_candidate(s) for s in all_signals]
        candidates = [c for c in candidates if c.score >= min_score]
        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = candidates[:top_n]

        if not candidates and all_signals:
            max_seen = max((c.score for c in [to_candidate(s) for s in all_signals]), default=0)
            console.print(
                f"  [yellow]No candidates above {min_score:.0%} threshold. "
                f"Highest score seen: {max_seen:.0%}. "
                f"Re-run with --min-score 0 to see all datasets ranked, "
                f"or add --with-usage / --with-lineage for richer signals.[/yellow]"
            )

        console.print(
            f"  [green]{len(candidates)}[/green] candidates above "
            f"{min_score:.0%} threshold."
        )

        # ── LLM justifications + summary ───────────────────────────────────────
        llm_summary = ""
        if not no_llm and candidates:
            if self._cfg.llm.provider != "none" and self._cfg.llm.api_key:
                console.print(
                    f"  Calling {self._cfg.llm.provider} ({self._cfg.llm.model})"
                    f" for justifications…"
                )
                llm = LLMClient(self._cfg.llm)
                justifications = self._generate_justifications(llm, candidates)
                for c in candidates:
                    c.justification = justifications.get(c.urn, "")
                llm_summary = llm.complete(
                    prompt=build_summary_prompt(candidates),
                    system=_SYSTEM_PROMPT,
                )
                console.print("  [green]LLM analysis complete.[/green]")
            elif self._cfg.llm.provider != "none":
                console.print(
                    "  [dim]No LLM API key — skipping justifications. "
                    "Set OPENAI_API_KEY or OPENROUTER_API_KEY to enable.[/dim]"
                )

        # ── Write report ───────────────────────────────────────────────────────
        self._output_dir.mkdir(parents=True, exist_ok=True)
        query_params = {
            "domain": domain,
            "platform": platform,
            "min_score": min_score,
        }
        md = format_discovery_report(candidates, query_params, llm_summary)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        md_path = self._output_dir / f"data_products_{ts}.md"
        md_path.write_text(md)
        latest = self._output_dir / "data_products.md"
        latest.write_text(md)
        console.print(f"  Report  → [green]{md_path}[/green]")
        console.print(f"  Latest  → [green]{latest}[/green]")

        json_path = self._output_dir / f"data_products_{ts}.json"
        json_path.write_text(json.dumps(self._to_json(candidates), indent=2))

        self._print_table(candidates[:10])

        summary = (
            f"Discovered {len(candidates)} data product candidates "
            f"(scanned {len(all_signals)} datasets). "
            f"Report: {md_path}"
        )
        if errors:
            return self._degraded(
                summary=summary,
                errors=errors,
                details={"candidates": len(candidates), "output": str(md_path)},
            )
        return self._ok(
            summary=summary,
            details={"candidates": len(candidates), "output": str(md_path)},
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _generate_justifications(
        self,
        llm: LLMClient,
        candidates: list[DataProductCandidate],
    ) -> dict[str, str]:
        """Generate one justification per candidate in a single LLM call."""
        raw = llm.complete(prompt=build_llm_prompt(candidates), system=_SYSTEM_PROMPT)
        if not raw:
            return {}
        try:
            text = raw.strip()
            # Strip markdown code fences the LLM sometimes wraps JSON in.
            if text.startswith("```"):
                text = "\n".join(text.split("\n")[1:]).rstrip("`").strip()
            items = json.loads(text)
            return {
                item["urn"]: item["justification"]
                for item in items
                if "urn" in item and "justification" in item
            }
        except Exception as exc:
            console.print(f"  [yellow]Could not parse justifications JSON: {exc}[/yellow]")
            return {}

    def _print_table(self, candidates: list[DataProductCandidate]) -> None:
        t = Table(title="Top Data Product Candidates", show_lines=False)
        t.add_column("#", style="dim", width=3)
        t.add_column("Dataset", style="cyan")
        t.add_column("Platform", style="dim")
        t.add_column("Score", justify="right")
        t.add_column("Queries", justify="right")
        t.add_column("Users", justify="right")
        t.add_column("Dashboards", justify="right")
        t.add_column("Owner")
        for i, c in enumerate(candidates, 1):
            s = c.signals
            owner = (
                s.owners[0] if s.owners
                else (s.owner_teams[0] if s.owner_teams else "—")
            )
            t.add_row(
                str(i),
                c.display_name,
                c.platform or "—",
                f"{c.score:.0%}",
                str(s.query_count_30d),
                str(s.unique_users_30d),
                str(s.downstream_dashboard_count),
                owner,
            )
        console.print(t)

    @staticmethod
    def _to_json(candidates: list[DataProductCandidate]) -> list[dict]:
        out = []
        for c in candidates:
            s = c.signals
            out.append({
                "urn": c.urn,
                "name": c.name,
                "platform": c.platform,
                "score": c.score,
                "score_breakdown": c.score_breakdown,
                "justification": c.justification,
                "signals": {
                    "query_count_30d": s.query_count_30d,
                    "unique_users_30d": s.unique_users_30d,
                    "downstream_dashboard_count": s.downstream_dashboard_count,
                    "downstream_dataset_count": s.downstream_dataset_count,
                    "downstream_chart_count": s.downstream_chart_count,
                    "owners": s.owners,
                    "owner_teams": s.owner_teams,
                    "has_owner": s.has_owner,
                    "has_description": s.has_description,
                    "schema_field_count": s.schema_field_count,
                    "tags": s.tags,
                    "domain": s.domain,
                    "description": s.description,
                },
            })
        return out
