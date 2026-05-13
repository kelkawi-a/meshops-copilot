"""Name-based dashboard audit for duplicate and overlap detection.

Unlike the signal-based ``duplicate_detector``, this auditor works purely on
dashboard **names**.  It strips team-ownership prefixes and status tokens,
clusters dashboards by their remaining core topic, then classifies each one
using deterministic rules.

No entity-detail or lineage calls are needed — only DataHub search results
(name + URN) are required, making it very fast even for large catalogues.

Entry point
-----------
    result = build_audit(entities)   # entities from search_dashboards()
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum


# ── Classification / action enums ─────────────────────────────────────────────

class DashboardClassification(str, Enum):
    GOLDEN_CANDIDATE     = "Golden candidate"
    ACTIVE_SUB_CUT       = "Active sub-cut"
    SUPERSEDED_VERSION   = "Superseded version"
    LEGACY               = "Legacy/unsupported"
    PERSONAL_COPY        = "Personal/test copy"
    WIP_DRAFT            = "WIP/draft"
    BROKEN_BACKUP        = "Broken/Backup"
    EXACT_DUPLICATE      = "Exact name duplicate"
    UNTITLED             = "Untitled"
    PERSONAL             = "Personal/playground"
    UNSUPPORTED_OWNERSHIP = "Unsupported ownership"


class DashboardAction(str, Enum):
    KEEP      = "Keep"
    DEPRECATE = "Deprecate"
    REVIEW    = "Review before deprecation"


# ── Domain models ──────────────────────────────────────────────────────────────

@dataclass
class DashboardRecord:
    """One Superset dashboard as it appears in a DataHub search result."""

    urn: str
    superset_id: int | None
    name: str
    team_prefix: str            # e.g. "Data" extracted from "[Data] Sales Overview"
    core_topic: str             # normalised, tokens stripped — used for clustering
    status_tokens: list[str]    # detected token types, e.g. ["copy", "version"]
    classification: DashboardClassification = DashboardClassification.GOLDEN_CANDIDATE
    action: DashboardAction = DashboardAction.KEEP
    replacement_id: int | None = None   # suggested replacement when deprecating


@dataclass
class DashboardCluster:
    """A group of dashboards that share the same (or near-matching) core topic."""

    topic: str
    members: list[DashboardRecord] = field(default_factory=list)


@dataclass
class AuditSummary:
    """Counts used to build the summary table in the report."""

    total: int = 0
    exact_duplicates: int = 0       # identical name, different IDs
    deprecated_legacy: int = 0      # deprecated / legacy / archived tokens
    unsupported: int = 0            # [Unsupported] ownership prefix or token
    copy_tokens: int = 0            # explicit "copy" markers
    old_test_dev: int = 0           # version / date / env tokens
    wip_draft: int = 0              # WIP / draft / staging
    untitled: int = 0               # blank or placeholder names
    personal: int = 0               # personal, playground, broken, backup

    @property
    def total_deprecation_candidates(self) -> int:
        return (
            self.exact_duplicates
            + self.deprecated_legacy
            + self.unsupported
            + self.copy_tokens
            + self.old_test_dev
            + self.wip_draft
            + self.untitled
            + self.personal
        )


@dataclass
class AuditResult:
    """Full output of ``build_audit()``."""

    all_records: list[DashboardRecord]
    clusters: list[DashboardCluster]    # all clusters; singletons included
    summary: AuditSummary


# ── Token detection patterns ───────────────────────────────────────────────────

# Team/ownership prefix: [TEAM], [TeamName], [ TeamName ] at the very start.
_TEAM_PREFIX_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)", re.DOTALL)

# Status token patterns keyed by token type.
_STATUS_PATTERNS: dict[str, re.Pattern] = {
    # Explicit copy markers
    "copy": re.compile(
        r"\bcopy\b|\bcopy\s+of\b|\bcopy\s*\d*\b|\bkopie\b|\bduplicate\b",
        re.IGNORECASE,
    ),
    # Version markers: "v2", "v10", "version 2"
    "version": re.compile(
        r"\bv\d+\b|\bversion\s*\d+\b",
        re.IGNORECASE,
    ),
    # Date stamps: "2024", "Jan 2024", "Q1 2024", "2024-01-15"
    "date": re.compile(
        r"\b(19|20)\d{2}\b"
        r"|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(19|20)\d{2}\b"
        r"|\bQ[1-4]\s*(19|20)?\d{2}\b"
        r"|\b\d{4}[-/]\d{2}[-/]\d{2}\b",
        re.IGNORECASE,
    ),
    # Environment markers
    "env": re.compile(
        r"\b(dev|development|test|testing|staging|stg|sandbox|uat|qa)\b",
        re.IGNORECASE,
    ),
    # Work-in-progress / draft
    "wip": re.compile(
        r"\b(wip|draft|in[-\s]?progress|todo|tbd)\b",
        re.IGNORECASE,
    ),
    # Deprecated / legacy / old (not "unsupported" — handled separately)
    "deprecated": re.compile(
        r"\b(old|deprecated|legacy|obsolete|archived|dead|sunset)\b",
        re.IGNORECASE,
    ),
    # Unsupported token (distinct from deprecated for summary categorisation)
    "unsupported": re.compile(
        r"\bunsupported\b",
        re.IGNORECASE,
    ),
    # Broken / backup / temp
    "broken": re.compile(
        r"\b(broken|backup|bak|temp|temporary)\b",
        re.IGNORECASE,
    ),
    # Personal / playground explicit keywords
    "personal": re.compile(
        r"\b(personal|playground|mine|my\s+\w+|test\s+dashboard)\b",
        re.IGNORECASE,
    ),
}

# Keywords stripped when computing the core topic for clustering.
_STRIP_KEYWORDS: list[str] = [
    "copy", "old", "deprecated", "legacy", "obsolete", "archived",
    "unsupported", "dev", "development", "test", "testing", "staging",
    "sandbox", "uat", "qa", "wip", "draft", "broken", "backup", "temp",
    "temporary", "personal", "playground",
]

# Team/ownership prefixes that indicate a dashboard is abandoned.
_UNSUPPORTED_TEAM_WORDS = frozenset({
    "unsupported", "deprecated", "legacy", "archived", "old", "retired",
    "dead", "abandoned", "obsolete",
})

# Words that look like department / team names (not personal names).
_DEPT_WORDS = frozenset({
    "sales", "finance", "engineering", "data", "marketing", "product",
    "analytics", "operations", "hr", "legal", "security", "infrastructure",
    "platform", "growth", "revenue", "customer", "commercial", "business",
    "insight", "reporting", "executive", "leadership", "management",
    "supply", "chain", "logistics", "support", "success", "delivery",
    "strategy", "research", "bi", "warehouse",
})


# ── Private helpers ────────────────────────────────────────────────────────────

def _extract_superset_id(urn: str) -> int | None:
    """Extract the numeric Superset dashboard ID from a DataHub URN."""
    m = re.search(r"urn:li:dashboard:\(superset,(\d+)\)", urn)
    if m:
        return int(m.group(1))
    # Fallback: last numeric sequence in the URN
    digits = re.findall(r"\d+", urn)
    return int(digits[-1]) if digits else None


def _extract_team_prefix(name: str) -> tuple[str, str]:
    """Split ``[TeamPrefix] rest of name`` into ``(team, rest)``.

    Returns ``("", name)`` if no bracketed prefix is present.
    """
    m = _TEAM_PREFIX_RE.match(name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", name.strip()


def _detect_status_tokens(name: str) -> list[str]:
    """Return a list of token type strings present in *name*.

    Order is deterministic (matches ``_STATUS_PATTERNS`` insertion order).
    """
    found: list[str] = []
    for token_type, pattern in _STATUS_PATTERNS.items():
        if pattern.search(name):
            found.append(token_type)
    return found


def _extract_core_topic(name: str) -> str:
    """Produce a normalised core topic string for clustering.

    Steps:
    1. Remove ``[Team]`` prefix.
    2. Remove parenthesised / bracketed sub-strings.
    3. Remove status keywords.
    4. Remove standalone version markers and year numbers.
    5. Lowercase, collapse whitespace, strip punctuation.
    """
    _, rest = _extract_team_prefix(name)

    # Remove anything inside ( ) or [ ]
    rest = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", rest)

    # Remove status keywords
    for kw in _STRIP_KEYWORDS:
        rest = re.sub(r"\b" + re.escape(kw) + r"\b", " ", rest, flags=re.IGNORECASE)

    # Remove standalone version markers and 4-digit years
    rest = re.sub(r"\bv\d+\b", " ", rest, flags=re.IGNORECASE)
    rest = re.sub(r"\b(19|20)\d{2}\b", " ", rest)

    # Normalise
    rest = re.sub(r"[^\w\s]", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip().lower()
    return rest


def _is_untitled(name: str) -> bool:
    """Return True if the name is blank, whitespace-only, or a placeholder."""
    clean = name.strip().lower()
    if not clean:
        return True
    return bool(re.fullmatch(
        r"(untitled|unnamed|new dashboard|dashboard\s*\d*|no title|n/a|\-+|\?+)",
        clean,
    ))


def _is_unsupported_ownership(team_prefix: str) -> bool:
    """Return True if the team prefix marks the dashboard as abandoned."""
    return team_prefix.strip().lower() in _UNSUPPORTED_TEAM_WORDS


def _looks_like_personal_name(word: str) -> bool:
    """Heuristic: could this word be a person's given name?"""
    return (
        len(word) >= 2
        and word[0].isupper()
        and not word.isupper()            # not an acronym
        and word.lower() not in _DEPT_WORDS
    )


