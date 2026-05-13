"""GrafanaDiagnosticsSkill — analyse Prometheus metrics and Loki logs via MCP."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

from meshops_copilot.connectors.grafana_mcp import GrafanaMCPClient
from meshops_copilot.core.config import GrafanaConfig, PrometheusConfig, LLMConfig
from meshops_copilot.core.llm import LLMClient
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill
from meshops_copilot.skills.grafana_diagnostics.bottleneck_detector import (
    detect_bottlenecks,
    recommend_actions,
)
from meshops_copilot.skills.grafana_diagnostics.metric_analyzer import MetricAnalyzer
from meshops_copilot.skills.grafana_diagnostics.log_analyzer import LogAnalyzer
from meshops_copilot.skills.grafana_diagnostics.models import DiagnosticsReport, LogResult

logger = logging.getLogger(__name__)
console = Console()


# ── System prompt for query interpretation ─────────────────────────────────────

_INTERPRET_SYSTEM = """\
You are a Prometheus/Grafana/Loki diagnostics assistant embedded inside the \
MeshOps Copilot CLI tool.  The user has asked a natural-language question about \
their infrastructure.  Your job is to extract structured parameters so the tool \
can run the right queries against Prometheus (metrics) and Loki (logs).

Respond ONLY with a JSON object (no markdown fences, no explanation) with these fields:
- "start": ISO 8601 timestamp or relative like "now-1h" (required)
- "end": ISO 8601 timestamp or "now" (required)
- "namespace": Kubernetes namespace regex to filter on, or ".+" for all (default ".+")
- "component": the application/service the user is asking about (e.g. "superset", "trino", "grafana"). Empty string if not specified.
- "metric_prefixes": list of Prometheus metric name prefixes the user mentions (e.g. ["superset_", "http_"]). Extract these from hints like "metrics start with superset_*" or "superset_ metrics". Empty list if none mentioned.
- "categories": list of metric categories to analyse, subset of ["cpu", "memory", "latency", "errors", "throughput", "queue", "network", "disk"] (default all)
- "include_logs": boolean — whether to also query Loki logs (default true)
- "summary_of_question": one sentence restating what the user wants to know

If the user says a time like "10:30" without a date, assume today in UTC.
If they say "last hour" or similar, use relative offsets from now.
If the user mentions errors, slowness, exceptions, or logs, set include_logs to true.
If the user mentions metric prefixes (e.g. "superset_*", "metrics start with http_"), extract the prefix without wildcards into metric_prefixes.
"""

_SYNTHESIS_SYSTEM = """\
You are MeshOps Copilot reporting on a diagnostics run that includes both \
Prometheus metrics and Loki log analysis.  Synthesise the results into a clear, \
concise answer to the user's original question.

