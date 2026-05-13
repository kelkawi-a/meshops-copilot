"""Convert stress-test result dicts into structured Markdown.

This module is intentionally pure-function / no-side-effect so it can be
tested without a live cluster or LLM.  The caller (ReportWriterSkill) is
responsible for loading JSON, calling the LLM, and writing files.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _s(val: Any, fmt: str = ".2f", suffix: str = "") -> str:
    """Format a numeric value or return '—' if missing."""
    if val is None:
        return "—"
    try:
        return f"{val:{fmt}}{suffix}"
    except (TypeError, ValueError):
        return str(val)


def _pct(errors: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{errors / total * 100:.0f}%"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a GitHub-flavoured Markdown table."""
    sep = ["-" * max(len(h), 3) for h in headers]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


# ── Section formatters ────────────────────────────────────────────────────────

def _header(data: dict) -> str:
    target = data.get("target", "unknown")
    scenario = data.get("scenario", "unknown")
    source = data.get("query_source", "unknown")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Trino Stress Test Report",
        f"",
        f"| Field | Value |",
        f"| --- | --- |",
        f"| Target | `{target}` |",
        f"| Scenario | `{scenario}` |",
        f"| Query source | {source} |",
        f"| Generated | {ts} |",
    ]
    tables = data.get("discovered_tables", [])
    if tables:
        lines.append(f"| Tables discovered | {len(tables)} |")
    return "\n".join(lines)


def _baseline_section(baseline: dict) -> str:
    if not baseline:
        return ""
    rows = []
    for name, r in baseline.items():
        times = r.get("times", [])
        errs = r.get("errors", [])
        if times:
            rows.append([
                f"`{name}`",
                str(len(times)),
                str(len(errs)),
                _s(min(times), suffix="s"),
                _s(statistics.median(times), suffix="s"),
                _s(max(times), suffix="s"),
                _s(r.get("peak_mem_mb"), suffix=" MB"),
            ])
        else:
            rows.append([f"`{name}`", "0", str(len(errs)), "—", "—", "—", "—"])

    table = _md_table(
        ["Query", "Runs", "Errors", "Min", "Median", "Max", "Peak Mem"],
        rows,
    )
    return f"## Baseline (serial latency)\n\n{table}"


def _concurrency_section(concurrency: dict) -> str:
    if not concurrency:
        return ""
    rows = []
    for workers_key in sorted(concurrency, key=lambda k: int(k)):
        r = concurrency[workers_key]
        total = r.get("completed", 0) + r.get("errors", 0)
        rows.append([
            str(workers_key),
            str(r.get("completed", 0)),
            str(r.get("errors", 0)),
            _pct(r.get("errors", 0), total),
            _s(r.get("qps"), suffix=" QPS"),
            _s(r.get("p50"), suffix="s"),
            _s(r.get("p95"), suffix="s"),
            _s(r.get("p99"), suffix="s"),
        ])
    table = _md_table(
        ["Workers", "Done", "Errors", "Error %", "QPS", "p50", "p95", "p99"],
        rows,
    )
    return f"## Concurrency Ramp\n\n{table}"


def _breaking_section(breaking: dict) -> str:
    if not breaking:
        return ""
    rows = []
    threshold = None
    for workers_key in sorted(breaking, key=lambda k: int(k)):
        r = breaking[workers_key]
        total = r.get("completed", 0) + r.get("errors", 0)
        err_pct = r.get("errors", 0) / total * 100 if total else 0
        rows.append([
            str(workers_key),
            str(r.get("completed", 0)),
            str(r.get("errors", 0)),
            f"{err_pct:.0f}%",
            _s(r.get("qps"), suffix=" QPS"),
            _s(r.get("p50"), suffix="s"),
            _s(r.get("p99"), suffix="s"),
        ])
        if threshold is None and err_pct >= 50:
            threshold = int(workers_key)

    table = _md_table(
        ["Workers", "Done", "Errors", "Error %", "QPS", "p50", "p99"],
        rows,
    )
    note = (
        f"\n\n> **Breaking point: {threshold} concurrent workers** "
        f"(≥50% error rate first observed here)."
        if threshold else ""
    )
    return f"## Breaking Point{note}\n\n{table}"