def _is_personal_dashboard(name: str, team_prefix: str) -> bool:
    """Heuristic: does this dashboard belong to a specific individual?

    Returns True when:
    - No team prefix AND the name begins with two capitalised non-dept words
      (looks like ``FirstName LastName …``), OR
    - The name contains explicit personal-ownership keywords.
    """
    if team_prefix:
        return False    # team-owned → not personal
    lower = name.lower()
    if re.search(r"\b(personal|playground|mine)\b", lower):
        return True
    words = re.findall(r"[A-Za-z]+", name)
    if len(words) >= 2:
        if _looks_like_personal_name(words[0]) and _looks_like_personal_name(words[1]):
            return True
    return False


# ── Entity parsing ─────────────────────────────────────────────────────────────

def _parse_entity(entity: dict) -> DashboardRecord | None:
    """Build a ``DashboardRecord`` from a raw DataHub search-result entity dict."""
    if not isinstance(entity, dict):
        return None

    urn = entity.get("urn", "")
    if not urn:
        return None

    # Name resolution: dashboardProperties.name > properties.name > top-level
    dash_props = entity.get("dashboardProperties") or {}
    props      = entity.get("properties") or {}
    name = (
        dash_props.get("name")
        or props.get("name")
        or entity.get("name")
        or entity.get("title")
        or ""
    )

    superset_id    = _extract_superset_id(urn)
    team_prefix, _ = _extract_team_prefix(name)
    status_tokens  = _detect_status_tokens(name)
    core_topic     = _extract_core_topic(name)

    return DashboardRecord(
        urn=urn,
        superset_id=superset_id,
        name=name,
        team_prefix=team_prefix,
        core_topic=core_topic,
        status_tokens=status_tokens,
    )


