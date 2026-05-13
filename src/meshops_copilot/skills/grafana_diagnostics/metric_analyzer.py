"""grafana_diagnostics.metric_analyzer — discover metrics via MCP, then query."""

from __future__ import annotations

import logging

from meshops_copilot.connectors.grafana_mcp import GrafanaMCPClient
from meshops_copilot.skills.grafana_diagnostics.models import CategoryResult, MetricSample
from meshops_copilot.skills.grafana_diagnostics.queries import (
    ALL_CATEGORIES,
    FALLBACK_QUERIES,
    PromQLQuery,
    build_queries_from_discovered,
    discover_and_classify,
)

logger = logging.getLogger(__name__)


class MetricAnalyzer:
    """Discovers available metrics for a component, then runs targeted queries.

    Uses the Grafana MCP server for all Prometheus interactions (discovery,
    queries, histograms).  The MCP server handles datasource proxy routing,
    authentication, and Mimir/Cortex compatibility internally.
    """

    def __init__(
        self,
        mcp: GrafanaMCPClient,
        namespace: str = ".+",
        component: str = "",
        window_minutes: int = 60,
        metric_prefixes: list[str] | None = None,
        start_time: str = "now-1h",
        end_time: str = "now",
    ) -> None:
        self.mcp = mcp
        self.namespace = namespace
        self.component = component
        self.window_minutes = window_minutes
        self.metric_prefixes = metric_prefixes or []
        self.start_time = start_time
        self.end_time = end_time
        self._discovered_queries: list[PromQLQuery] | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def discover(self) -> dict[str, int]:
        """Discover available metrics and return category → count mapping.

        Call this first to see what's available, then call ``analyse_all()``.
        """
        all_names = self._fetch_metric_names()
        logger.info("Discovered %d total metric names via MCP", len(all_names))

        classified = discover_and_classify(all_names, component_filter=self.component)
        self._discovered_queries = build_queries_from_discovered(
            classified,
            namespace=self.namespace,
            component=self.component,
        )

        summary = {cat: len(metrics) for cat, metrics in classified.items() if metrics}
        logger.info("Classified metrics by category: %s", summary)
        logger.info("Generated %d targeted queries", len(self._discovered_queries))
        return summary

    def analyse_all(self, categories: list[str] | None = None) -> dict[str, CategoryResult]:
        """Run all discovered queries (or fallbacks) and return results by category."""
        if self._discovered_queries is None:
            self.discover()

        queries = self._discovered_queries or FALLBACK_QUERIES
        if not queries:
            logger.warning("No metrics discovered and no fallbacks — using static queries")
            queries = FALLBACK_QUERIES

        target_categories = set(categories) if categories else set(ALL_CATEGORIES)

        # Group queries by category
        by_cat: dict[str, list[PromQLQuery]] = {cat: [] for cat in target_categories}
        for q in queries:
            if q.category in target_categories:
                by_cat[q.category].append(q)

        results: dict[str, CategoryResult] = {}
        for cat in target_categories:
            cat_queries = by_cat.get(cat, [])
            results[cat] = self._run_category(cat, cat_queries)

        return results

    def analyse_category(self, category: str) -> CategoryResult:
        """Run queries for a single category."""
        result = self.analyse_all(categories=[category])
        return result.get(category, CategoryResult(category=category))

    # ── Internal ───────────────────────────────────────────────────────────────

    def _fetch_metric_names(self) -> list[str]:
        """Fetch available metric names via MCP.

        Uses a tiered strategy:

        1. **Prefix-based PromQL discovery** — if metric prefixes like
           ``["superset_"]`` are provided, use ``query_prometheus`` with
           ``count by (__name__) ({__name__=~"prefix.*"})`` which Mimir
           handles much better than the label values endpoint.
        2. **Component-based PromQL discovery** — if a component is set,
           query ``count by (__name__) ({pod=~".*component.*"})``.
        3. **MCP list_prometheus_metric_names** — paginated listing with
           regex filter (falls back to label values endpoint).
        4. **Bare fallback** — empty list, triggering fallback queries.
        """
        all_names: list[str] = []

        # ── Strategy 1: prefix-based PromQL count ──────────────────────────
        if self.metric_prefixes:
            for prefix in self.metric_prefixes:
                safe = _escape_regex(prefix)
                expr = f'count by (__name__) ({{__name__=~"{safe}.*"}})'
                try:
                    resp = self.mcp.query_prometheus(
                        expr=expr,
                        end_time=self.end_time,
                        query_type="instant",
                    )
                    names = _names_from_count_result(resp)
                    if names:
                        logger.info(
                            "PromQL discovery for prefix '%s' found %d metrics",
                            prefix, len(names),
                        )
                        all_names.extend(names)
                except RuntimeError as exc:
                    logger.warning("PromQL discovery for prefix '%s' failed: %s", prefix, exc)

            if all_names:
                return sorted(set(all_names))

        # ── Strategy 2: component-based PromQL count ───────────────────────
        if self.component:
            expr = f'count by (__name__) ({{pod=~".*{self.component}.*"}})'
            try:
                resp = self.mcp.query_prometheus(
                    expr=expr,
                    end_time=self.end_time,
                    query_type="instant",
                )
                names = _names_from_count_result(resp)
                if names:
                    logger.info(
                        "PromQL discovery for component '%s' found %d metrics",
                        self.component, len(names),
                    )
                    return names
            except RuntimeError as exc:
                logger.debug("PromQL component discovery failed: %s", exc)

        # ── Strategy 3: MCP list_prometheus_metric_names ───────────────────
        regex = ""
        if self.metric_prefixes:
            regex = f"^({'|'.join(_escape_regex(p) for p in self.metric_prefixes)}).*"
        elif self.component:
            regex = f".*{_escape_regex(self.component)}.*"

        try:
            names = self._list_all_metric_names(regex=regex)
            if names:
                logger.info("MCP metric name listing returned %d names", len(names))
                return names
        except RuntimeError as exc:
            logger.warning("Failed to list metric names via MCP: %s", exc)

        # ── Strategy 4: empty — will trigger fallback queries ──────────────
        return []

    def _list_all_metric_names(self, regex: str, page_size: int = 500) -> list[str]:
        """Paginate through list_prometheus_metric_names to get all matches."""
        all_names: list[str] = []
        page = 1
        while True:
            batch = self.mcp.list_metric_names(
                regex=regex, limit=page_size, page=page,
            )
            all_names.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
            # Safety: cap at 10 pages (5000 metrics) to avoid runaway queries
            if page > 10:
                logger.warning("Capped metric name listing at %d pages", page - 1)
                break
        return all_names

    def _run_category(self, category: str, queries: list[PromQLQuery]) -> CategoryResult:
        """Execute a list of queries for a category via MCP."""
        result = CategoryResult(category=category)

        for query_def in queries:
            result.raw_queries.append(query_def.expr)

            try:
                resp = self.mcp.query_prometheus(
                    expr=query_def.expr,
                    start_time=self.start_time,
                    end_time=self.end_time,
                    query_type="instant",
                )
            except RuntimeError as exc:
                result.errors.append(f"{query_def.name}: {exc}")
                logger.warning("Query %s failed: %s", query_def.name, exc)
                continue

            # MCP returns {"data": <prometheus result>, "hints": ...}
            data = resp.get("data") if isinstance(resp, dict) else resp
            if data is None:
                result.errors.append(f"{query_def.name}: empty response")
                continue

            samples = self._extract_samples(data, query_def.name)
            result.top_consumers.extend(samples)

        # Build a summary from top result
        if result.top_consumers:
            top = result.top_consumers[0]
            label_str = top.labels.get("pod", top.labels.get("instance", "unknown"))
            result.summary = f"Top: {label_str} ({_fmt_value(top.value, category)})"
        elif not result.errors:
            result.summary = "No data"

        return result

    @staticmethod
    def _extract_samples(data: Any, query_name: str) -> list[MetricSample]:
        """Extract MetricSample objects from MCP Prometheus query result.

        The MCP tool returns Prometheus results in various formats. We handle
        both vector (instant) and matrix (range) result types.
        """
        samples: list[MetricSample] = []

        # The data could be a list (vector result) or have a resultType field
        items = data if isinstance(data, list) else []

        for item in items:
            if not isinstance(item, dict):
                continue

            metric_labels = item.get("metric", {})

            # Instant vector: {"metric": {...}, "value": [ts, val]}
            if "value" in item:
                pair = item["value"]
                if isinstance(pair, list) and len(pair) >= 2:
                    try:
                        value = float(pair[1])
                    except (ValueError, TypeError):
                        continue
                    samples.append(MetricSample(
                        metric=query_name,
                        labels=metric_labels,
                        value=value,
                        timestamp=float(pair[0]),
                    ))

            # Range matrix: {"metric": {...}, "values": [[ts, val], ...]}
            elif "values" in item:
                values = item["values"]
                if values and isinstance(values, list):
                    pair = values[-1]
                    if isinstance(pair, list) and len(pair) >= 2:
                        try:
                            value = float(pair[1])
                        except (ValueError, TypeError):
                            continue
                        samples.append(MetricSample(
                            metric=query_name,
                            labels=metric_labels,
                            value=value,
                            timestamp=float(pair[0]),
                        ))

        return samples


