"""Superset REST API connector.

Uses the stdlib ``urllib`` only — no external dependencies — consistent with
the TrinoConnector approach.

Authentication
--------------
Superset uses JWT bearer tokens.  Call ``login()`` once to obtain a token;
it is cached on the instance and sent as ``Authorization: Bearer <token>`` on
every subsequent request.
"""

from __future__ import annotations

import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request

from meshops_copilot.core.errors import ConnectorError


class SupersetConnector:
    """Thin client for the Superset v1 REST API."""

    def __init__(
        self,
        url: str,
        user: str = "admin",
        password: str = "admin",
        timeout: int = 60,
        verify_ssl: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self._token: str | None = None
        self._csrf_token: str | None = None
        self._ssl_ctx: ssl.SSLContext | None = None
        if not verify_ssl:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── Auth ───────────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Authenticate and cache the JWT access token and CSRF token."""
        payload = json.dumps(
            {
                "username": self.user,
                "password": self.password,
                "provider": "db",
                "refresh": True,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.url}/api/v1/security/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            body = json.loads(self._open(req, timeout=30))
        except Exception as exc:
            raise ConnectorError(f"Superset login failed: {exc}") from exc

        token = body.get("access_token")
        if not token:
            raise ConnectorError(
                f"Superset login response missing access_token: {body}"
            )
        self._token = token

        # Fetch CSRF token — required for POST requests on instances where
        # WTF_CSRF_ENABLED is True (typical in production deployments).
        try:
            csrf_req = urllib.request.Request(
                f"{self.url}/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            csrf_body = json.loads(self._open(csrf_req, timeout=10))
            self._csrf_token: str | None = csrf_body.get("result")
        except Exception:
            self._csrf_token = None

    def _ensure_logged_in(self) -> None:
        if self._token is None:
            self.login()

    # ── Low-level HTTP ─────────────────────────────────────────────────────────

    def _headers(self, content_type: str | None = None, csrf: bool = False) -> dict[str, str]:
        self._ensure_logged_in()
        h: dict[str, str] = {"Authorization": f"Bearer {self._token}"}
        if content_type:
            h["Content-Type"] = content_type
        if csrf and self._csrf_token:
            h["X-CSRFToken"] = self._csrf_token
            h["Referer"] = self.url
        return h

    def _open(self, req: urllib.request.Request, timeout: int | None = None) -> bytes:
        kw: dict = {"timeout": timeout or self.timeout}
        if self._ssl_ctx:
            kw["context"] = self._ssl_ctx
        with urllib.request.urlopen(req, **kw) as resp:
            return resp.read()

    # ── Chart data ─────────────────────────────────────────────────────────────

    def chart_data(
        self, query_context: dict
    ) -> tuple[float | None, dict, str | None]:
        """POST /api/v1/chart/data with a full query_context.

        Returns:
            (elapsed_seconds, result_stats_dict, error_message_or_None)

        ``result_stats`` will contain ``{"row_count": N}`` on success.
        """
        self._ensure_logged_in()
        payload = json.dumps(query_context).encode()
        req = urllib.request.Request(
            f"{self.url}/api/v1/chart/data",
            data=payload,
            headers=self._headers("application/json", csrf=True),
            method="POST",
        )
        t_start = time.monotonic()
        try:
            raw = self._open(req)
        except urllib.error.HTTPError as exc:
            elapsed = time.monotonic() - t_start
            try:
                body = json.loads(exc.read())
                # Superset ≥2 wraps errors in {"errors": [{"message": ...}]}
                if "errors" in body and body["errors"]:
                    msg = body["errors"][0].get("message", str(exc))
                else:
                    msg = body.get("message", str(exc))
            except Exception:
                msg = f"HTTP Error {exc.code}: {exc.reason}"
            return elapsed, {}, msg
        except Exception as exc:
            return time.monotonic() - t_start, {}, str(exc)

        elapsed = time.monotonic() - t_start
        try:
            body = json.loads(raw)
        except Exception as exc:
            return elapsed, {}, f"JSON decode error: {exc}"

        # Superset wraps results in {"result": [...]}
        results = body.get("result", [])
        if not results:
            msg = body.get("message") or body.get("error")
            if msg:
                return elapsed, {}, str(msg)
            return elapsed, {}, "Empty result from /api/v1/chart/data"

        first = results[0]
        if first.get("status") == "failed":
            err = first.get("error") or first.get("message", "unknown chart error")
            return elapsed, {}, str(err)

        stats = {
            "row_count": first.get("rowcount", 0),
            "status": first.get("status", "unknown"),
        }
        return elapsed, stats, None

    # ── Dashboard & chart metadata ─────────────────────────────────────────────

    def list_dashboards(self) -> list[dict]:
        """GET /api/v1/dashboard — return a list of dashboard dicts."""
        self._ensure_logged_in()
        req = urllib.request.Request(
            f"{self.url}/api/v1/dashboard/?q=(page_size:100)",
            headers=self._headers(),
        )
        try:
            body = json.loads(self._open(req))
            return body.get("result", [])
        except Exception as exc:
            raise ConnectorError(f"list_dashboards failed: {exc}") from exc

    def get_dashboard(self, dashboard_id: int) -> dict:
        """GET /api/v1/dashboard/{id} — return dashboard metadata."""
        self._ensure_logged_in()
        req = urllib.request.Request(
            f"{self.url}/api/v1/dashboard/{dashboard_id}",
            headers=self._headers(),
        )
        try:
            body = json.loads(self._open(req))
            return body.get("result", {})
        except Exception as exc:
            raise ConnectorError(
                f"get_dashboard({dashboard_id}) failed: {exc}"
            ) from exc

    def list_charts(self, dashboard_id: int | None = None, max_items: int = 500) -> list[dict]:
        """GET /api/v1/chart — return charts with ``params`` and metadata.

        Paginates automatically up to ``max_items`` results.
        Optionally filtered to a single dashboard (client-side).
        """
        self._ensure_logged_in()
        results: list[dict] = []
        page = 0
        page_size = min(max_items, 100)

        while len(results) < max_items:
            url = f"{self.url}/api/v1/chart/?q=(page_size:{page_size},page:{page})"
            req = urllib.request.Request(url, headers=self._headers())
            try:
                body = json.loads(self._open(req))
            except Exception as exc:
                raise ConnectorError(f"list_charts failed: {exc}") from exc

            page_results = body.get("result", [])
            results.extend(page_results)

            # Stop when we have fetched all available charts.
            if len(page_results) < page_size or len(results) >= body.get("count", len(results)):
                break
            page += 1

        if dashboard_id is not None:
            results = [
                c for c in results
                if dashboard_id in (d.get("id") for d in c.get("dashboards", []))
            ]

        return results[:max_items]

    def get_chart(self, chart_id: int) -> dict:
        """GET /api/v1/chart/{id} — return full chart detail including ``query_context``.

        Use this when the list endpoint's ``query_context`` is None; the detail
        endpoint sometimes has a stored context from the last render.
        """
        self._ensure_logged_in()
        req = urllib.request.Request(
            f"{self.url}/api/v1/chart/{chart_id}",
            headers=self._headers(),
        )
        try:
            body = json.loads(self._open(req))
            return body.get("result", {})
        except Exception as exc:
            raise ConnectorError(f"get_chart({chart_id}) failed: {exc}") from exc

    # ── Docker resource sampling ───────────────────────────────────────────────

    @staticmethod
    def docker_stats(container: str = "superset") -> dict:
        """Return CPU% and memory for a Docker container via ``docker stats``."""
        try:
            result = subprocess.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
                    container,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            parts = result.stdout.strip().split("\t")
            if len(parts) == 3:
                return {"cpu": parts[0], "mem_usage": parts[1], "mem_perc": parts[2]}
        except Exception:
            pass
        return {}