# ── Classification ─────────────────────────────────────────────────────────────

def _classify_record(
    record: DashboardRecord,
    cluster_members: list[DashboardRecord] | None = None,
) -> tuple[DashboardClassification, DashboardAction]:
    """Classify a single dashboard.

    ``cluster_members`` (optional) provides the full cluster context so that
    version / sub-cut ambiguity can be resolved by checking whether a clean
    golden candidate exists alongside the record.
    """
    DC = DashboardClassification
    DA = DashboardAction

    name   = record.name
    tokens = record.status_tokens
    team   = record.team_prefix

    # ── 1. Untitled ────────────────────────────────────────────────────────────
    if _is_untitled(name):
        return DC.UNTITLED, DA.DEPRECATE

    # ── 2. Unsupported / abandoned ownership prefix ────────────────────────────
    if team and _is_unsupported_ownership(team):
        return DC.UNSUPPORTED_OWNERSHIP, DA.DEPRECATE

    # ── 3. Personal dashboard (no team, looks personal) ────────────────────────
    if _is_personal_dashboard(name, team):
        return DC.PERSONAL, DA.REVIEW

    # ── 4. Status-token classification ────────────────────────────────────────
    if "unsupported" in tokens:
        return DC.LEGACY, DA.DEPRECATE

    if "deprecated" in tokens:
        return DC.LEGACY, DA.DEPRECATE

    if "broken" in tokens:
        return DC.BROKEN_BACKUP, DA.DEPRECATE

    if "copy" in tokens:
        return DC.PERSONAL_COPY, DA.DEPRECATE

    if "wip" in tokens:
        return DC.WIP_DRAFT, DA.REVIEW

    if "personal" in tokens:
        return DC.PERSONAL, DA.REVIEW

    # ── 5. Version / date / env — resolve using cluster context ───────────────
    soft_tokens = [t for t in tokens if t in ("version", "date", "env")]
    if soft_tokens:
        if cluster_members:
            clean = [
                m for m in cluster_members
                if m is not record
                and not m.status_tokens
                and m.action == DA.KEEP
            ]
            if clean:
                return DC.SUPERSEDED_VERSION, DA.DEPRECATE
        # No clean sibling found — treat as a legitimate sub-cut for now
        return DC.ACTIVE_SUB_CUT, DA.REVIEW

    # ── 6. No disqualifying tokens — golden ───────────────────────────────────
    return DC.GOLDEN_CANDIDATE, DA.KEEP