def _memory_section(memory: dict) -> str:
    if not memory:
        return ""
    rows = []
    for name, r in memory.items():
        total = r.get("completed", 0) + r.get("errors", 0)
        rows.append([
            f"`{name}`",
            str(r.get("workers", "?")),
            str(r.get("completed", 0)),
            str(r.get("errors", 0)),
            _s(r.get("peak_mem_mb"), suffix=" MB"),
            _s(r.get("p50"), suffix="s"),
        ])
    table = _md_table(
        ["Query", "Workers", "Done", "Errors", "Peak Mem", "p50"],
        rows,
    )
    return f"## Memory Pressure\n\n{table}"


def _mixed_section(mixed: dict) -> str:
    """Format the mixed-workload phase.

    ``run_mixed`` returns::

        {
            "wall": float,
            "timings": {name: [float, ...]},
            "errors":  {name: [str, ...]},
            "docker_mid": {...},
            ...
        }
    """
    if not mixed:
        return ""
    timings: dict = mixed.get("timings", {})
    errors: dict  = mixed.get("errors", {})
    if not timings and not errors:
        return ""

    all_names = sorted(set(timings) | set(errors))
    rows = []
    for name in all_names:
        ts  = timings.get(name, [])
        err = errors.get(name, [])
        if ts:
            rows.append([
                f"`{name}`",
                str(len(ts)),
                str(len(err)),
                _s(statistics.median(ts), suffix="s"),
                _s(max(ts), suffix="s"),
            ])
        else:
            rows.append([f"`{name}`", "0", str(len(err)), "—", "—"])

    wall = mixed.get("wall")
    header = f"## Mixed Workload\n\nTotal wall time: {_s(wall, suffix='s')}\n"
    table = _md_table(["Query", "Done", "Errors", "Median", "Max"], rows)
    return header + "\n" + table


# ── Public API ────────────────────────────────────────────────────────────────

def format_stress_report(data: dict, llm_narrative: str = "") -> str:
    """Return a complete Markdown report string from a stress results dict.

    Args:
        data: The parsed ``stress_results.json`` dict.
        llm_narrative: Optional LLM-generated analysis to embed after the
            executive summary section.
    """
    sections = [_header(data)]

    if llm_narrative:
        sections.append(f"## Analysis\n\n{llm_narrative.strip()}")

    baseline = _baseline_section(data.get("baseline", {}))
    if baseline:
        sections.append(baseline)

    concurrency = _concurrency_section(data.get("concurrency", {}))
    if concurrency:
        sections.append(concurrency)

    breaking = _breaking_section(data.get("breaking", {}))
    if breaking:
        sections.append(breaking)

    memory = _memory_section(data.get("memory", {}))
    if memory:
        sections.append(memory)

    mixed = _mixed_section(data.get("mixed", {}))
    if mixed:
        sections.append(mixed)

    return "\n\n---\n\n".join(sections) + "\n"


