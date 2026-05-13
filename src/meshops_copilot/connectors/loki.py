"""Loki HTTP API connector.

Talks directly to the Loki HTTP API or proxies through Grafana's
datasource proxy (``/api/datasources/proxy/uid/<uid>/``).  Uses stdlib
``urllib`` to remain consistent with other connectors in this package.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LokiStream:
    """A single log stream with its labels and log lines."""

    labels: dict[str, str]
    entries: list[tuple[str, str]] = field(default_factory=list)  # [(timestamp_ns, line), ...]


@dataclass
class LokiResponse:
    """Parsed response from the Loki HTTP API."""

    status: str  # "success" or "error"
    result_type: str = ""  # "streams", "matrix", "vector"
    streams: list[LokiStream] = field(default_factory=list)
    # For metric queries (rate, count_over_time, etc.)
    metric_result: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed_seconds: float = 0.0

    @property
    def total_lines(self) -> int:
        return sum(len(s.entries) for s in self.streams)


class LokiConnector:
    """Thin client for the Loki HTTP v1 query API."""

    def __init__(
        self,
        url: str,
        token: str = "",
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl_ctx: ssl.SSLContext | None = None
        if not verify_ssl:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        qs = urllib.parse.urlencode(params)
        full_url = f"{self.url}{path}?{qs}"
        logger.debug("GET %s", full_url)
        req = urllib.request.Request(full_url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"Loki API error {exc.code} for {full_url}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Loki connection error for {full_url}: {exc.reason}"
            ) from exc

    # ── Public query methods ────────────────────────────────────────────────────

    def query(self, logql: str, limit: int = 100, time_: str | None = None) -> LokiResponse:
        """Execute an instant log query."""
        params: dict[str, str] = {"query": logql, "limit": str(limit)}
        if time_:
            params["time"] = time_

        t0 = time.perf_counter()
        raw = self._get("/loki/api/v1/query", params)
        elapsed = time.perf_counter() - t0
        return self._parse(raw, elapsed)

    def query_range(
        self,
        logql: str,
        start: str,
        end: str,
        limit: int = 100,
        step: str | None = None,
    ) -> LokiResponse:
        """Execute a range log query.

        Parameters
        ----------
        logql : str
            LogQL expression — can be a log query or a metric query
            (e.g. ``rate({app="superset"} |= "error" [5m])``).
        start, end : str
            RFC3339 timestamps or Unix epoch (nanoseconds for Loki).
        limit : int
            Max log lines to return.
        step : str, optional
            Query step for metric queries (e.g. "60s").
        """
        params: dict[str, str] = {
            "query": logql,
            "start": start,
            "end": end,
            "limit": str(limit),
        }
        if step:
            params["step"] = step

        t0 = time.perf_counter()
        raw = self._get("/loki/api/v1/query_range", params)
        elapsed = time.perf_counter() - t0
        return self._parse(raw, elapsed)

    def label_names(self) -> list[str]:
        """List all label names."""
        raw = self._get("/loki/api/v1/labels", {})
        if raw.get("status") == "success":
            return raw.get("data", [])
        return []

    def label_values(self, label: str) -> list[str]:
        """List all values for a given label."""
        raw = self._get(f"/loki/api/v1/label/{label}/values", {})
        if raw.get("status") == "success":
            return raw.get("data", [])
        return []

    # ── Response parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _parse(raw: dict[str, Any], elapsed: float) -> LokiResponse:
        if raw.get("status") != "success":
            return LokiResponse(
                status="error",
                error=raw.get("error", raw.get("message", "Unknown error")),
                elapsed_seconds=elapsed,
            )

        data = raw.get("data", {})
        result_type = data.get("resultType", "")
        result = data.get("result", [])

        response = LokiResponse(
            status="success",
            result_type=result_type,
            elapsed_seconds=elapsed,
        )

        if result_type == "streams":
            for stream_data in result:
                labels = stream_data.get("stream", {})
                values = stream_data.get("values", [])
                response.streams.append(LokiStream(
                    labels=labels,
                    entries=[(ts, line) for ts, line in values],
                ))
        elif result_type in ("matrix", "vector"):
            # Metric query result — same format as Prometheus
            response.metric_result = result

        return response

    # ── Grafana proxy factory ──────────────────────────────────────────────────

    @classmethod
    def via_grafana(
        cls,
        grafana_url: str,
        grafana_token: str,
        datasource_uid: str | None = None,
        timeout: int = 30,
    ) -> "LokiConnector":
        """Create a connector that proxies through Grafana's datasource API.

        Auto-discovers the first Loki-type datasource if *datasource_uid* is None.
        """
        grafana_url = grafana_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {grafana_token}",
        }

        if datasource_uid is None:
            datasource_uid = cls._discover_loki_uid(grafana_url, headers, timeout)
            if datasource_uid is None:
                raise RuntimeError(
                    f"No Loki datasource found in Grafana at {grafana_url}. "
                    "Configure a Loki datasource in Grafana or set LOKI_URL directly."
                )

        proxy_url = f"{grafana_url}/api/datasources/proxy/uid/{datasource_uid}"
        logger.info("Using Grafana Loki proxy: %s", proxy_url)
        return cls(url=proxy_url, token=grafana_token, timeout=timeout)

    @staticmethod
    def _discover_loki_uid(
        grafana_url: str,
        headers: dict[str, str],
        timeout: int,
    ) -> str | None:
        """Find the UID of the first Loki-type datasource in Grafana."""
        url = f"{grafana_url}/api/datasources"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                datasources = json.loads(resp.read().decode())
        except Exception as exc:
            logger.warning("Failed to list Grafana datasources at %s: %s", url, exc)
            return None

        for ds in datasources:
            ds_type = ds.get("type", "").lower()
            if ds_type in ("loki",):
                uid = ds.get("uid")
                logger.info(
                    "Auto-discovered Loki datasource: %s (uid=%s)",
                    ds.get("name", "?"), uid,
                )
                return uid

        return None
