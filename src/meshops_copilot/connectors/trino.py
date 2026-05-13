"""Trino REST API connector.

Uses the stdlib ``urllib`` only — no trino-python-client dependency —
so the package remains installable on any Python 3.12 environment.

Authentication
--------------
Trino supports password authentication via HTTP Basic Auth.  When a
``password`` is provided the connector sends an ``Authorization: Basic``
header on every request alongside ``X-Trino-User``.  Leave ``password``
empty (the default) for open / LDAP-less clusters.

For HTTPS deployments simply pass an ``https://`` URL; ``urllib`` will
handle TLS negotiation automatically.  To skip certificate verification
(e.g. self-signed certs in a dev cluster) set ``verify_ssl=False``.
"""

from __future__ import annotations

import base64
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request


class TrinoConnector:
    """Thin client for the Trino v1 REST API."""

    def __init__(
        self,
        url: str,
        user: str = "meshops",
        password: str = "",
        timeout: int = 180,
        verify_ssl: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self._ssl_ctx = None if verify_ssl else ssl.create_default_context()
        if self._ssl_ctx:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ── Header factory ─────────────────────────────────────────────────────────

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        """Return the common request headers, including auth if configured."""
        h: dict[str, str] = {"X-Trino-User": self.user}
        if self.password:
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {token}"
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _open(self, req: urllib.request.Request, timeout: int | None = None) -> bytes:
        """Open a request, respecting the SSL context if set."""
        kw: dict = {"timeout": timeout or self.timeout}
        if self._ssl_ctx:
            kw["context"] = self._ssl_ctx
        with urllib.request.urlopen(req, **kw) as resp:
            return resp.read()

    # ── Query execution ────────────────────────────────────────────────────────

    def execute(self, sql: str, query_timeout: int | None = None) -> tuple[float | None, dict, str | None]:
        """Submit a SQL statement and poll until completion.

        Args:
            sql: The SQL statement to execute.
            query_timeout: Maximum total seconds to wait for the query to
                finish.  Defaults to ``self.timeout``.  A ``ConnectorError``
                is raised if the deadline is exceeded.

        Returns:
            (elapsed_seconds, final_stats_dict, error_message_or_None)
        """
        timeout = query_timeout if query_timeout is not None else self.timeout
        req = urllib.request.Request(
            f"{self.url}/v1/statement",
            data=sql.strip().encode(),
            headers=self._headers("text/plain"),
            method="POST",
        )

        t_start = time.monotonic()
        deadline = t_start + timeout
        try:
            body = json.loads(self._open(req))
        except Exception as exc:
            return None, {}, str(exc)

        next_uri: str | None = body.get("nextUri")
        final_stats: dict = body.get("stats", {})

        while next_uri:
            if time.monotonic() > deadline:
                return time.monotonic() - t_start, final_stats, f"Query timed out after {timeout}s"
            time.sleep(0.05)
            try:
                poll = urllib.request.Request(next_uri, headers=self._headers())
                body = json.loads(self._open(poll))
            except urllib.error.HTTPError as exc:
                if exc.code == 503:
                    time.sleep(1)
                    continue
                return time.monotonic() - t_start, final_stats, f"HTTP {exc.code}"
            except Exception as exc:
                return time.monotonic() - t_start, final_stats, str(exc)

            final_stats = body.get("stats", final_stats)
            next_uri = body.get("nextUri")

            if body.get("error"):
                msg = body["error"].get("message", "unknown error")
                return time.monotonic() - t_start, final_stats, msg

            if final_stats.get("state") in ("FINISHED", "FAILED") and not next_uri:
                break

        return time.monotonic() - t_start, final_stats, None

    def query_rows(self, sql: str, query_timeout: int | None = None) -> list[dict]:
        """Execute SQL and return result rows as a list of dicts.

        Unlike ``execute()``, this method collects and returns the actual row
        data from the query response.  Used by schema discovery.

        Args:
            sql: The SQL statement to execute.
            query_timeout: Maximum total seconds to wait.  Defaults to
                ``self.timeout``.

        Raises:
            ConnectorError: if the query fails, the server returns an error,
                or the deadline is exceeded.
        """
        from meshops_copilot.core.errors import ConnectorError

        timeout = query_timeout if query_timeout is not None else self.timeout
        req = urllib.request.Request(
            f"{self.url}/v1/statement",
            data=sql.strip().encode(),
            headers=self._headers("text/plain"),
            method="POST",
        )

        try:
            body = json.loads(self._open(req))
        except Exception as exc:
            raise ConnectorError(f"Query submission failed: {exc}") from exc

        deadline = time.monotonic() + timeout
        columns: list[str] = []
        rows: list[dict] = []

        def _extract(b: dict) -> None:
            nonlocal columns
            if not columns and "columns" in b:
                columns = [c["name"] for c in b["columns"]]
            for row in b.get("data", []):
                rows.append(dict(zip(columns, row)))

        _extract(body)
        next_uri: str | None = body.get("nextUri")

        while next_uri:
            if time.monotonic() > deadline:
                raise ConnectorError(f"Query timed out after {timeout}s")
            time.sleep(0.05)
            try:
                poll = urllib.request.Request(next_uri, headers=self._headers())
                body = json.loads(self._open(poll))
            except urllib.error.HTTPError as exc:
                if exc.code == 503:
                    time.sleep(1)
                    continue
                raise ConnectorError(f"HTTP {exc.code}") from exc
            except Exception as exc:
                raise ConnectorError(str(exc)) from exc

            if body.get("error"):
                msg = body["error"].get("message", "unknown error")
                raise ConnectorError(msg)

            _extract(body)
            next_uri = body.get("nextUri")

            if body.get("stats", {}).get("state") in ("FINISHED", "FAILED") and not next_uri:
                break

        return rows

    # ── Cluster metadata ───────────────────────────────────────────────────────

    def cluster_stats(self) -> dict:
        """Fetch /v1/cluster."""
        try:
            req = urllib.request.Request(
                f"{self.url}/v1/cluster",
                headers=self._headers(),
            )
            return json.loads(self._open(req, timeout=10))
        except Exception:
            return {}

    def nodes(self) -> list[dict]:
        """Fetch /v1/node."""
        try:
            req = urllib.request.Request(
                f"{self.url}/v1/node",
                headers=self._headers(),
            )
            return json.loads(self._open(req, timeout=10))
        except Exception:
            return []

    # ── Docker resource sampling ───────────────────────────────────────────────

    @staticmethod
    def docker_stats(container: str = "trino") -> dict:
        """Return CPU% and memory for a Docker container via ``docker stats``."""
        try:
            result = subprocess.run(
                [
                    "docker", "stats", "--no-stream", "--format",
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
