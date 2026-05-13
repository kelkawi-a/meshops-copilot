"""DataHub MCP (Model Context Protocol) connector.

Communicates with a DataHub MCP server subprocess over stdio using the
official ``mcp`` Python SDK.

Usage
-----
    with DataHubMCPConnector(gms_url="https://datahub.example.com", token="...") as conn:
        datasets = conn.search_datasets(count=200)
        for ds in datasets:
            usage   = conn.get_usage_stats(ds["urn"])
            lineage = conn.get_lineage(ds["urn"])

The connector spawns ``uvx mcp-server-datahub`` once and keeps the async
session alive for the duration of the ``with`` block.  Authentication is
handled via environment variables (``DATAHUB_GMS_URL`` / ``DATAHUB_TOKEN``)
passed to the subprocess — not as CLI flags.

Tool names (mcp-server-datahub ≥ 0.3)
--------------------------------------
- ``search``            – full-text search; filter syntax ``entity_type = DATASET``
- ``get_entities``      – fetch full entity metadata by URN(s)
- ``get_dataset_queries`` – SQL queries run against a dataset (usage proxy)
- ``get_lineage``       – upstream / downstream lineage graph

Optional dependency
-------------------
The ``mcp`` package must be installed::

    pip install mcp
    # or
    uv pip install '.[datahub]'
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from meshops_copilot.core.logging import get_logger

log = get_logger(__name__)


class DataHubMCPConnector:
    """DataHub MCP client with a persistent subprocess session.

    Must be used as a context manager::

        with DataHubMCPConnector(gms_url=..., token=...) as conn:
            datasets = conn.search_datasets()

    The constructor accepts an optional ``server_command`` list to override
    the default ``uvx mcp-server-datahub`` invocation — useful in tests or
    when the MCP server is installed differently.
    """

    def __init__(
        self,
        gms_url: str,
        token: str = "",
        server_command: list[str] | None = None,
        timeout: int = 60,
    ) -> None:
        self.gms_url = gms_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._server_command: list[str] = server_command or ["uvx", "mcp-server-datahub"]
        self._available_tools: set[str] = set()

        # Async runtime — runs in a dedicated background thread so that the
        # fully-sync caller does not need to be inside an event loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None    # mcp.ClientSession (set in _connect)
        self._stdio_cm: Any = None   # async CM returned by stdio_client
        self._session_cm: Any = None  # async CM returned by ClientSession

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _subprocess_env(self) -> dict[str, str]:
        """Build the environment dict for the MCP server subprocess.

        Merges the current process environment with DataHub credentials so
        the server can call ``DataHubClient.from_env()``.

        The ``acryl-datahub`` SDK reads ``DATAHUB_GMS_TOKEN`` for auth; we
        also keep ``DATAHUB_TOKEN`` for forward-compatibility and in case the
        caller set only one of them.
        """
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self.gms_url
        if self.token:
            # SDK uses DATAHUB_GMS_TOKEN; accept DATAHUB_TOKEN as an alias too
            env["DATAHUB_GMS_TOKEN"] = self.token
            env["DATAHUB_TOKEN"] = self.token
        # If no token was passed to constructor, try to forward any token already
        # in the environment under either name.
        elif not env.get("DATAHUB_GMS_TOKEN"):
            if fallback := env.get("DATAHUB_TOKEN"):
                env["DATAHUB_GMS_TOKEN"] = fallback
        return env

    def __enter__(self) -> "DataHubMCPConnector":
        try:
            from mcp import ClientSession, StdioServerParameters  # noqa: F401
            from mcp.client.stdio import stdio_client              # noqa: F401
        except ModuleNotFoundError:
            raise RuntimeError(
                "mcp package is not installed. "
                "Run: pip install mcp  or  uv pip install '.[datahub]'"
            )

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="datahub-mcp-loop",
        )
        self._thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=self.timeout)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                future.result(timeout=10)
            except Exception as exc:
                log.debug("MCP disconnect error: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop.close()
        self._loop = None
        self._thread = None
        self._session = None

    # ── Async internals ────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=self._server_command[0],
            args=self._server_command[1:],
            env=self._subprocess_env(),
        )
        self._stdio_cm = stdio_client(server_params)
        read, write = await self._stdio_cm.__aenter__()

        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        # Discover which tools are available so callers can skip gracefully.
        try:
            tools_result = await self._session.list_tools()
            self._available_tools = {t.name for t in tools_result.tools}
            log.debug("DataHub MCP tools available: %s", self._available_tools)
        except Exception as exc:
            log.warning("Could not list MCP tools: %s", exc)

    async def _disconnect(self) -> None:
        for cm in (self._session_cm, self._stdio_cm):
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _call_tool_async(self, name: str, arguments: dict) -> Any:
        return await self._session.call_tool(name, arguments)

    # ── Sync dispatch ──────────────────────────────────────────────────────────

    def _call(self, name: str, arguments: dict) -> Any:
        """Dispatch a tool call to the background loop; return the parsed result."""
        if self._available_tools and name not in self._available_tools:
            log.debug("MCP tool %r not advertised — skipping.", name)
            return None
        if self._loop is None:
            raise RuntimeError("Connector is not open — use it as a context manager.")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(name, arguments), self._loop
        )
        try:
            result = future.result(timeout=self.timeout)
        except Exception as exc:
            log.warning("MCP tool %r failed: %s", name, exc)
            return None
        return self._parse(result)

    @staticmethod
    def _parse(result: Any) -> Any:
        """Extract the first text content block and JSON-decode it if possible."""
        if result is None:
            return None
        content = getattr(result, "content", None)
        if not content:
            return result
        for item in content:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        return None

    # ── Domain methods ─────────────────────────────────────────────────────────

    def search_datasets(
        self,
        query: str = "*",
        domain: str | None = None,
        platform: str | None = None,
        count: int = 200,
    ) -> list[dict]:
        """Search DataHub for datasets.

        Returns a list of dicts, each with at least a ``urn`` key and
        typically ``properties.name`` and ``platform``.

        Uses the ``search`` MCP tool with ``filter="entity_type = DATASET"``.
        Domain and platform filters are appended to the filter string when
        provided.
        """
        # Build filter string (mcp-server-datahub mini SQL syntax)
        filter_parts = ["entity_type = DATASET"]
        if domain:
            filter_parts.append(f"domain = {domain}")
        if platform:
            filter_parts.append(f"platform = {platform}")
        filter_str = " AND ".join(filter_parts)

        # Build search query — exclude common system/internal schemas
        # that are never data product candidates (information_schema, pg_catalog, etc.)
        _SYSTEM_SCHEMA_EXCLUSIONS = (
            "information_schema",
            "pg_catalog",
            "pg_toast",
            "pg_temp",
            "sys",
            "__internal",
        )
        if query == "*":
            exclusions = " ".join(
                f"NOT {s}" for s in _SYSTEM_SCHEMA_EXCLUSIONS
            )
            effective_query = f"/q {exclusions}"
        else:
            effective_query = query

        # The search tool caps num_results at 50; page if more are needed.
        page_size = min(count, 50)
        all_entities: list[dict] = []
        offset = 0

        while len(all_entities) < count:
            result = self._call("search", {
                "query": effective_query,
                "filter": filter_str,
                "num_results": page_size,
                "offset": offset,
            })
            if not isinstance(result, dict):
                break

            page = (
                result.get("searchResults")
                or result.get("entities")
                or result.get("results")
                or []
            )
            if not page:
                break

            for hit in page:
                # Each search result is {"entity": {...}, "matchedFields": [...]}
                entity = hit.get("entity", hit) if isinstance(hit, dict) else {}
                if entity:
                    all_entities.append(entity)

            total = result.get("total", 0)
            offset += len(page)
            if offset >= total or offset >= count:
                break

        return all_entities[:count]

    def get_entity(self, urn: str) -> dict:
        """Return full entity metadata for a dataset URN.

        Uses the ``get_entities`` MCP tool with a single URN (returns a dict,
        not a list, per the tool's single-URN behaviour).
        """
        result = self._call("get_entities", {"urns": urn})
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result:
            return result[0]
        return {}

    def get_entities_batch(self, urns: list[str]) -> list[dict]:
        """Fetch full entity metadata for multiple URNs in a single MCP call.

        Returns a list of entity dicts in the same order as ``urns``.  Any URN
        that cannot be resolved returns an empty dict at that position.
        """
        if not urns:
            return []
        result = self._call("get_entities", {"urns": urns})
        if isinstance(result, list):
            # Pad / fill any missing entries so len(result) == len(urns)
            entities = list(result)
            while len(entities) < len(urns):
                entities.append({})
            return entities[:len(urns)]
        if isinstance(result, dict):
            return [result] + [{} for _ in urns[1:]]
        return [{} for _ in urns]

    def get_usage_stats(self, urn: str, window_days: int = 30) -> dict:
        """Return approximate usage statistics for a dataset.

        Uses ``get_dataset_queries`` as a proxy — the OSS DataHub MCP server
        does not expose a dedicated usage-stats tool.

        Returned keys: ``query_count`` (total queries indexed), ``unique_users``
        (always 0 for OSS — usage-user breakdown is a Cloud-only feature).
        """
        result = self._call("get_dataset_queries", {"urn": urn, "count": 100})
        if not isinstance(result, dict):
            return {}
        total = (
            result.get("total")
            or result.get("totalSqlQueries")
            or len(result.get("queries", []))
            or 0
        )
        return {"query_count": int(total), "unique_users": 0}

    def search_dashboards(
        self,
        query: str = "*",
        platform: str | None = None,
        domain: str | None = None,
        count: int = 200,
    ) -> list[dict]:
        """Search DataHub for dashboard entities.

        Returns a list of dicts, each with at least a ``urn`` key and
        typically ``dashboardProperties.name`` and ``platform``.

        Uses the ``search`` MCP tool with ``filter="entity_type = DASHBOARD"``.
        """
        filter_parts = ["entity_type = DASHBOARD"]
        if platform:
            filter_parts.append(f"platform = {platform}")
        if domain:
            filter_parts.append(f"domain = {domain}")
        filter_str = " AND ".join(filter_parts)

        page_size = min(count, 50)
        all_entities: list[dict] = []
        offset = 0

        while len(all_entities) < count:
            result = self._call("search", {
                "query": query,
                "filter": filter_str,
                "num_results": page_size,
                "offset": offset,
            })
            if not isinstance(result, dict):
                break

            page = (
                result.get("searchResults")
                or result.get("entities")
                or result.get("results")
                or []
            )
            if not page:
                break

            for hit in page:
                entity = hit.get("entity", hit) if isinstance(hit, dict) else {}
                if entity:
                    all_entities.append(entity)

            total = result.get("total", 0)
            offset += len(page)
            if offset >= total or offset >= count:
                break

        return all_entities[:count]

    def get_lineage(
        self,
        urn: str,
        direction: str = "downstream",
        depth: int = 3,
    ) -> dict:
        """Return the lineage graph for a dataset.

        Uses the ``get_lineage`` MCP tool (``upstream=False`` for downstream).

        Returned structure::

            {
              "searchResults": [
                {"entity": {"type": "DATASET|DASHBOARD|CHART", "urn": "..."}, ...},
                ...
              ]
            }
        """
        upstream = direction.lower() != "downstream"
        result = self._call("get_lineage", {
            "urn": urn,
            "upstream": upstream,
            "max_hops": depth,
            "max_results": 50,
        })
        return result if isinstance(result, dict) else {}
