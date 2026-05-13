"""grafana_diagnostics.queries — PromQL query generation with metric discovery.

Instead of hardcoded queries, this module provides:
1. A discovery step that finds which metrics actually exist for a target.
2. Pattern-based classification of discovered metrics into categories.
3. Dynamic query generation from what's available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PromQLQuery:
    """A named PromQL query with metadata."""

    name: str
    expr: str
    category: str
    description: str = ""


# ── Metric classification patterns ────────────────────────────────────────────
# Maps regex patterns to (category, sub_type) so we can classify any metric
# we discover in the target namespace.

_METRIC_PATTERNS: list[tuple[str, str, str]] = [
    # CPU
    (r"(container_cpu_usage_seconds_total|process_cpu_seconds_total)", "cpu", "usage"),
    (r"cpu_cfs_throttled", "cpu", "throttle"),
    (r"cpu", "cpu", "generic"),

    # Memory
    (r"(container_memory_working_set_bytes|container_memory_usage_bytes)", "memory", "working_set"),
    (r"(go_memstats_heap_inuse_bytes|go_memstats_alloc_bytes)", "memory", "heap"),
    (r"(go_gc_duration_seconds|go_gc_pause)", "memory", "gc"),
    (r"(container_oom|oom_kill)", "memory", "oom"),
    (r"(memory|mem_)", "memory", "generic"),

    # Latency / request performance (application-level)
    (r"(request_duration|request_latency|http_request_duration|response_time)", "latency", "http"),
    (r"(query_duration|query_execution_time|query_time|execution_time)", "latency", "query"),
    (r"(duration_seconds|latency_seconds|time_seconds)", "latency", "generic"),

    # Error rates
    (r"(errors_total|error_count|failed_total|failures_total)", "errors", "counter"),
    (r"(http_responses_total|response_status|status_code)", "errors", "http_status"),
    (r"(5[0-9]{2}|error|err_)", "errors", "generic"),

    # Throughput / request rates
    (r"(requests_total|request_count|http_requests_total|queries_total)", "throughput", "requests"),
    (r"(rows_total|rows_processed|bytes_processed)", "throughput", "data"),

    # Queue / concurrency
    (r"(queue_length|queue_size|queued|pending)", "queue", "depth"),
    (r"(in_progress|inflight|active_queries|concurrent)", "queue", "inflight"),
    (r"(pool_size|thread_pool|worker)", "queue", "pool"),

    # Network
    (r"(network_receive_bytes|network_transmit_bytes|rx_bytes|tx_bytes)", "network", "throughput"),
    (r"(network_.*error|network_.*drop)", "network", "errors"),

    # Disk
    (r"(fs_reads_bytes|fs_writes_bytes|disk_read|disk_write)", "disk", "throughput"),
    (r"(fs_reads_total|fs_writes_total|disk_io)", "disk", "iops"),
]

# Categories in display order
ALL_CATEGORIES = ["cpu", "memory", "latency", "errors", "throughput", "queue", "network", "disk"]


@dataclass
class DiscoveredMetric:
    """A metric discovered in the target environment."""

    name: str
    category: str
    sub_type: str


def classify_metric(metric_name: str) -> DiscoveredMetric | None:
    """Classify a metric name into a category using pattern matching."""
    for pattern, category, sub_type in _METRIC_PATTERNS:
        if re.search(pattern, metric_name, re.IGNORECASE):
            return DiscoveredMetric(name=metric_name, category=category, sub_type=sub_type)
    return None


def discover_and_classify(
    all_metric_names: list[str],
    component_filter: str = "",
) -> dict[str, list[DiscoveredMetric]]:
    """Classify a list of metric names into categories.

    Parameters
    ----------
    all_metric_names : list[str]
        All metric names available (e.g. from ``/api/v1/label/__name__/values``).
    component_filter : str
        Optional substring to pre-filter metrics (e.g. "superset", "trino").
        If empty, all metrics are classified.

    Returns
    -------
    dict mapping category -> list of DiscoveredMetric
    """
    by_category: dict[str, list[DiscoveredMetric]] = {cat: [] for cat in ALL_CATEGORIES}

    for name in all_metric_names:
        # If a component filter is set, only keep metrics whose name contains it
        if component_filter and component_filter.lower() not in name.lower():
            # Also keep standard container/node metrics — they're always relevant
            if not any(prefix in name for prefix in (
                "container_", "node_", "process_", "go_", "kube_",
            )):
                continue

        classified = classify_metric(name)
        if classified and classified.category in by_category:
            by_category[classified.category].append(classified)

    return by_category


def build_queries_from_discovered(
    discovered: dict[str, list[DiscoveredMetric]],
    namespace: str = ".+",
    component: str = "",
    limit: int = 5,
) -> list[PromQLQuery]:
    """Generate PromQL queries from discovered metrics.

    Builds topk/rate queries appropriate for each metric's category and sub-type.
    """
    queries: list[PromQLQuery] = []

    label_filter = _build_label_filter(namespace, component)

    for category, metrics in discovered.items():
        if not metrics:
            continue

        # Deduplicate and take the most specific metrics first
        seen: set[str] = set()
        for m in metrics:
            if m.name in seen:
                continue
            seen.add(m.name)

            q = _query_for_metric(m, label_filter, limit)
            if q:
                queries.append(q)

    return queries


def _build_label_filter(namespace: str, component: str) -> str:
    """Build a Prometheus label filter string."""
    parts = []
    if namespace and namespace != ".+":
        parts.append(f'namespace=~"{namespace}"')
    if component:
        parts.append(f'pod=~".*{component}.*"')
    if not parts:
        return ""
    return "{" + ", ".join(parts) + "}"


def _query_for_metric(
    metric: DiscoveredMetric,
    label_filter: str,
    limit: int,
) -> PromQLQuery | None:
    """Build an appropriate PromQL query for a discovered metric."""
    name = metric.name
    lf = label_filter

    # Counter metrics (name ends with _total) → use rate()
    if name.endswith("_total") or name.endswith("_count"):
        if metric.category in ("cpu", "network", "disk", "throughput", "errors"):
            expr = f"topk({limit}, sum by (pod) (rate({name}{lf}[5m])))"
            return PromQLQuery(
                name=name,
                expr=expr,
                category=metric.category,
                description=f"Rate of {name} by pod",
            )

    # Histogram summaries (_bucket, _sum, _count) → use histogram_quantile for latency
    if metric.category == "latency" and name.endswith("_bucket"):
        base = name.removesuffix("_bucket")
        expr = f'histogram_quantile(0.95, sum by (le, pod) (rate({name}{lf}[5m])))'
        return PromQLQuery(
            name=f"{base}_p95",
            expr=expr,
            category="latency",
            description=f"p95 latency for {base}",
        )

    if metric.category == "latency" and name.endswith("_sum"):
        base = name.removesuffix("_sum")
        count_name = f"{base}_count"
        expr = f"topk({limit}, sum by (pod) (rate({name}{lf}[5m])) / sum by (pod) (rate({count_name}{lf}[5m])))"
        return PromQLQuery(
            name=f"{base}_avg",
            expr=expr,
            category="latency",
            description=f"Average latency for {base}",
        )

    # Gauge metrics → use directly or avg_over_time
    if metric.category in ("memory", "queue"):
        if metric.sub_type in ("depth", "inflight", "pool"):
            expr = f"topk({limit}, {name}{lf})"
        else:
            expr = f"topk({limit}, {name}{lf})"
        return PromQLQuery(
            name=name,
            expr=expr,
            category=metric.category,
            description=f"Current {name} by pod",
        )

    # Fallback: just query it as-is
    if metric.sub_type != "generic":
        expr = f"topk({limit}, {name}{lf})"
        return PromQLQuery(
            name=name,
            expr=expr,
            category=metric.category,
            description=f"{name}",
        )

    return None


# ── Fallback static queries (when discovery returns nothing) ───────────────────

FALLBACK_QUERIES = [
    PromQLQuery("cpu_top", 'topk(5, sum by (pod) (rate(container_cpu_usage_seconds_total[5m])))', "cpu", "Top pods by CPU"),
    PromQLQuery("cpu_process", 'topk(5, sum by (pod) (rate(process_cpu_seconds_total[5m])))', "cpu", "Top pods by process CPU"),
    PromQLQuery("mem_top", 'topk(5, container_memory_working_set_bytes)', "memory", "Top pods by memory"),
    PromQLQuery("mem_heap", 'topk(5, go_memstats_heap_inuse_bytes)', "memory", "Top pods by Go heap"),
    PromQLQuery("net_rx", 'topk(5, sum by (pod) (rate(container_network_receive_bytes_total[5m])))', "network", "Top pods by net receive"),
    PromQLQuery("net_tx", 'topk(5, sum by (pod) (rate(container_network_transmit_bytes_total[5m])))', "network", "Top pods by net transmit"),
    PromQLQuery("disk_read", 'topk(5, sum by (pod) (rate(container_fs_reads_bytes_total[5m])))', "disk", "Top pods by disk read"),
    PromQLQuery("disk_write", 'topk(5, sum by (pod) (rate(container_fs_writes_bytes_total[5m])))', "disk", "Top pods by disk write"),
]