from typing import Any  # noqa: E402


def _names_from_count_result(resp: dict | Any) -> list[str]:
    """Extract __name__ values from a ``count by (__name__) (...)`` query result.

    The MCP ``query_prometheus`` tool returns::

        {"data": [{"metric": {"__name__": "foo"}, "value": [ts, val]}, ...]}
    """
    names: list[str] = []
    data = resp.get("data") if isinstance(resp, dict) else resp
    if not isinstance(data, list):
        return names
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("metric", {}).get("__name__", "")
        if name:
            names.append(name)
    return sorted(set(names))


def _escape_regex(s: str) -> str:
    """Escape special regex characters in a string (except *)."""
    import re
    return re.sub(r'([.+?^${}()|[\]\\])', r'\\\1', s)


def _fmt_value(value: float, category: str) -> str:
    """Format a metric value with appropriate units."""
    if category in ("memory", "network", "disk"):
        if value >= 1e9:
            return f"{value / 1e9:.1f} GB"
        if value >= 1e6:
            return f"{value / 1e6:.1f} MB"
        if value >= 1e3:
            return f"{value / 1e3:.1f} KB"
        return f"{value:.0f} B"
    if category == "cpu":
        return f"{value:.2f} cores"
    if category == "latency":
        if value >= 1:
            return f"{value:.2f}s"
        return f"{value * 1000:.0f}ms"
    return f"{value:.4g}"