# ── Exact-duplicate handling ───────────────────────────────────────────────────

def _mark_exact_duplicates(records: list[DashboardRecord]) -> None:
    """Flag records with identical (case-insensitive) names in-place.

    The dashboard with the **highest** Superset ID is kept as the de-facto
    golden; all others are marked as ``EXACT_DUPLICATE / Deprecate``.
    """
    by_name: dict[str, list[DashboardRecord]] = defaultdict(list)
    for r in records:
        by_name[r.name.strip().lower()].append(r)

    for group in by_name.values():
        if len(group) < 2:
            continue
        # Sort descending by ID — highest (newest) is kept
        ranked = sorted(group, key=lambda r: r.superset_id or 0, reverse=True)
        golden = ranked[0]
        # Only promote to golden if it has no disqualifying tokens
        if not golden.status_tokens and not _is_untitled(golden.name):
            golden.classification = DashboardClassification.GOLDEN_CANDIDATE
            golden.action         = DashboardAction.KEEP
        for dup in ranked[1:]:
            dup.classification = DashboardClassification.EXACT_DUPLICATE
            dup.action         = DashboardAction.DEPRECATE
            dup.replacement_id = golden.superset_id


# ── Topic clustering ───────────────────────────────────────────────────────────

_MIN_TOPIC_SIMILARITY = 0.82   # SequenceMatcher threshold for fuzzy grouping
_MIN_TOPIC_LENGTH     = 2      # ignore very short / empty core topics


def _topic_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _cluster_records(records: list[DashboardRecord]) -> list[DashboardCluster]:
    """Group records by similar core topics using greedy nearest-neighbour.

    1. Exact matches are grouped first.
    2. Remaining records are matched to the nearest existing cluster if
       similarity ≥ ``_MIN_TOPIC_SIMILARITY``; otherwise a new cluster is
       created.

    Clusters are sorted descending by member count.
    """
    # Pass 1: exact topic match
    exact_groups: dict[str, list[DashboardRecord]] = defaultdict(list)
    no_topic: list[DashboardRecord] = []
    for r in records:
        if len(r.core_topic) >= _MIN_TOPIC_LENGTH:
            exact_groups[r.core_topic].append(r)
        else:
            no_topic.append(r)

    # Merge exact groups into clusters
    clusters: list[DashboardCluster] = []
    for topic, members in exact_groups.items():
        clusters.append(DashboardCluster(topic=topic, members=list(members)))

    # Pass 2: fuzzy merge — combine clusters whose representative topics are similar
    merged = True
    while merged:
        merged = False
        new_clusters: list[DashboardCluster] = []
        used = set()
        for i, ca in enumerate(clusters):
            if i in used:
                continue
            for j, cb in enumerate(clusters):
                if j <= i or j in used:
                    continue
                if _topic_similarity(ca.topic, cb.topic) >= _MIN_TOPIC_SIMILARITY:
                    ca.members.extend(cb.members)
                    # Keep the longer / more descriptive topic as representative
                    if len(cb.topic) > len(ca.topic):
                        ca.topic = cb.topic
                    used.add(j)
                    merged = True
            new_clusters.append(ca)
        clusters = new_clusters

    # Add no-topic records as singletons
    for r in no_topic:
        clusters.append(DashboardCluster(topic=r.name.strip(), members=[r]))

    clusters.sort(key=lambda c: len(c.members), reverse=True)
    return clusters


