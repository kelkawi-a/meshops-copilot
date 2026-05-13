"""Signal collection from DataHub MCP.

``SignalCollector`` fetches entity metadata, and optionally usage statistics
and lineage, for a list of dataset URNs.

Entity metadata is fetched in a **single batched MCP call** (``get_entities``
accepts an array of URNs) so N datasets cost 1 round-trip instead of N.
Usage and lineage are expensive per-dataset calls and are therefore opt-in via
``collect_usage`` and ``collect_lineage`` constructor flags.

Individual fetch failures are captured in ``DatasetSignals.collection_errors``
so one bad dataset does not abort the whole run (graceful degradation).
"""

from __future__ import annotations

import concurrent.futures
from typing import TYPE_CHECKING

from meshops_copilot.core.logging import get_logger
from meshops_copilot.skills.data_product_discovery.models import DatasetSignals

if TYPE_CHECKING:
    from meshops_copilot.connectors.datahub_mcp import DataHubMCPConnector

log = get_logger(__name__)


class SignalCollector:
    """Collect all signals for a batch of dataset URNs via DataHub MCP."""

    def __init__(
        self,
        connector: "DataHubMCPConnector",
        max_workers: int = 8,
        usage_window_days: int = 30,
        collect_usage: bool = False,
        collect_lineage: bool = False,
    ) -> None:
        self._conn = connector
        self._max_workers = max_workers
        self._window = usage_window_days
        self._collect_usage = collect_usage
        self._collect_lineage = collect_lineage

    def collect_all(self, urns: list[str]) -> list[DatasetSignals]:
        """Collect signals for all URNs and return results.

        Entity metadata is fetched in a single batched MCP call.
        Usage and lineage (if enabled) are fetched in parallel per-dataset.
        """
        if not urns:
            return []

        # ── 1. Batch-fetch all entity metadata in ONE MCP call ─────────────
        fetch_errors: dict[str, str] = {}
        try:
            entities = self._conn.get_entities_batch(urns)
        except Exception as exc:
            log.warning("Batch get_entities failed: %s — falling back to per-URN", exc)
            entities = []
            for urn in urns:
                try:
                    entities.append(self._conn.get_entity(urn))
                except Exception as e:
                    log.debug("get_entity failed for %s: %s", urn, e)
                    fetch_errors[urn] = f"entity: {e}"
                    entities.append({})

        # Build initial signals from entity data
        results: list[DatasetSignals] = []
        for urn, entity in zip(urns, entities):
            signals = DatasetSignals(urn=urn, name=urn)
            errors: list[str] = []
            if urn in fetch_errors:
                errors.append(fetch_errors[urn])
            self._apply_entity(urn, entity, signals, errors)
            signals.collection_errors = errors
            results.append(signals)

        # ── 2. Optional: usage + lineage in parallel ────────────────────────
        if not self._collect_usage and not self._collect_lineage:
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._enrich_one, signals): signals
                for signals in results
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    sig = futures[future]
                    log.warning("Enrichment failed for %s: %s", sig.urn, exc)
                    sig.collection_errors.append(str(exc))

        return results

    # ── Entity parsing ─────────────────────────────────────────────────────

    def _apply_entity(
        self, urn: str, entity: dict, signals: DatasetSignals, errors: list[str]
    ) -> None:
        """Populate ``signals`` from a single ``get_entities`` response dict."""
        try:
            if not entity:
                return

            # mcp-server-datahub returns nested DataHub GQL structure:
            #   properties.name, platform (object), editableProperties.description,
            #   domain (object), tags.tags[].tag.properties.name,
            #   schemaMetadata.fields, ownership.owners[].owner (CorpUser/CorpGroup)
            props = entity.get("properties") or {}
            editable_props = entity.get("editableProperties") or {}

            signals.name = (
                props.get("name")
                or entity.get("name")
                or entity.get("title")
                or urn
            )

            platform_raw = entity.get("platform") or {}
            signals.platform = (
                platform_raw.get("name", "")
                if isinstance(platform_raw, dict)
                else str(platform_raw)
            )

            signals.description = (
                editable_props.get("description")
                or props.get("description")
                or entity.get("description")
                or entity.get("editableDescription")
                or ""
            )
            signals.has_description = bool(signals.description.strip())

            domain_raw = entity.get("domain") or {}
            signals.domain = (
                domain_raw.get("urn", "")
                if isinstance(domain_raw, dict)
                else str(domain_raw)
            )

            # Tags: entity.tags.tags[].tag.properties.name
            tags_wrapper = entity.get("tags") or {}
            tag_list = (
                tags_wrapper.get("tags", [])
                if isinstance(tags_wrapper, dict)
                else (tags_wrapper if isinstance(tags_wrapper, list) else [])
            )
            signals.tags = []
            for t in tag_list:
                if not isinstance(t, dict):
                    signals.tags.append(str(t))
                    continue
                tag_obj = t.get("tag") or t
                tag_name = (
                    (tag_obj.get("properties") or {}).get("name")
                    or tag_obj.get("name")
                    or str(tag_obj)
                )
                signals.tags.append(tag_name)

            # Schema field count (proxy for richness / stability)
            schema = entity.get("schemaMetadata") or entity.get("schema") or {}
            if isinstance(schema, dict):
                signals.schema_field_count = len(schema.get("fields", []))

            # Ownership: ownership.owners[].owner (CorpUser/CorpGroup)
            ownership = entity.get("ownership") or {}
            for o in (ownership.get("owners", []) if isinstance(ownership, dict) else []):
                if not isinstance(o, dict):
                    continue
                owner_obj = o.get("owner") or o
                if not isinstance(owner_obj, dict):
                    continue
                owner_props = owner_obj.get("properties") or {}
                name = (
                    owner_props.get("displayName")
                    or owner_props.get("email")
                    or owner_obj.get("username")
                    or owner_obj.get("name")
                    or owner_obj.get("urn", "")
                )
                # CorpGroup owners count as a "team"
                is_group = (
                    owner_obj.get("__typename") == "CorpGroup"
                    or "name" in owner_obj
                )
                team = owner_obj.get("name", "") if is_group else ""
                if name:
                    signals.owners.append(str(name))
                if team and str(team) not in signals.owner_teams:
                    signals.owner_teams.append(str(team))

        except Exception as exc:
            errors.append(f"entity: {exc}")
            log.debug("_apply_entity failed for %s: %s", urn, exc)

    # ── Optional enrichment (usage + lineage) ─────────────────────────────

    def _enrich_one(self, signals: DatasetSignals) -> None:
        """Fetch usage and/or lineage for a single dataset (runs in thread pool)."""
        if self._collect_usage:
            self._fetch_usage(signals.urn, signals, signals.collection_errors)
        if self._collect_lineage:
            self._fetch_lineage(signals.urn, signals, signals.collection_errors)

    def _fetch_usage(
        self, urn: str, signals: DatasetSignals, errors: list[str]
    ) -> None:
        try:
            usage = self._conn.get_usage_stats(urn, window_days=self._window)
            if not usage:
                return
            signals.query_count_30d = int(
                usage.get("query_count")
                or usage.get("total")
                or usage.get("totalSqlQueries")
                or 0
            )
            signals.unique_users_30d = int(
                usage.get("unique_users")
                or usage.get("uniqueUserCount")
                or 0
            )
        except Exception as exc:
            errors.append(f"usage: {exc}")
            log.debug("get_usage_stats failed for %s: %s", urn, exc)

    def _fetch_lineage(
        self, urn: str, signals: DatasetSignals, errors: list[str]
    ) -> None:
        try:
            lineage = self._conn.get_lineage(urn, direction="downstream", depth=3)
            if not lineage:
                return

            # mcp-server-datahub returns: {"downstreams": {"searchResults": [...], ...}}
            # Each search result: {"entity": {"type": "...", "urn": "..."}, ...}
            downstream_wrapper = lineage.get("downstreams") or lineage
            candidates = (
                downstream_wrapper.get("searchResults")
                or downstream_wrapper.get("entities")
                or lineage.get("entities")
                or lineage.get("relationships")
                or []
            )
            for hit in candidates:
                if not isinstance(hit, dict):
                    continue
                entity = hit.get("entity", hit)
                etype = (entity.get("type") or "").upper()
                if etype == "DASHBOARD":
                    signals.downstream_dashboard_count += 1
                elif etype == "CHART":
                    signals.downstream_chart_count += 1
                elif etype == "DATASET":
                    signals.downstream_dataset_count += 1
        except Exception as exc:
            errors.append(f"lineage: {exc}")
            log.debug("get_lineage failed for %s: %s", urn, exc)
