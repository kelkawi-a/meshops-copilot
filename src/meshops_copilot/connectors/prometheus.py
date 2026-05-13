"""Prometheus HTTP API connector.

Talks directly to the Prometheus HTTP API **or** proxies through Grafana's
datasource proxy (``/api/datasources/proxy/<uid>/``).  Uses stdlib ``urllib``
to remain consistent with other connectors in this package.

Grafana proxy mode
------------------
When a Grafana URL and service-account token are provided the connector can
auto-discover the first Prometheus-type datasource and route all queries
through ``<grafana_url>/api/datasources/proxy/<uid>/api/v1/…``.  This avoids
needing direct network access to the Prometheus pod.
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
class PrometheusResponse:
    """Parsed response from the Prometheus HTTP API."""

    status: str  # "success" or "error"
    result_type: str = ""  # "matrix", "vector", "scalar", "string"
    result: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed_seconds: float = 0.0


class PrometheusConnector:
    """Thin client for the Prometheus HTTP v1 query API.

    Can target either a standalone Prometheus instance or a Grafana
    datasource proxy endpoint.
    """

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
        """Issue a GET request and return the decoded JSON body."""
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
                f"Prometheus API error {exc.code} for {full_url}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Prometheus connection error for {full_url}: {exc.reason}"
            ) from exc

    # ── Public query methods ────────────────────────────────────────────────────

    def query(self, promql: str, time_: str | None = None) -> PrometheusResponse:
        """Execute an instant query.

        Parameters
        ----------
        promql : str
            PromQL expression.
        time_ : str, optional
            Evaluation timestamp (RFC3339 or Unix epoch).  Defaults to now.
        """
        params: dict[str, str] = {"query": promql}
        if time_:
            params["time"] = time_

        t0 = time.perf_counter()
        raw = self._get("/api/v1/query", params)
        elapsed = time.perf_counter() - t0

        return self._parse(raw, elapsed)

    def query_range(
        self,
        promql: str,
        start: str,
        end: str,
        step: str = "60s",
    ) -> PrometheusResponse:
        """Execute a range query.

        Parameters
        ----------
        promql : str
            PromQL expression.
        start : str
            Start time (RFC3339 or Unix epoch).
        end : str
            End time (RFC3339 or Unix epoch).
        step : str
            Query resolution step (e.g. "60s", "5m").
        """
        params = {"query": promql, "start": start, "end": end, "step": step}

        t0 = time.perf_counter()
        raw = self._get("/api/v1/query_range", params)
        elapsed = time.perf_counter() - t0

        return self._parse(raw, elapsed)

    def label_values(
        self,
        label: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[str]:
        """Fetch all values for a given label name, optionally time-bounded."""
        params: dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        raw = self._get(f"/api/v1/label/{label}/values", params)
        if raw.get("status") == "success":
            return raw.get("data", [])
        return []

    def metric_names(
        self,
        start: str | None = None,
        end: str | None = None,
    ) -> list[str]:
        """List all metric names (__name__ label values), optionally time-bounded."""
        return self.label_values("__name__", start=start, end=end)

    def series(
        self,
        match: list[str],
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, str]]:
        """Find series matching one or more label selectors.

        Uses the ``/api/v1/series`` endpoint which is well-supported by
        Mimir and other Prometheus-compatible backends.

        Parameters
        ----------
        match : list[str]
            Label selectors, e.g. ``['{__name__=~"superset_.*"}']``.
        start, end : str, optional
            Time bounds (RFC3339 or Unix epoch).
        """
        # Build query string with repeated match[] params (urllib doesn't
        # natively support repeated keys, so we assemble manually).
        qs_parts = [urllib.parse.urlencode({"match[]": m}) for m in match]
        if start:
            qs_parts.append(urllib.parse.urlencode({"start": start}))
        if end:
            qs_parts.append(urllib.parse.urlencode({"end": end}))
        qs = "&".join(qs_parts)

        full_url = f"{self.url}/api/v1/series?{qs}"
        logger.debug("GET %s", full_url)
        req = urllib.request.Request(full_url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"Prometheus series API error {exc.code} for {full_url}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Prometheus series connection error for {full_url}: {exc.reason}"
            ) from exc

        if raw.get("status") == "success":
            return raw.get("data", [])
        return []

    def metadata(self) -> dict[str, list[dict[str, str]]]:
        """Fetch metric metadata (type, help text)."""
        raw = self._get("/api/v1/targets/metadata", {})
        if raw.get("status") == "success":
            return raw.get("data", {})
        return {}

    # ── Response parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _parse(raw: dict[str, Any], elapsed: float) -> PrometheusResponse:
        if raw.get("status") != "success":
            return PrometheusResponse(
                status="error",
                error=raw.get("error", "Unknown error"),
                elapsed_seconds=elapsed,
            )
        data = raw.get("data", {})
        return PrometheusResponse(
            status="success",
            result_type=data.get("resultType", ""),
            result=data.get("result", []),
            elapsed_seconds=elapsed,
        )

    # ── Grafana proxy factory ──────────────────────────────────────────────────

    @classmethod
    def via_grafana(
        cls,
        grafana_url: str,
        grafana_token: str,
        datasource_uid: str | None = None,
        timeout: int = 30,
    ) -> "PrometheusConnector":
        """Create a connector that proxies through Grafana's datasource API.

        If *datasource_uid* is ``None`` the first Prometheus-type datasource
        is auto-discovered via ``GET /api/datasources``.
        """
        grafana_url = grafana_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {grafana_token}",
        }

        if datasource_uid is None:
            datasource_uid = cls._discover_prometheus_uid(grafana_url, headers, timeout)
            if datasource_uid is None:
                raise RuntimeError(
                    f"No Prometheus datasource found in Grafana at {grafana_url}. "
                    "Set PROMETHEUS_URL to a reachable Prometheus instance or add a "
                    "Prometheus datasource to Grafana."
                )

        proxy_url = f"{grafana_url}/api/datasources/proxy/uid/{datasource_uid}"
        logger.info("Using Grafana datasource proxy: %s", proxy_url)
        return cls(url=proxy_url, token=grafana_token, timeout=timeout)

    @staticmethod
    def _discover_prometheus_uid(
        grafana_url: str,
        headers: dict[str, str],
        timeout: int,
    ) -> str | None:
        """Find the UID of the first Prometheus-type datasource in Grafana."""
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
            if ds_type in ("prometheus", "prometheus-datasource"):
                uid = ds.get("uid")
                logger.info(
                    "Auto-discovered Prometheus datasource: %s (uid=%s)",
                    ds.get("name", "?"), uid,
                )
                return uid

        return None
