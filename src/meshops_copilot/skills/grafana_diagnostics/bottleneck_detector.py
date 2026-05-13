"""grafana_diagnostics.bottleneck_detector — rank bottlenecks from metric and log results."""

from __future__ import annotations

from meshops_copilot.skills.grafana_diagnostics.models import (
    Bottleneck,
    CategoryResult,
    LogResult,
    RecommendedAction,
    Severity,
)


# Thresholds for severity classification
_CPU_CRITICAL = 4.0       # > 4 cores sustained
_CPU_HIGH = 2.0
_MEMORY_CRITICAL = 8e9    # > 8 GB
_MEMORY_HIGH = 4e9
_QUEUE_CRITICAL = 50
_QUEUE_HIGH = 20
_NET_CRITICAL = 500e6     # > 500 MB/s
_NET_HIGH = 100e6
_LATENCY_CRITICAL = 5.0   # > 5s p95
_LATENCY_HIGH = 1.0       # > 1s
_ERROR_RATE_CRITICAL = 1.0  # > 1 error/s
_ERROR_RATE_HIGH = 0.1


def detect_bottlenecks(
    metric_results: dict[str, CategoryResult],
    logs: LogResult | None = None,
) -> list[Bottleneck]:
    """Detect and rank bottlenecks from all results."""
    bottlenecks: list[Bottleneck] = []

    # ── Metric-based bottlenecks ───────────────────────────────────────────────
    for category, result in metric_results.items():
        if not result.top_consumers:
            continue

        top = result.top_consumers[0]
        label = top.labels.get("pod", top.labels.get("instance", "unknown"))

        b = _classify_metric_bottleneck(category, label, top.metric, top.value)
        if b:
            bottlenecks.append(b)

    # ── Log-based bottlenecks ──────────────────────────────────────────────────
    if logs:
        if logs.error_rate >= _ERROR_RATE_CRITICAL:
            bottlenecks.append(Bottleneck(
                rank=0,
                component=logs.component or "unknown",
                severity=Severity.CRITICAL,
                root_cause=f"Error rate at {logs.error_rate:.2f}/s in logs",
                source="logs",
                metric="log_error_rate",
                value=logs.error_rate,
            ))
        elif logs.error_rate >= _ERROR_RATE_HIGH:
            bottlenecks.append(Bottleneck(
                rank=0,
                component=logs.component or "unknown",
                severity=Severity.HIGH,
                root_cause=f"Elevated error rate at {logs.error_rate:.2f}/s in logs",
                source="logs",
                metric="log_error_rate",
                value=logs.error_rate,
            ))
        elif logs.error_lines and len(logs.error_lines) >= 10:
            bottlenecks.append(Bottleneck(
                rank=0,
                component=logs.component or "unknown",
                severity=Severity.MEDIUM,
                root_cause=f"{len(logs.error_lines)} error log entries in window",
                source="logs",
                metric="log_error_count",
                value=float(len(logs.error_lines)),
            ))

    # ── Rank by severity ────────────────────────────────────────────────────────
    severity_order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    bottlenecks.sort(key=lambda b: (severity_order.get(b.severity, 9), -b.value))
    for i, b in enumerate(bottlenecks, start=1):
        b.rank = i

    return bottlenecks[:10]