# ── Summary counting ───────────────────────────────────────────────────────────

def _build_summary(records: list[DashboardRecord]) -> AuditSummary:
    DC = DashboardClassification
    s  = AuditSummary(total=len(records))
    for r in records:
        cls = r.classification
        if cls == DC.EXACT_DUPLICATE:
            s.exact_duplicates += 1
        elif cls == DC.LEGACY:
            # Distinguish "unsupported" from general deprecated/legacy
            if "unsupported" in r.status_tokens or (
                r.team_prefix and _is_unsupported_ownership(r.team_prefix)
            ):
                s.unsupported += 1
            else:
                s.deprecated_legacy += 1
        elif cls == DC.UNSUPPORTED_OWNERSHIP:
            s.unsupported += 1
        elif cls == DC.PERSONAL_COPY:
            s.copy_tokens += 1
        elif cls in (DC.SUPERSEDED_VERSION, DC.ACTIVE_SUB_CUT):
            s.old_test_dev += 1
        elif cls == DC.WIP_DRAFT:
            s.wip_draft += 1
        elif cls == DC.UNTITLED:
            s.untitled += 1
        elif cls in (DC.PERSONAL, DC.BROKEN_BACKUP):
            s.personal += 1
    return s


# ── Main entry point ───────────────────────────────────────────────────────────

def build_audit(entities: list[dict]) -> AuditResult:
    """Build a complete ``AuditResult`` from raw DataHub search-result entities.

    Parameters
    ----------
    entities:
        List of entity dicts returned by
        ``DataHubMCPConnector.search_dashboards()``.  Only ``urn`` and name
        fields are required.

    Returns
    -------
    ``AuditResult`` with ``all_records``, ``clusters``, and ``summary``
    populated.
    """
    # ── 1. Parse entities into records ────────────────────────────────────────
    records: list[DashboardRecord] = []
    for entity in entities:
        r = _parse_entity(entity)
        if r:
            records.append(r)

    # ── 2. First-pass classification (no cluster context) ─────────────────────
    for r in records:
        r.classification, r.action = _classify_record(r, cluster_members=None)

    # ── 3. Mark exact name duplicates (overrides first-pass for those records) ─
    _mark_exact_duplicates(records)

    # ── 4. Cluster by core topic ──────────────────────────────────────────────
    clusters = _cluster_records(records)

    # ── 5. Second-pass refinement using cluster context ───────────────────────
    # Upgrade ACTIVE_SUB_CUT → SUPERSEDED_VERSION when a clean golden sibling
    # exists in the same cluster.
    for cluster in clusters:
        if len(cluster.members) < 2:
            continue
        golden_members = [
            m for m in cluster.members
            if m.classification == DashboardClassification.GOLDEN_CANDIDATE
        ]
        if not golden_members:
            continue
        for member in cluster.members:
            if member.classification == DashboardClassification.ACTIVE_SUB_CUT:
                member.classification = DashboardClassification.SUPERSEDED_VERSION
                member.action         = DashboardAction.DEPRECATE
                member.replacement_id = golden_members[0].superset_id

    # ── 6. Build summary ──────────────────────────────────────────────────────
    summary = _build_summary(records)

    return AuditResult(all_records=records, clusters=clusters, summary=summary)
