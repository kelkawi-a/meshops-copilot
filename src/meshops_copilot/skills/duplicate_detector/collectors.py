"""Signal collection from DataHub MCP (and optionally Superset REST).

``DashboardCollector`` fetches dashboard entity metadata for a list of URNs
from DataHub via a single batched MCP call, then optionally enriches each
profile with:

- **Upstream dataset URNs** via ``get_lineage(..., direction="upstream")``
  (opt-in: ``collect_lineage=True``).  One MCP call per dashboard.

- **SQL fingerprints** from Superset chart ``query_context``
  (opt-in: ``collect_sql=True``, requires a ``SupersetConnector``).  One
  Superset API call per unique chart referenced by the dashboards.

Individual fetch failures are captured in ``DashboardProfile.collection_errors``
so one bad entity does not abort the whole run (graceful degradation).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from typing import TYPE_CHECKING

from meshops_copilot.core.logging import get_logger
from meshops_copilot.skills.duplicate_detector.models import DashboardProfile

if TYPE_CHECKING:
    from meshops_copilot.connectors.datahub_mcp import DataHubMCPConnector
    from meshops_copilot.connectors.superset import SupersetConnector

log = get_logger(__name__)


class DashboardCollector:
    """Collect all signals for a batch of dashboard URNs."""

    def __init__(
        self,
        connector: "DataHubMCPConnector",
        superset_connector: "SupersetConnector | None" = None,
        max_workers: int = 8,
        collect_lineage: bool = False,
        collect_sql: bool = False,
    ) -> None:
        self._conn = connector
        self._superset = superset_connector
        self._max_workers = max_workers
        self._collect_lineage = collect_lineage
        self._collect_sql = collect_sql and superset_connector is not None

    def collect_all(self, urns: list[str]) -> list[DashboardProfile]:
        """Collect signals for all dashboard URNs.

        Entity metadata is fetched in a single batched MCP call.
        Lineage and SQL (if enabled) are fetched in parallel per-dashboard.
        """
        if not urns:
            return []

        # ── 1. Batch-fetch entity metadata (1 MCP call) ────────────────────
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

        # ── 2. Parse entity data into DashboardProfile objects ─────────────
        profiles: list[DashboardProfile] = []
        for urn, entity in zip(urns, entities):
            profile = DashboardProfile(urn=urn)
            errors: list[str] = []
            if urn in fetch_errors:
                errors.append(fetch_errors[urn])
            self._apply_entity(urn, entity, profile, errors)
            profile.collection_errors = errors
            profiles.append(profile)

        # ── 3. Optional enrichment in parallel ─────────────────────────────
        needs_enrichment = self._collect_lineage or self._collect_sql
        if not needs_enrichment:
            return profiles

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._enrich_one, profile): profile
                for profile in profiles
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    prof = futures[future]
                    log.warning("Enrichment failed for %s: %s", prof.urn, exc)
                    prof.collection_errors.append(str(exc))

        return profiles

    # ── Entity parsing ──────────────────────────────────────────────────────

    def _apply_entity(
        self,
        urn: str,
        entity: dict,
        profile: DashboardProfile,
        errors: list[str],
    ) -> None:
        """Populate ``profile`` from a single DataHub dashboard entity dict."""
        try:
            if not entity:
                return

            # title — DataHub stores it in dashboardProperties.name; fall back to
            # top-level name/title for older schema versions
            dash_props = entity.get("dashboardProperties") or {}
            edit_props = entity.get("editableProperties") or {}

            profile.title = (
                dash_props.get("name")
                or entity.get("name")
                or entity.get("title")
                or urn
            )

            profile.description = (
                edit_props.get("description")
                or dash_props.get("description")
                or entity.get("description")
                or ""
            )

            # Platform
            platform_raw = entity.get("platform") or {}
            profile.platform = (
                platform_raw.get("name", "")
                if isinstance(platform_raw, dict)
                else str(platform_raw)
            )

            # Chart URNs — dashboardProperties.charts[].urn
            charts_raw = dash_props.get("charts") or entity.get("charts") or []
            profile.chart_urns = _extract_urns(charts_raw)

            # Dataset URNs — dashboardProperties.datasets[].urn (static, no lineage needed)
            datasets_raw = dash_props.get("datasets") or entity.get("datasets") or []
            profile.dataset_urns = _extract_urns(datasets_raw)

            # Ownership
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
                is_group = (
                    owner_obj.get("__typename") == "CorpGroup"
                    or "name" in owner_obj
                )
                team = owner_obj.get("name", "") if is_group else ""
                if name:
                    profile.owners.append(str(name))
                if team and str(team) not in profile.owner_teams:
                    profile.owner_teams.append(str(team))

            # Tags
            tags_wrapper = entity.get("tags") or {}
            tag_list = (
                tags_wrapper.get("tags", [])
                if isinstance(tags_wrapper, dict)
                else (tags_wrapper if isinstance(tags_wrapper, list) else [])
            )
            for t in tag_list:
                if not isinstance(t, dict):
                    profile.tags.append(str(t))
                    continue
                tag_obj = t.get("tag") or t
                tag_name = (
                    (tag_obj.get("properties") or {}).get("name")
                    or tag_obj.get("name")
                    or str(tag_obj)
                )
                profile.tags.append(tag_name)

            # Glossary terms — glossaryTerms.terms[].term.urn
            gt_wrapper = entity.get("glossaryTerms") or {}
            term_list = (
                gt_wrapper.get("terms", [])
                if isinstance(gt_wrapper, dict)
                else []
            )
            for t in term_list:
                if not isinstance(t, dict):
                    continue
                term_obj = t.get("term") or t
                if isinstance(term_obj, dict):
                    term_urn = term_obj.get("urn", "")
                    if term_urn:
                        profile.glossary_term_urns.append(term_urn)

        except Exception as exc:
            errors.append(f"entity: {exc}")
            log.debug("_apply_entity failed for %s: %s", urn, exc)

    # ── Optional enrichment ─────────────────────────────────────────────────

    def _enrich_one(self, profile: DashboardProfile) -> None:
        """Fetch lineage / SQL for a single dashboard (runs in thread pool)."""
        if self._collect_lineage:
            self._fetch_lineage(profile)
        if self._collect_sql:
            self._fetch_sql(profile)

    def _fetch_lineage(self, profile: DashboardProfile) -> None:
        """Fetch upstream dataset URNs via DataHub lineage."""
        try:
            lineage = self._conn.get_lineage(profile.urn, direction="upstream", depth=2)
            if not lineage:
                return

            # Response shape mirrors downstream: {"upstreams": {"searchResults": [...]}}
            # but the connector uses "upstream" param; the actual key may vary.
            wrapper = (
                lineage.get("upstreams")
                or lineage.get("downstreams")  # some versions use same wrapper
                or lineage
            )
            candidates = (
                wrapper.get("searchResults")
                or wrapper.get("entities")
                or lineage.get("entities")
                or lineage.get("relationships")
                or []
            )
            seen = set(profile.dataset_urns)
            for hit in candidates:
                if not isinstance(hit, dict):
                    continue
                entity = hit.get("entity", hit)
                etype = (entity.get("type") or "").upper()
                if etype == "DATASET":
                    urn = entity.get("urn", "")
                    if urn and urn not in seen:
                        profile.dataset_urns.append(urn)
                        seen.add(urn)
        except Exception as exc:
            profile.collection_errors.append(f"lineage: {exc}")
            log.debug("get_lineage failed for %s: %s", profile.urn, exc)

    def _fetch_sql(self, profile: DashboardProfile) -> None:
        """Fetch SQL fingerprints from Superset chart query_contexts."""
        if not self._superset or not profile.chart_urns:
            return
        fingerprints: list[str] = []
        for chart_urn in profile.chart_urns:
            chart_id = _superset_chart_id(chart_urn)
            if chart_id is None:
                continue
            try:
                chart = self._superset.get_chart(chart_id)
                qc = chart.get("query_context") or chart.get("params") or ""
                fp = _sql_fingerprint(qc)
                if fp:
                    fingerprints.append(fp)
            except Exception as exc:
                profile.collection_errors.append(f"sql[{chart_id}]: {exc}")
                log.debug("Superset get_chart(%s) failed: %s", chart_id, exc)
        profile.sql_fingerprints = fingerprints


# ── Utilities ──────────────────────────────────────────────────────────────────

def _extract_urns(raw: list | dict | None) -> list[str]:
    """Extract URN strings from a variety of DataHub list shapes."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw] if raw.startswith("urn:") else []
    if isinstance(raw, dict):
        urn = raw.get("urn", "")
        return [urn] if urn else []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.startswith("urn:"):
            result.append(item)
        elif isinstance(item, dict):
            urn = item.get("urn", "")
            if urn:
                result.append(urn)
    return result


