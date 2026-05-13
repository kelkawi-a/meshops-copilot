"""Grafana MCP connector — spawns mcp-grafana as a subprocess and calls tools via JSON-RPC.

The official ``grafana/mcp-grafana`` server exposes Prometheus, Loki, dashboard,
alerting, and other tools over the Model Context Protocol (MCP).  This module
provides a thin Python client that:

1. Spawns ``mcp-grafana`` (via ``uvx mcp-grafana``) as a child process
   communicating over **stdio** (JSON-RPC 2.0).
2. Sends ``initialize`` + ``initialized`` handshake.
3. Exposes ``call_tool(name, arguments)`` to invoke any MCP tool.
4. Offers convenience wrappers for the Prometheus and Loki tools we use
   most frequently in the diagnostics skill.

Environment variables consumed by the subprocess:
    GRAFANA_URL — Grafana instance URL (required)
    GRAFANA_SERVICE_ACCOUNT_TOKEN — service-account token (required)

Only stdlib modules are used (subprocess, json, threading) to stay consistent
with the rest of the meshops-copilot connectors.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)


class GrafanaMCPClient:
    """Thin MCP client that talks to ``mcp-grafana`` over stdio JSON-RPC."""

    def __init__(
        self,
        grafana_url: str,
        grafana_token: str,
        command: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._grafana_url = grafana_url
        self._grafana_token = grafana_token
        self._command = command or ["uvx", "mcp-grafana"]
        self._extra_env = extra_env or {}
        self._proc: subprocess.Popen | None = None
        self._request_id = 0
        self._lock = threading.Lock()
        # Cached datasource UIDs
        self._prometheus_uid: str | None = None
        self._loki_uid: str | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the mcp-grafana subprocess and perform the MCP handshake."""
        env = os.environ.copy()
        env["GRAFANA_URL"] = self._grafana_url
        env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = self._grafana_token
        env.update(self._extra_env)

        logger.info("Starting MCP server: %s", " ".join(self._command))
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )

        # MCP handshake: initialize → response → notifications/initialized
        init_result = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "meshops-copilot", "version": "0.1.0"},
        })
        logger.info(
            "MCP server ready: %s v%s",
            init_result.get("serverInfo", {}).get("name", "?"),
            init_result.get("serverInfo", {}).get("version", "?"),
        )

        # Send initialized notification (no response expected)
        self._send_notification("notifications/initialized", {})

    def stop(self) -> None:
        """Shut down the subprocess."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> GrafanaMCPClient:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── JSON-RPC transport ─────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("MCP server process is not running")

        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        raw = json.dumps(msg) + "\n"
        logger.debug("MCP → %s", raw.strip())
        self._proc.stdin.write(raw.encode())
        self._proc.stdin.flush()

        # Read response line
        line = self._proc.stdout.readline()
        if not line:
            stderr_out = ""
            if self._proc.stderr:
                try:
                    stderr_out = self._proc.stderr.read().decode(errors="replace")
                except Exception:
                    pass
            raise RuntimeError(
                f"MCP server returned empty response (process exited: {self._proc.poll()}). "
                f"stderr: {stderr_out[:500]}"
            )

        logger.debug("MCP ← %s", line.decode().strip())
        resp = json.loads(line)

        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"MCP error {err.get('code', '?')}: {err.get('message', 'unknown')}"
            )

        return resp.get("result", {})

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._proc is None:
            return
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        raw = json.dumps(msg) + "\n"
        logger.debug("MCP notify → %s", raw.strip())
        self._proc.stdin.write(raw.encode())
        self._proc.stdin.flush()

    # ── Generic tool call ──────────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool and return the parsed result content.

        Returns the text content of the first content block, parsed as JSON
        if possible, otherwise as a raw string.
        """
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        # Result structure: {"content": [{"type": "text", "text": "..."}], "isError": bool}
        if result.get("isError"):
            text = self._extract_text(result)
            raise RuntimeError(f"MCP tool {tool_name} error: {text}")

        text = self._extract_text(result)

        # Try to parse as JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    @staticmethod
    def _extract_text(result: dict) -> str:
        content = result.get("content", [])
        if content and isinstance(content, list):
            return content[0].get("text", "")
        return str(result)

    def list_tools(self) -> list[dict]:
        """List available MCP tools."""
        result = self._send_request("tools/list", {})
        return result.get("tools", [])

    # ── Datasource discovery ───────────────────────────────────────────────────

    def list_datasources(self) -> list[dict]:
        """List all Grafana datasources via the MCP tool."""
        result = self.call_tool("list_datasources", {})
        # The MCP tool returns {"datasources": [...], "total": N, "hasMore": bool}
        if isinstance(result, dict):
            return result.get("datasources", [])
        if isinstance(result, list):
            return result
        return []

    def discover_prometheus_uid(self) -> str:
        """Find the UID of the first Prometheus-compatible datasource."""
        if self._prometheus_uid:
            return self._prometheus_uid
        datasources = self.list_datasources()
        for ds in datasources:
            ds_type = ds.get("type", "").lower()
            if ds_type in ("prometheus", "mimir", "cortex"):
                self._prometheus_uid = ds.get("uid", "")
                logger.info(
                    "Discovered Prometheus datasource: %s (uid=%s)",
                    ds.get("name", "?"), self._prometheus_uid,
                )
                return self._prometheus_uid
        raise RuntimeError("No Prometheus datasource found in Grafana")

    def discover_loki_uid(self) -> str:
        """Find the UID of the first Loki-compatible datasource."""
        if self._loki_uid:
            return self._loki_uid
        datasources = self.list_datasources()
        for ds in datasources:
            ds_type = ds.get("type", "").lower()
            if "loki" in ds_type:
                self._loki_uid = ds.get("uid", "")
                logger.info(
                    "Discovered Loki datasource: %s (uid=%s)",
                    ds.get("name", "?"), self._loki_uid,
                )
                return self._loki_uid
        raise RuntimeError("No Loki datasource found in Grafana")

    # ── Prometheus convenience methods ─────────────────────────────────────────

    def list_metric_names(
        self,
        regex: str = "",
        limit: int = 500,
        page: int = 1,
        datasource_uid: str | None = None,
    ) -> list[str]:
        """List Prometheus metric names, optionally filtered by regex."""
        uid = datasource_uid or self.discover_prometheus_uid()
        args: dict[str, Any] = {"datasourceUid": uid, "limit": limit, "page": page}
        if regex:
            args["regex"] = regex
        result = self.call_tool("list_prometheus_metric_names", args)
        if isinstance(result, list):
            return result
        return []

    def query_prometheus(
        self,
        expr: str,
        start_time: str = "now-1h",
        end_time: str = "now",
        step_seconds: int = 60,
        query_type: str = "instant",
        datasource_uid: str | None = None,
    ) -> dict[str, Any]:
        """Execute a PromQL query via the MCP tool."""
        uid = datasource_uid or self.discover_prometheus_uid()
        args: dict[str, Any] = {
            "datasourceUid": uid,
            "expr": expr,
            "endTime": end_time,
            "queryType": query_type,
        }
        if query_type == "range":
            args["startTime"] = start_time
            args["stepSeconds"] = step_seconds
        return self.call_tool("query_prometheus", args)

    def query_histogram(
        self,
        metric: str,
        percentile: float = 95,
        labels: str = "",
        rate_interval: str = "5m",
        start_time: str = "now-1h",
        end_time: str = "now",
        step_seconds: int = 60,
        datasource_uid: str | None = None,
    ) -> dict[str, Any]:
        """Query Prometheus histogram percentiles."""
        uid = datasource_uid or self.discover_prometheus_uid()
        args: dict[str, Any] = {
            "datasourceUid": uid,
            "metric": metric,
            "percentile": percentile,
            "rateInterval": rate_interval,
            "startTime": start_time,
            "endTime": end_time,
            "stepSeconds": step_seconds,
        }
        if labels:
            args["labels"] = labels
        return self.call_tool("query_prometheus_histogram", args)

    def list_label_values(
        self,
        label_name: str,
        limit: int = 100,
        datasource_uid: str | None = None,
    ) -> list[str]:
        """List values for a Prometheus label."""
        uid = datasource_uid or self.discover_prometheus_uid()
        result = self.call_tool("list_prometheus_label_values", {
            "datasourceUid": uid,
            "labelName": label_name,
            "limit": limit,
        })
        if isinstance(result, list):
            return result
        return []

    # ── Loki convenience methods ───────────────────────────────────────────────

    def query_loki_logs(
        self,
        logql: str,
        start_rfc3339: str = "",
        end_rfc3339: str = "",
        limit: int = 50,
        direction: str = "backward",
        datasource_uid: str | None = None,
    ) -> dict[str, Any]:
        """Query Loki logs via the MCP tool."""
        uid = datasource_uid or self.discover_loki_uid()
        args: dict[str, Any] = {
            "datasourceUid": uid,
            "logql": logql,
            "limit": limit,
            "direction": direction,
        }
        if start_rfc3339:
            args["startRfc3339"] = start_rfc3339
        if end_rfc3339:
            args["endRfc3339"] = end_rfc3339
        return self.call_tool("query_loki_logs", args)

    def query_loki_patterns(
        self,
        logql: str,
        start_rfc3339: str = "",
        end_rfc3339: str = "",
        step: str = "",
        datasource_uid: str | None = None,
    ) -> list[dict]:
        """Query detected log patterns from Loki."""
        uid = datasource_uid or self.discover_loki_uid()
        args: dict[str, Any] = {"datasourceUid": uid, "logql": logql}
        if start_rfc3339:
            args["startRfc3339"] = start_rfc3339
        if end_rfc3339:
            args["endRfc3339"] = end_rfc3339
        if step:
            args["step"] = step
        result = self.call_tool("query_loki_patterns", args)
        if isinstance(result, list):
            return result
        return []

    def query_loki_stats(
        self,
        logql: str,
        start_rfc3339: str = "",
        end_rfc3339: str = "",
        datasource_uid: str | None = None,
    ) -> dict[str, Any]:
        """Get stats about Loki log streams."""
        uid = datasource_uid or self.discover_loki_uid()
        args: dict[str, Any] = {"datasourceUid": uid, "logql": logql}
        if start_rfc3339:
            args["startRfc3339"] = start_rfc3339
        if end_rfc3339:
            args["endRfc3339"] = end_rfc3339
        return self.call_tool("query_loki_stats", args)

    def list_loki_label_names(
        self,
        start_rfc3339: str = "",
        end_rfc3339: str = "",
        datasource_uid: str | None = None,
    ) -> list[str]:
        """List Loki label names."""
        uid = datasource_uid or self.discover_loki_uid()
        args: dict[str, Any] = {"datasourceUid": uid}
        if start_rfc3339:
            args["startRfc3339"] = start_rfc3339
        if end_rfc3339:
            args["endRfc3339"] = end_rfc3339
        result = self.call_tool("list_loki_label_names", args)
        if isinstance(result, list):
            return result
        return []

    def list_loki_label_values(
        self,
        label_name: str,
        start_rfc3339: str = "",
        end_rfc3339: str = "",
        datasource_uid: str | None = None,
    ) -> list[str]:
        """List Loki label values."""
        uid = datasource_uid or self.discover_loki_uid()
        args: dict[str, Any] = {"datasourceUid": uid, "labelName": label_name}
        if start_rfc3339:
            args["startRfc3339"] = start_rfc3339
        if end_rfc3339:
            args["endRfc3339"] = end_rfc3339
        result = self.call_tool("list_loki_label_values", args)
        if isinstance(result, list):
            return result
        return []