Rules:
- Cite specific metric values and pod/container names
- Reference specific log lines or error patterns when relevant
- Rank issues by severity (Critical > High > Medium)
- Suggest concrete actions
- Keep it under 500 words
- Use markdown formatting for readability
"""


class GrafanaDiagnosticsSkill(BaseSkill):
    """Analyse Prometheus metrics and Loki logs via the Grafana MCP server."""

    name = "grafana_diagnostics"

    def __init__(
        self,
        prometheus_cfg: PrometheusConfig,
        grafana_cfg: GrafanaConfig | None = None,
        llm_cfg: LLMConfig | None = None,
        output_file: str | None = None,
    ) -> None:
        self._prom_cfg = prometheus_cfg
        self._grafana_cfg = grafana_cfg
        self._llm_cfg = llm_cfg
        self._output_file = output_file

    def run(self, query: str | None = None, **kwargs) -> SkillResult:
        """Execute the diagnostics skill.

        Parameters
        ----------
        query : str, optional
            Natural-language question (e.g. "why was superset slow between
            10:30 and 11:00").  If omitted, runs a full diagnostic sweep.
        """
        # ── Interpret the user's query ──────────────────────────────────────────
        intent = self._interpret_query(query)
        component = intent.get("component", "")
        namespace = intent.get("namespace", ".+")
        metric_prefixes = intent.get("metric_prefixes", [])
        categories = intent.get("categories", [
            "cpu", "memory", "latency", "errors", "throughput", "queue", "network", "disk",
        ])
        include_logs = intent.get("include_logs", True)
        window_minutes = self._compute_window(intent)

        console.print(f"\n[bold]Diagnosing[/bold]", end="")
        if component:
            console.print(f" [cyan]{component}[/cyan]", end="")
        console.print(f" (window: {window_minutes}m, namespace: {namespace})")
        if intent.get("summary_of_question"):
            console.print(f"  [dim]{intent['summary_of_question']}[/dim]\n")
        if metric_prefixes:
            console.print(f"  [dim]Metric prefixes: {', '.join(metric_prefixes)}[/dim]")

        report = DiagnosticsReport()

        # ── Start MCP server ────────────────────────────────────────────────────
        mcp = self._build_mcp_client()
        if mcp is None:
            return self._failed(
                ["No Grafana configuration available. Set GRAFANA_URL and GRAFANA_TOKEN."],
            )

        try:
            console.print("  [bold]Starting Grafana MCP server...[/bold]")
            mcp.start()

            # Compute time bounds for queries
            start_time = intent.get("start", "now-1h")
            end_time = intent.get("end", "now")

            # ── Metrics analysis ────────────────────────────────────────────────
            console.print("  [bold]Discovering metrics...[/bold]")
            analyzer = MetricAnalyzer(
                mcp=mcp,
                namespace=namespace,
                component=component,
                window_minutes=window_minutes,
                metric_prefixes=metric_prefixes,
                start_time=start_time,
                end_time=end_time,
            )

            report.discovered_metrics = analyzer.discover()
            if report.discovered_metrics:
                counts = ", ".join(f"{k}: {v}" for k, v in report.discovered_metrics.items() if v)
                console.print(f"  [dim]Found: {counts}[/dim]")
            else:
                console.print("  [yellow]No component-specific metrics found — using generic queries[/yellow]")

            # Query phase
            console.print("  [bold]Querying metrics...[/bold]")
            report.metric_results = analyzer.analyse_all(categories=categories)

            for cat, result in report.metric_results.items():
                if result.top_consumers:
                    console.print(f"    {cat}: {result.summary}")
                elif result.errors:
                    console.print(f"    {cat}: [yellow]no data[/yellow] ({len(result.errors)} errors)")

            # ── Log analysis ────────────────────────────────────────────────────
            if include_logs:
                console.print("  [bold]Querying logs...[/bold]")
                try:
                    log_analyzer = LogAnalyzer(
                        mcp=mcp,
                        component=component,
                        namespace=namespace,
                        window_minutes=window_minutes,
                    )
                    report.logs = log_analyzer.analyse()
                    if report.logs.summary:
                        console.print(f"    logs: {report.logs.summary}")
                except RuntimeError as exc:
                    console.print(f"  [yellow]Loki not available: {exc}[/yellow]")

            # ── Detect bottlenecks ──────────────────────────────────────────────
            report.bottlenecks = detect_bottlenecks(
                metric_results=report.metric_results,
                logs=report.logs if report.logs.error_lines or report.logs.error_rate > 0 else None,
            )
            report.actions = recommend_actions(report.bottlenecks)

            # Collect errors
            for cat_result in report.metric_results.values():
                report.errors.extend(cat_result.errors)
            report.errors.extend(report.logs.errors)

        finally:
            mcp.stop()

        # ── Display ─────────────────────────────────────────────────────────────
        self._print_report(report)

        # ── LLM synthesis ───────────────────────────────────────────────────────
        answer = ""
        if query and self._llm_cfg and self._llm_cfg.provider != "none":
            answer = self._synthesise(query, report)
            if answer:
                console.print("\n[bold green]Analysis[/bold green]\n")
                console.print(answer)

        # ── Save output ─────────────────────────────────────────────────────────
        output_data = report.to_dict()
        if answer:
            output_data["llm_answer"] = answer
        output_data["query"] = query or ""
        output_data["intent"] = intent

        if self._output_file:
            Path(self._output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self._output_file, "w") as f:
                json.dump(output_data, f, indent=2, default=str)
            console.print(f"\n[dim]Results written to {self._output_file}[/dim]")

        # ── Return ──────────────────────────────────────────────────────────────
        if report.errors and not report.bottlenecks and not report.logs.error_lines:
            return self._failed(report.errors, details=output_data)

        summary = answer or self._build_text_summary(report)
        return self._ok(summary=summary, details=output_data)

    # ── MCP client setup ───────────────────────────────────────────────────────

    def _build_mcp_client(self) -> GrafanaMCPClient | None:
        """Build a GrafanaMCPClient from config."""
        if not self._grafana_cfg or not self._grafana_cfg.url or not self._grafana_cfg.token:
            return None

        console.print(f"  [dim]Grafana: {self._grafana_cfg.url}[/dim]")
        return GrafanaMCPClient(
            grafana_url=self._grafana_cfg.url,
            grafana_token=self._grafana_cfg.token,
        )

    # ── Query interpretation ───────────────────────────────────────────────────

    def _interpret_query(self, query: str | None) -> dict:
        """Use LLM to parse the user query into structured params."""
        if not query:
            return {
                "start": "now-1h",
                "end": "now",
                "namespace": ".+",
                "component": "",
                "metric_prefixes": [],
                "categories": [
                    "cpu", "memory", "latency", "errors",
                    "throughput", "queue", "network", "disk",
                ],
                "include_logs": True,
                "summary_of_question": "Full diagnostic sweep",
            }

        if not self._llm_cfg or self._llm_cfg.provider == "none":
            return self._interpret_without_llm(query)

        llm = LLMClient(self._llm_cfg)
        raw = llm.complete(
            prompt=f"User question: {query}\n\nToday's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            system=_INTERPRET_SYSTEM,
        )

        if not raw:
            return self._interpret_without_llm(query)

        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[1:])
            if cleaned.endswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[:-1])
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse LLM intent: %s", raw[:200])
            return self._interpret_without_llm(query)

    def _interpret_without_llm(self, query: str) -> dict:
        """Keyword-based fallback."""
        q = query.lower()

        # Extract component
        component = ""
        for svc in ("superset", "trino", "grafana", "prometheus", "datahub", "loki"):
            if svc in q:
                component = svc
                break

        # Extract metric prefixes (e.g. "superset_*", "start with http_")
        import re
        metric_prefixes: list[str] = []
        for m in re.finditer(r'(\w+_)\*?', q):
            prefix = m.group(1)
            if len(prefix) > 2 and not prefix.startswith(("the_", "a_", "is_")):
                metric_prefixes.append(prefix)

        # Guess categories
        categories = []
        if any(w in q for w in ("cpu", "compute", "throttl")):
            categories.append("cpu")
        if any(w in q for w in ("memory", "heap", "oom", "gc", "ram")):
            categories.append("memory")
        if any(w in q for w in ("slow", "latency", "duration", "response time", "p95", "p99")):
            categories.append("latency")
        if any(w in q for w in ("error", "exception", "fail", "5xx", "500")):
            categories.append("errors")
        if any(w in q for w in ("throughput", "request", "qps", "rps")):
            categories.append("throughput")
        if any(w in q for w in ("queue", "pending", "inflight")):
            categories.append("queue")
        if any(w in q for w in ("network", "bandwidth")):
            categories.append("network")
        if any(w in q for w in ("disk", "io", "iops", "spill")):
            categories.append("disk")

        if not categories:
            categories = [
                "cpu", "memory", "latency", "errors",
                "throughput", "queue", "network", "disk",
            ]

        # Should we include logs?
        include_logs = any(w in q for w in (
            "log", "error", "exception", "slow", "timeout", "why",
            "what happened", "debug", "trace",
        )) or not categories

        namespace = f".*{component}.*" if component else ".+"

        return {
            "start": "now-1h",
            "end": "now",
            "namespace": namespace,
            "component": component,
            "metric_prefixes": metric_prefixes,
            "categories": categories,
            "include_logs": include_logs,
            "summary_of_question": query,
        }

    # ── LLM synthesis ──────────────────────────────────────────────────────────

    def _synthesise(self, query: str, report: DiagnosticsReport) -> str:
        """Use LLM to synthesise results into a human-readable answer."""
        if not self._llm_cfg:
            return ""

        llm = LLMClient(self._llm_cfg)

        # Build a compact summary — include log excerpts
        summary_data: dict = {
            "discovered_metrics": report.discovered_metrics,
            "metric_categories": {},
            "bottlenecks": [],
            "log_summary": {},
        }

        for cat, result in report.metric_results.items():
            if result.top_consumers:
                summary_data["metric_categories"][cat] = {
                    "summary": result.summary,
                    "top_values": [
                        {"pod": s.labels.get("pod", "?"), "metric": s.metric, "value": s.value}
                        for s in result.top_consumers[:3]
                    ],
                    "queries_used": result.raw_queries,
                }

        for b in report.bottlenecks:
            summary_data["bottlenecks"].append({
                "rank": b.rank, "component": b.component,
                "severity": b.severity.value, "root_cause": b.root_cause,
                "source": b.source,
            })

        if report.logs.error_lines or report.logs.warning_lines:
            summary_data["log_summary"] = {
                "error_count": len(report.logs.error_lines),
                "warning_count": len(report.logs.warning_lines),
                "error_rate_per_sec": report.logs.error_rate,
                "sample_errors": [e.line[:200] for e in report.logs.error_lines[:5]],
                "sample_warnings": [w.line[:200] for w in report.logs.warning_lines[:3]],
            }

        if report.logs.patterns:
            summary_data["log_patterns"] = report.logs.patterns[:10]

        data_str = json.dumps(summary_data, indent=2, default=str)
        if len(data_str) > 10000:
            data_str = data_str[:10000] + "\n... (truncated)"

        prompt = (
            f"Original user question: {query}\n\n"
            f"Diagnostics data:\n{data_str}"
        )
        return llm.complete(prompt=prompt, system=_SYNTHESIS_SYSTEM)

    # ── Time window ────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_window(intent: dict) -> int:
        start_str = intent.get("start", "now-1h")
        end_str = intent.get("end", "now")
        now = datetime.now(timezone.utc)
        start = _parse_time(start_str, now)
        end = _parse_time(end_str, now)
        if start and end:
            minutes = max(int((end - start).total_seconds() / 60), 5)
            return min(minutes, 1440)
        return 60

    # ── Display ────────────────────────────────────────────────────────────────

    def _print_report(self, report: DiagnosticsReport) -> None:
        console.print()

        # Bottleneck table
        if report.bottlenecks:
            table = Table(title="Bottlenecks", show_lines=True)
            table.add_column("Rank", style="bold", width=4)
            table.add_column("Component")
            table.add_column("Severity")
            table.add_column("Source")
            table.add_column("Root Cause")

            severity_colors = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "dim"}
            for b in report.bottlenecks:
                color = severity_colors.get(b.severity.value, "white")
                table.add_row(
                    str(b.rank),
                    b.component,
                    f"[{color}]{b.severity.value.upper()}[/{color}]",
                    b.source or "metrics",
                    b.root_cause,
                )
            console.print(table)
        else:
            console.print("  [dim]No bottlenecks detected above thresholds[/dim]")

        # Log highlights
        if report.logs.error_lines:
            console.print(f"\n  [bold]Log errors[/bold] ({len(report.logs.error_lines)} found)")
            for entry in report.logs.error_lines[:5]:
                console.print(f"    [red]{entry.line[:120]}[/red]")
            if len(report.logs.error_lines) > 5:
                console.print(f"    [dim]... and {len(report.logs.error_lines) - 5} more[/dim]")

        if report.logs.warning_lines and not report.logs.error_lines:
            console.print(f"\n  [bold]Log warnings[/bold] ({len(report.logs.warning_lines)} found)")
            for entry in report.logs.warning_lines[:3]:
                console.print(f"    [yellow]{entry.line[:120]}[/yellow]")

        # Log patterns
        if report.logs.patterns:
            console.print(f"\n  [bold]Log patterns[/bold] ({len(report.logs.patterns)} detected)")
            for p in report.logs.patterns[:5]:
                if isinstance(p, dict):
                    console.print(f"    [dim]{p.get('pattern', '?')} ({p.get('totalCount', '?')} occurrences)[/dim]")

        # Actions
        if report.actions:
            console.print("\n  [bold]Recommended Actions[/bold]")
            for i, action in enumerate(report.actions, 1):
                console.print(f"    {i}. {action.action}")
                console.print(f"       [dim]{action.reason} | Effort: {action.effort}[/dim]")

    @staticmethod
    def _build_text_summary(report: DiagnosticsReport) -> str:
        parts = []
        if report.bottlenecks:
            top = report.bottlenecks[0]
            parts.append(f"Top bottleneck: {top.component} ({top.severity.value}) — {top.root_cause}")
        if report.logs.error_lines:
            parts.append(f"{len(report.logs.error_lines)} error log entries found.")
        if not parts:
            parts.append("No significant issues detected.")
        if report.errors:
            parts.append(f"{len(report.errors)} query errors encountered.")
        return " ".join(parts)


# ── Time parsing ───────────────────────────────────────────────────────────────

def _parse_time(time_str: str, now: datetime) -> datetime | None:
    if not time_str:
        return None
    time_str = time_str.strip()
    if time_str == "now":
        return now
    if time_str.startswith("now-"):
        suffix = time_str[4:]
        amount = ""
        unit = ""
        for ch in suffix:
            if ch.isdigit():
                amount += ch
            else:
                unit += ch
        if amount:
            n = int(amount)
            if "h" in unit:
                return now - timedelta(hours=n)
            elif "m" in unit:
                return now - timedelta(minutes=n)
            elif "d" in unit:
                return now - timedelta(days=n)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(time_str, fmt)
            if fmt == "%H:%M":
                parsed = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            elif parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None
