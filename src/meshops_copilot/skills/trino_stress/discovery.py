"""Schema discovery for the trino_stress skill.

Walks a Trino cluster's ``information_schema`` and ``SHOW STATS`` to build a
complete picture of available catalogs, tables, columns, row counts, and likely
join relationships — without requiring any prior knowledge of the target schema.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshops_copilot.connectors.trino import TrinoConnector

log = logging.getLogger(__name__)

# ── Type classification sets ──────────────────────────────────────────────────

_NUMERIC_TYPES = frozenset({
    "integer", "int", "bigint", "smallint", "tinyint",
    "decimal", "numeric", "double", "real", "float",
})
_TIMESTAMP_TYPES = frozenset({
    "timestamp", "timestamp with time zone", "date", "time",
})
_CATEGORICAL_TYPES = frozenset({"varchar", "char", "boolean"})

# Catalogs / schemas that are always internal to Trino — never user data
_SYSTEM_CATALOGS = frozenset({
    "system", "tpch", "tpcds", "jmx", "$internal", "memory",
})
_SYSTEM_SCHEMAS = frozenset({
    "information_schema", "pg_catalog", "pg_toast",
    "pg_temp_1", "pg_toast_temp_1",
})

# Tool-internal schemas that are never useful for stress testing.
# Users can extend this list via ``exclude_schemas`` in the scenario YAML.
_DEFAULT_EXCLUDE_SCHEMAS = frozenset({
    "airbyte_internal",
})

# Catalog name suffixes that indicate non-production mirrors
_EXCLUDE_SUFFIXES = ("_developer", "_candidate")

# Per-table introspection timeout (seconds).  External connectors (Marketo,
# Google Ads, etc.) can hang indefinitely on SHOW STATS or TABLESAMPLE.
_DEFAULT_TABLE_TIMEOUT = 15


# ── Domain models ─────────────────────────────────────────────────────────────

@dataclass
class ColumnMeta:
    name: str
    data_type: str      # normalised to lowercase
    ordinal: int

    # ── Type predicates ───────────────────────────────────────────────────────

    @property
    def _base_type(self) -> str:
        return self.data_type.split("(")[0].strip().lower()

    @property
    def is_numeric(self) -> bool:
        return self._base_type in _NUMERIC_TYPES

    @property
    def is_timestamp(self) -> bool:
        return any(self._base_type.startswith(t) for t in _TIMESTAMP_TYPES)

    @property
    def is_categorical(self) -> bool:
        return self._base_type in _CATEGORICAL_TYPES

    # ── Key / FK heuristics ───────────────────────────────────────────────────

    @property
    def looks_like_pk(self) -> bool:
        return self.name.lower() == "id"

    @property
    def looks_like_fk(self) -> bool:
        n = self.name.lower()
        return n.endswith("_id") and n != "id"

    @property
    def fk_target(self) -> str | None:
        """Infer dimension table stem from an FK column name.

        ``user_id`` → ``"user"``  (caller checks for ``user`` and ``users``)
        """
        if not self.looks_like_fk:
            return None
        return self.name.lower().removesuffix("_id")


@dataclass
class TableMeta:
    catalog: str
    schema: str
    table: str
    columns: list[ColumnMeta] = field(default_factory=list)
    row_count: int | None = None    # None = unknown

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    @property
    def estimated_size(self) -> int:
        return self.row_count or 0

    # ── Column filters ────────────────────────────────────────────────────────

    @property
    def numeric_columns(self) -> list[ColumnMeta]:
        return [c for c in self.columns if c.is_numeric]

    @property
    def categorical_columns(self) -> list[ColumnMeta]:
        return [c for c in self.columns if c.is_categorical]

    @property
    def timestamp_columns(self) -> list[ColumnMeta]:
        return [c for c in self.columns if c.is_timestamp]

    @property
    def pk_column(self) -> ColumnMeta | None:
        return next((c for c in self.columns if c.looks_like_pk), None)

    @property
    def fk_columns(self) -> list[ColumnMeta]:
        return [c for c in self.columns if c.looks_like_fk]


@dataclass
class JoinPath:
    """A heuristically detected join between two tables."""

    from_table: TableMeta
    from_column: str
    to_table: TableMeta
    to_column: str


@dataclass
class DiscoveryResult:
    catalogs: list[str]
    tables: list[TableMeta]
    joins: list[JoinPath] = field(default_factory=list)

    def tables_by_size(self) -> list[TableMeta]:
        return sorted(self.tables, key=lambda t: t.estimated_size, reverse=True)

    def largest_table(self) -> TableMeta | None:
        ranked = self.tables_by_size()
        return ranked[0] if ranked else None


# ── Discovery engine ──────────────────────────────────────────────────────────

class SchemaDiscovery:
    """Walks a Trino cluster to build a :class:`DiscoveryResult`.

    Strategy
    --------
    1. ``SHOW CATALOGS`` → filter out system catalogs and non-production
       suffixes (``_developer``, ``_candidate``).  When ``include_catalogs``
       is set, only those catalogs are scanned.
    2. Per catalog: ``information_schema.tables`` → list user tables.
    3. Per table (parallel, with per-table timeout): columns + row count.
       ``SHOW STATS FOR`` is tried first (no full scan); falls back to a 1 %
       Bernoulli sample.  Tables that exceed ``table_timeout`` seconds are
       skipped with a warning rather than blocking the whole scan.
    4. FK heuristic: ``<X>_id`` columns matched against ``<X>`` / ``<X>s``.
    """

    def __init__(
        self,
        connector: TrinoConnector,
        include_catalogs: list[str] | None = None,
        exclude_catalogs: list[str] | None = None,
        exclude_schemas: list[str] | None = None,
        max_tables: int = 50,
        max_workers: int = 8,
        table_timeout: int = _DEFAULT_TABLE_TIMEOUT,
        catalog_timeout: int = 30,
    ) -> None:
        self._conn = connector
        self._include_catalogs: frozenset[str] | None = (
            frozenset(c.lower() for c in include_catalogs) if include_catalogs else None
        )
        self._exclude_catalogs = frozenset(
            list(_SYSTEM_CATALOGS) + (exclude_catalogs or [])
        )
        self._exclude_schemas = frozenset(
            list(_SYSTEM_SCHEMAS) + list(_DEFAULT_EXCLUDE_SCHEMAS) + (exclude_schemas or [])
        )
        self._max_tables = max_tables
        self._max_workers = max_workers
        self._table_timeout = table_timeout
        self._catalog_timeout = catalog_timeout  # timeout for SHOW CATALOGS / table listing

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> DiscoveryResult:
        log.info("Starting schema discovery against %s", self._conn.url)

        catalogs = self._discover_catalogs()
        log.info("User catalog(s) found: %s", catalogs)

        all_tables: list[TableMeta] = []
        for catalog in catalogs:
            tables = self._tables_in_catalog(catalog)
            all_tables.extend(tables)
            if len(all_tables) >= self._max_tables:
                log.warning(
                    "Reached max_tables=%d; stopping catalog scan.", self._max_tables
                )
                all_tables = all_tables[: self._max_tables]
                break

        log.info(
            "Introspecting %d table(s) (up to %d parallel workers, %ds timeout per table)…",
            len(all_tables), self._max_workers, self._table_timeout,
        )
        self._introspect_parallel(all_tables)

        joins = self._detect_joins(all_tables)
        log.info("Detected %d join path(s).", len(joins))

        return DiscoveryResult(catalogs=catalogs, tables=all_tables, joins=joins)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _discover_catalogs(self) -> list[str]:
        try:
            rows = self._conn.query_rows("SHOW CATALOGS", query_timeout=self._catalog_timeout)
        except Exception as exc:
            log.warning("SHOW CATALOGS failed: %s", exc)
            return []
        result = []
        for row in rows:
            name = next(iter(row.values()), None)
            if not name:
                continue
            name_lower = name.lower()
            # Explicit include list takes priority
            if self._include_catalogs is not None:
                if name_lower in self._include_catalogs:
                    result.append(name)
                continue
            # Skip system catalogs and non-production suffixes
            if name_lower in self._exclude_catalogs:
                continue
            if any(name_lower.endswith(sfx) for sfx in _EXCLUDE_SUFFIXES):
                log.debug("Skipping non-production catalog: %s", name)
                continue
            result.append(name)
        return result

    def _tables_in_catalog(self, catalog: str) -> list[TableMeta]:
        sql = f"""
            SELECT table_schema, table_name
            FROM {catalog}.information_schema.tables
            WHERE table_type = 'BASE TABLE'
        """
        try:
            rows = self._conn.query_rows(sql, query_timeout=self._catalog_timeout)
        except Exception as exc:
            log.warning("Table listing failed for catalog '%s': %s", catalog, exc)
            return []
        tables = []
        for row in rows:
            schema = row.get("table_schema", "")
            table = row.get("table_name", "")
            if schema and table and schema.lower() not in self._exclude_schemas:
                tables.append(TableMeta(catalog=catalog, schema=schema, table=table))
        return tables

    def _introspect_one(self, tbl: TableMeta) -> None:
        """Fetch columns and row count for a single table (runs in a thread)."""
        tbl.columns = self._columns(tbl)
        tbl.row_count = self._row_count(tbl)
        log.debug(
            "  %s  rows≈%s  cols=%d",
            tbl.full_name,
            f"{tbl.row_count:,}" if tbl.row_count else "unknown",
            len(tbl.columns),
        )

    def _introspect_parallel(self, tables: list[TableMeta]) -> None:
        """Introspect all tables in parallel; skip any that exceed the timeout."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self._introspect_one, tbl): tbl for tbl in tables}
            for future in as_completed(futures):
                tbl = futures[future]
                try:
                    future.result(timeout=self._table_timeout)
                except FuturesTimeout:
                    log.warning(
                        "Introspection timed out after %ds — skipping %s",
                        self._table_timeout, tbl.full_name,
                    )
                except Exception as exc:
                    log.warning("Introspection failed for %s: %s", tbl.full_name, exc)

    def _columns(self, tbl: TableMeta) -> list[ColumnMeta]:
        sql = f"""
            SELECT column_name, data_type, ordinal_position
            FROM {tbl.catalog}.information_schema.columns
            WHERE table_schema = '{tbl.schema}'
              AND table_name   = '{tbl.table}'
            ORDER BY ordinal_position
        """
        try:
            rows = self._conn.query_rows(sql, query_timeout=self._table_timeout)
        except Exception as exc:
            log.warning("Column discovery failed for %s: %s", tbl.full_name, exc)
            return []
        return [
            ColumnMeta(
                name=r["column_name"],
                data_type=r["data_type"].lower(),
                ordinal=int(r["ordinal_position"]),
            )
            for r in rows
            if "column_name" in r and "data_type" in r
        ]

    def _row_count(self, tbl: TableMeta) -> int | None:
        # ── Attempt 1: SHOW STATS (no full scan, connector-dependent) ─────────
        try:
            rows = self._conn.query_rows(
                f"SHOW STATS FOR {tbl.full_name}",
                query_timeout=self._table_timeout,
            )
            for row in rows:
                # The summary row has a null column_name and holds row_count
                if row.get("column_name") is None:
                    rc = row.get("row_count")
                    if rc is not None:
                        return int(float(rc))
        except Exception:
            pass  # fall through

        # ── Attempt 2: 1 % Bernoulli sample (much faster than full COUNT(*)) ─
        try:
            rows = self._conn.query_rows(
                f"SELECT COUNT(*) AS n FROM {tbl.full_name} TABLESAMPLE BERNOULLI(1)",
                query_timeout=self._table_timeout,
            )
            if rows:
                n = rows[0].get("n", 0)
                return int(n) * 100  # scale back up
        except Exception:
            pass

        return None  # unknown

    def _detect_joins(self, tables: list[TableMeta]) -> list[JoinPath]:
        """Match FK columns to PK columns of likely target tables.

        Heuristic: a column named ``<X>_id`` on table A is treated as a FK
        to a table named ``<X>`` or ``<X>s`` (plural) that has an ``id`` column.
        """
        by_name: dict[str, TableMeta] = {t.table.lower(): t for t in tables}
        joins: list[JoinPath] = []
        seen: set[tuple] = set()

        for tbl in tables:
            for fk_col in tbl.fk_columns:
                stem = fk_col.fk_target  # e.g. "user" from "user_id"
                if not stem:
                    continue
                for candidate in (stem + "s", stem):   # try plural first
                    target = by_name.get(candidate)
                    if target and target is not tbl:
                        pk = target.pk_column
                        if pk:
                            key = (tbl.full_name, fk_col.name, target.full_name, pk.name)
                            if key not in seen:
                                seen.add(key)
                                joins.append(
                                    JoinPath(
                                        from_table=tbl,
                                        from_column=fk_col.name,
                                        to_table=target,
                                        to_column=pk.name,
                                    )
                                )
                        break  # stop at first matching candidate

        return joins