def _superset_chart_id(chart_urn: str) -> int | None:
    """Extract the numeric Superset chart ID from a DataHub chart URN.

    DataHub formats Superset chart URNs as ``urn:li:chart:(superset,<id>)``.
    Returns ``None`` for non-Superset URNs or if the ID cannot be parsed.
    """
    match = re.search(r"urn:li:chart:\(superset,(\d+)\)", chart_urn)
    if match:
        return int(match.group(1))
    # Fallback: last numeric component
    digits = re.findall(r"\d+", chart_urn)
    if digits:
        return int(digits[-1])
    return None


_VOLATILE_QC_KEYS = frozenset({
    "force", "result_format", "result_type", "cache_timeout",
    "annotation_layers", "url_params", "time_range_endpoints",
})


def _sql_fingerprint(query_context: str | dict | None) -> str | None:
    """Normalise a Superset ``query_context`` and return its SHA-256 fingerprint.

    Normalisation steps:
    1. Parse JSON if needed.
    2. Remove volatile / cache-related keys that differ between renders.
    3. Sort all list values so order differences are ignored.
    4. SHA-256 the canonical JSON string.

    Returns ``None`` if the input is empty or un-parseable.
    """
    if not query_context:
        return None
    if isinstance(query_context, str):
        try:
            qc = json.loads(query_context)
        except (json.JSONDecodeError, TypeError):
            # Treat the raw string as an opaque SQL fingerprint
            normalised = re.sub(r"\s+", " ", query_context.lower().strip())
            return hashlib.sha256(normalised.encode()).hexdigest()
    else:
        qc = query_context

    if not isinstance(qc, dict):
        return None

    cleaned = _clean_qc(qc)
    canonical = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _clean_qc(obj: object) -> object:
    """Recursively remove volatile keys and sort lists."""
    if isinstance(obj, dict):
        return {
            k: _clean_qc(v)
            for k, v in sorted(obj.items())
            if k not in _VOLATILE_QC_KEYS
        }
    if isinstance(obj, list):
        cleaned = [_clean_qc(i) for i in obj]
        try:
            return sorted(cleaned, key=lambda x: json.dumps(x, sort_keys=True))
        except TypeError:
            return cleaned
    return obj