def build_llm_prompt(data: dict) -> str:
    """Build a compact metrics summary to send to the LLM.

    Keeps token count low by only including the numbers, not raw SQL or
    column metadata.
    """
    target = data.get("target", "unknown")
    scenario = data.get("scenario", "unknown")
    source = data.get("query_source", "unknown")

    lines = [
        f"Trino stress test results for `{target}` (scenario: {scenario}, query source: {source}).",
        "",
        "### Baseline latencies (median / p95 / max, in seconds)",
    ]
    for name, r in data.get("baseline", {}).items():
        times = r.get("times", [])
        errs = r.get("errors", [])
        if times:
            lines.append(
                f"- {name}: median={statistics.median(times):.2f}s  "
                f"max={max(times):.2f}s  errors={len(errs)}"
            )
        else:
            lines.append(f"- {name}: no successful runs, {len(errs)} error(s)")

    concurrency = data.get("concurrency", {})
    if concurrency:
        lines += ["", "### Concurrency ramp (workers → QPS / p99 / error%)"]
        for w in sorted(concurrency, key=lambda k: int(k)):
            r = concurrency[w]
            total = r.get("completed", 0) + r.get("errors", 0)
            err_pct = r.get("errors", 0) / total * 100 if total else 0
            lines.append(
                f"- {w} workers: QPS={r.get('qps', 0):.2f}  "
                f"p99={_s(r.get('p99'), suffix='s')}  errors={err_pct:.0f}%"
            )

    breaking = data.get("breaking", {})
    if breaking:
        threshold = None
        lines += ["", "### Breaking point"]
        for w in sorted(breaking, key=lambda k: int(k)):
            r = breaking[w]
            total = r.get("completed", 0) + r.get("errors", 0)
            err_pct = r.get("errors", 0) / total * 100 if total else 0
            lines.append(f"- {w} workers: error rate={err_pct:.0f}%")
            if threshold is None and err_pct >= 50:
                threshold = int(w)
        if threshold:
            lines.append(f"Breaking point: {threshold} concurrent workers.")

    memory = data.get("memory", {})
    if memory:
        lines += ["", "### Memory pressure"]
        for name, r in memory.items():
            lines.append(
                f"- {name}: peak_mem={_s(r.get('peak_mem_mb'), suffix=' MB')}  "
                f"errors={r.get('errors', 0)}"
            )

    mixed = data.get("mixed", {})
    if mixed and (mixed.get("timings") or mixed.get("errors")):
        lines += ["", "### Mixed workload (concurrent query types)"]
        timings_m: dict = mixed.get("timings", {})
        errors_m: dict  = mixed.get("errors", {})
        all_names = sorted(set(timings_m) | set(errors_m))
        for name in all_names:
            ts  = timings_m.get(name, [])
            err = errors_m.get(name, [])
            if ts:
                lines.append(
                    f"- {name}: median={statistics.median(ts):.2f}s  "
                    f"max={max(ts):.2f}s  errors={len(err)}"
                )
            else:
                lines.append(f"- {name}: no successful runs, {len(err)} error(s)")
        if mixed.get("wall"):
            lines.append(f"Total wall time: {mixed['wall']:.2f}s")

    lines += [
        "",
        "Based on the numbers above, produce a Trino deployment fine-tuning analysis"
        " with the following sections. Be concise. Use Markdown. Reference actual"
        " values from the data — do not give generic advice.",
        "",
        "### Executive Summary",
        "2–3 sentences on overall cluster health and where it is struggling.",
        "",
        "### Primary Bottleneck",
        "Identify the main constraint (CPU, memory, coordinator, I/O, network)"
        " with direct evidence from the metrics.",
        "",
        "### Deployment Configuration Recommendations",
        "Up to 6 actionable changes ordered by expected impact. For each state:",
        "- The exact property / flag (e.g. `query.max-memory-per-node` in"
        " `config.properties`, `-Xmx` in `jvm.config`, `task.concurrency`)",
        "- A concrete recommended value derived from the observed metrics",
        "- One sentence explaining why this change addresses the bottleneck",
        "",
        "Cover the areas below where the data supports a recommendation:",
        "- JVM heap and GC (`-Xmx`, `-Xms`, GC flags in `jvm.config`)",
        "- Query memory limits (`query.max-memory`,"
        " `query.max-memory-per-node`, `memory.heap-headroom-per-node`)",
        "- Parallelism (`task.concurrency`, `task.max-drivers-per-task`,"
        " `task.writer-count`)",
        "- Worker node scaling (add/remove workers, coordinator isolation)",
        "- Connector-specific knobs (e.g. `hive.max-concurrent-file-system-operations`,"
        " `jdbc.maximum-pool-size`)",
        "- Session properties worth promoting to cluster-wide defaults",
    ]
    return "\n".join(lines)
