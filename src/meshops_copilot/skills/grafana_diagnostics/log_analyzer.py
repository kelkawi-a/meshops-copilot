"""grafana_diagnostics.log_analyzer — query Loki logs via MCP."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from meshops_copilot.connectors.grafana_mcp import GrafanaMCPClient
from meshops_copilot.skills.grafana_diagnostics.models import LogEntry, LogResult

logger = logging.getLogger(__name__)


class LogAnalyzer:
    """Queries Loki for logs related to a target component via the Grafana MCP server."""

    def __init__(
        self,
        mcp: GrafanaMCPClient,
        component: str = "",
        namespace: str = ".+",
        window_minutes: int = 60,
    ) -> None:
        self.mcp = mcp
        self.component = component
        self.namespace = namespace
        self.window_minutes = window_minutes

    def analyse(self) -> LogResult:
        """Run log queries and return a LogResult."""
        result = LogResult(component=self.component)

        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=self.window_minutes)
        start_rfc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_rfc = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build a LogQL stream selector for the component
        selector = self._build_selector()
        if not selector:
            result.summary = "No log selector could be built — skipping"
            return result

        # 1. Fetch recent error logs
        result = self._query_errors(result, selector, start_rfc, end_rfc)

        # 2. Fetch recent warning logs
        result = self._query_warnings(result, selector, start_rfc, end_rfc)

        # 3. Compute error rate via metric query
        result = self._query_error_rate(result, selector, start_rfc, end_rfc)

        # 4. Get a sample of recent logs for context
        result = self._query_sample(result, selector, start_rfc, end_rfc)

        # 5. Get log patterns (if Loki supports it)
        result = self._query_patterns(result, selector, start_rfc, end_rfc)

        # Build summary
        result.summary = self._build_summary(result)

        return result

    # ── Query helpers ──────────────────────────────────────────────────────────

    def _build_selector(self) -> str:
        """Build a LogQL stream selector like {namespace=~"...", pod=~".*superset.*"}."""
        parts: list[str] = []
        if self.namespace and self.namespace != ".+":
            parts.append(f'namespace=~"{self.namespace}"')
        if self.component:
            parts.append(f'pod=~".*{self.component}.*"')

        if not parts:
            if self.component:
                parts.append(f'app=~".*{self.component}.*"')
            else:
                return ""

        return "{" + ", ".join(parts) + "}"

    def _query_errors(
        self, result: LogResult, selector: str, start: str, end: str,
    ) -> LogResult:
        """Query for error-level log lines."""
        logql = f'{selector} |~ "(?i)(error|exception|fatal|panic|traceback)"'
        result.raw_queries.append(logql)
        try:
            resp = self.mcp.query_loki_logs(
                logql=logql, start_rfc3339=start, end_rfc3339=end, limit=50,
            )
            entries = _extract_log_entries(resp)
            for entry in entries:
                result.error_lines.append(entry)
            logger.info("Found %d error log lines", len(result.error_lines))
        except RuntimeError as exc:
            result.errors.append(f"Error log query failed: {exc}")
        return result

    def _query_warnings(
        self, result: LogResult, selector: str, start: str, end: str,
    ) -> LogResult:
        """Query for warning-level log lines."""
        logql = f'{selector} |~ "(?i)(warn|warning|timeout|retry|slow)"'
        result.raw_queries.append(logql)
        try:
            resp = self.mcp.query_loki_logs(
                logql=logql, start_rfc3339=start, end_rfc3339=end, limit=30,
            )
            entries = _extract_log_entries(resp)
            for entry in entries:
                result.warning_lines.append(entry)
            logger.info("Found %d warning log lines", len(result.warning_lines))
        except RuntimeError as exc:
            result.errors.append(f"Warning log query failed: {exc}")
        return result

    def _query_error_rate(
        self, result: LogResult, selector: str, start: str, end: str,
    ) -> LogResult:
        """Compute error rate using a LogQL metric query."""
        logql = f'sum(rate({selector} |~ "(?i)(error|exception|fatal)" [5m]))'
        result.raw_queries.append(logql)
        try:
            resp = self.mcp.query_loki_logs(
                logql=logql, start_rfc3339=start, end_rfc3339=end,
                limit=1,
            )
            # The MCP tool returns metric results in the "data" field
            data = resp.get("data", []) if isinstance(resp, dict) else resp
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # Instant metric: {"value": float, ...}
                    if "value" in item and item["value"] is not None:
                        try:
                            result.error_rate = float(item["value"])
                        except (ValueError, TypeError):
                            pass
                        break
                    # Range metric: {"values": [...], ...}
                    if "values" in item and item["values"]:
                        last = item["values"][-1]
                        if isinstance(last, dict) and "value" in last:
                            try:
                                result.error_rate = float(last["value"])
                            except (ValueError, TypeError):
                                pass
                            break
            logger.info("Error rate: %.4f errors/sec", result.error_rate)
        except RuntimeError as exc:
            result.errors.append(f"Error rate query failed: {exc}")
        return result

    def _query_sample(
        self, result: LogResult, selector: str, start: str, end: str,
    ) -> LogResult:
        """Get a small sample of recent logs for general context."""
        result.raw_queries.append(selector)
        try:
            resp = self.mcp.query_loki_logs(
                logql=selector, start_rfc3339=start, end_rfc3339=end, limit=20,
            )
            entries = _extract_log_entries(resp)
            for entry in entries[:20]:
                result.sample_lines.append(entry)

            # Extract metadata for total lines info
            metadata = resp.get("metadata", {}) if isinstance(resp, dict) else {}
            if metadata.get("totalLinesScanned"):
                result.total_lines = metadata["totalLinesScanned"]
        except RuntimeError as exc:
            result.errors.append(f"Sample log query failed: {exc}")
        return result

    def _query_patterns(
        self, result: LogResult, selector: str, start: str, end: str,
    ) -> LogResult:
        """Get detected log patterns from Loki."""
        try:
            patterns = self.mcp.query_loki_patterns(
                logql=selector, start_rfc3339=start, end_rfc3339=end,
            )
            if patterns:
                result.patterns = patterns
                logger.info("Found %d log patterns", len(patterns))
        except RuntimeError as exc:
            # Patterns are optional — some Loki versions don't support them
            logger.debug("Log pattern query failed (non-critical): %s", exc)
        return result

    @staticmethod
    def _build_summary(result: LogResult) -> str:
        """Build a human-readable summary of log findings."""
        parts: list[str] = []
        if result.error_lines:
            parts.append(f"{len(result.error_lines)} errors")
        if result.warning_lines:
            parts.append(f"{len(result.warning_lines)} warnings")
        if result.error_rate > 0:
            parts.append(f"error rate: {result.error_rate:.2f}/s")
        if hasattr(result, "patterns") and result.patterns:
            parts.append(f"{len(result.patterns)} log patterns")
        if not parts:
            return "No notable log entries found"
        return ", ".join(parts)


def _extract_log_entries(resp: dict | list) -> list[LogEntry]:
    """Extract LogEntry objects from an MCP query_loki_logs response.

    The MCP tool returns:
    {
      "data": [{"timestamp": "...", "line": "...", "labels": {...}}, ...],
      "metadata": {...},
      "hints": ...
    }
    """
    entries: list[LogEntry] = []
    data = resp.get("data", []) if isinstance(resp, dict) else resp
    if not isinstance(data, list):
        return entries

    for item in data:
        if not isinstance(item, dict):
            continue
        line = item.get("line", "")
        if not line:
            continue
        entries.append(LogEntry(
            timestamp=item.get("timestamp", ""),
            line=line[:500],
            labels=item.get("labels", {}),
        ))

    return entries