def _classify_metric_bottleneck(
    category: str, label: str, metric: str, value: float,
) -> Bottleneck | None:
    """Classify a single top-consumer into a bottleneck if it exceeds thresholds."""
    if category == "cpu":
        if value >= _CPU_CRITICAL:
            return Bottleneck(0, label, Severity.CRITICAL, f"CPU at {value:.2f} cores", "metrics", metric, value)
        if value >= _CPU_HIGH:
            return Bottleneck(0, label, Severity.HIGH, f"Elevated CPU at {value:.2f} cores", "metrics", metric, value)

    elif category == "memory":
        if value >= _MEMORY_CRITICAL:
            return Bottleneck(0, label, Severity.CRITICAL, f"Memory at {value / 1e9:.1f} GB", "metrics", metric, value)
        if value >= _MEMORY_HIGH:
            return Bottleneck(0, label, Severity.HIGH, f"Elevated memory at {value / 1e9:.1f} GB", "metrics", metric, value)

    elif category == "latency":
        if value >= _LATENCY_CRITICAL:
            return Bottleneck(0, label, Severity.CRITICAL, f"p95 latency at {value:.2f}s", "metrics", metric, value)
        if value >= _LATENCY_HIGH:
            return Bottleneck(0, label, Severity.HIGH, f"Elevated latency at {value:.2f}s", "metrics", metric, value)

    elif category == "errors":
        if value > 0:
            return Bottleneck(0, label, Severity.HIGH, f"Error rate at {value:.4g}/s", "metrics", metric, value)

    elif category == "queue":
        if value >= _QUEUE_CRITICAL:
            return Bottleneck(0, label, Severity.CRITICAL, f"Queue depth at {value:.0f}", "metrics", metric, value)
        if value >= _QUEUE_HIGH:
            return Bottleneck(0, label, Severity.HIGH, f"Queue depth at {value:.0f}", "metrics", metric, value)

    elif category == "network":
        if value >= _NET_CRITICAL:
            return Bottleneck(0, label, Severity.HIGH, f"Network at {value / 1e6:.0f} MB/s", "metrics", metric, value)
        if value >= _NET_HIGH:
            return Bottleneck(0, label, Severity.MEDIUM, f"Elevated network at {value / 1e6:.0f} MB/s", "metrics", metric, value)

    elif category == "disk":
        if value > 50e6:
            return Bottleneck(0, label, Severity.MEDIUM, f"Disk I/O at {value / 1e6:.0f} MB/s", "metrics", metric, value)

    return None


def recommend_actions(bottlenecks: list[Bottleneck]) -> list[RecommendedAction]:
    """Generate recommended actions based on detected bottlenecks."""
    actions: list[RecommendedAction] = []

    for b in bottlenecks:
        cat = _infer_category(b)

        if cat == "cpu":
            actions.append(RecommendedAction(
                action=f"Increase CPU limits for {b.component} or add horizontal replicas",
                reason=b.root_cause, effort="M",
            ))
        elif cat == "memory":
            actions.append(RecommendedAction(
                action=f"Increase memory limits for {b.component}; review heap settings",
                reason=b.root_cause, effort="S",
            ))
        elif cat == "latency":
            actions.append(RecommendedAction(
                action=f"Investigate slow queries/requests in {b.component}; consider caching or query optimisation",
                reason=b.root_cause, effort="M",
            ))
        elif cat == "errors":
            actions.append(RecommendedAction(
                action=f"Investigate error source in {b.component}; check logs for stack traces",
                reason=b.root_cause, effort="S",
            ))
        elif cat == "queue":
            actions.append(RecommendedAction(
                action="Scale query workers or reduce concurrency limits",
                reason=b.root_cause, effort="M",
            ))
        elif cat == "network":
            actions.append(RecommendedAction(
                action=f"Investigate network-heavy workload in {b.component}; consider result caching",
                reason=b.root_cause, effort="M",
            ))
        elif cat == "disk":
            actions.append(RecommendedAction(
                action=f"Review disk spill in {b.component}; increase memory to avoid disk fallback",
                reason=b.root_cause, effort="L",
            ))
        elif b.source == "logs":
            actions.append(RecommendedAction(
                action=f"Review error logs for {b.component}; identify recurring exceptions",
                reason=b.root_cause, effort="S",
            ))

    return actions


def _infer_category(b: Bottleneck) -> str:
    """Infer the category from a bottleneck's metric or root cause."""
    text = f"{b.metric} {b.root_cause}".lower()
    if "cpu" in text:
        return "cpu"
    if "memory" in text or "heap" in text or "oom" in text:
        return "memory"
    if "latency" in text or "duration" in text or "p95" in text or "slow" in text:
        return "latency"
    if "error" in text:
        return "errors"
    if "queue" in text or "depth" in text:
        return "queue"
    if "network" in text:
        return "network"
    if "disk" in text or "io" in text:
        return "disk"
    return ""
