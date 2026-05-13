"""Unit tests for SchemaDiscovery (mocked connector)."""

from __future__ import annotations

import pytest

from meshops_copilot.skills.trino_stress.discovery import (
    ColumnMeta,
    DiscoveryResult,
    JoinPath,
    SchemaDiscovery,
    TableMeta,
    _EXCLUDE_SUFFIXES,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _col(name: str, data_type: str, ordinal: int = 1) -> ColumnMeta:
    return ColumnMeta(name=name, data_type=data_type, ordinal=ordinal)


def _table(catalog: str, schema: str, table: str, cols: list[ColumnMeta], rows: int | None = None) -> TableMeta:
    t = TableMeta(catalog=catalog, schema=schema, table=table)
    t.columns = cols
    t.row_count = rows
    return t


class _MockConn:
    url = "http://mock:8080"
    def __init__(self, catalog_rows=None, table_rows=None):
        self._catalog_rows = catalog_rows or []
        self._table_rows = table_rows or []
    def query_rows(self, sql: str, query_timeout: int | None = None):
        if "SHOW CATALOGS" in sql:
            return self._catalog_rows
        return self._table_rows


# ── ColumnMeta predicates ─────────────────────────────────────────────────────

def test_numeric_types():
    assert _col("x", "bigint").is_numeric
    assert _col("x", "decimal(10,2)").is_numeric
    assert not _col("x", "varchar").is_numeric


def test_timestamp_types():
    assert _col("x", "timestamp").is_timestamp
    assert _col("x", "timestamp with time zone").is_timestamp
    assert _col("x", "date").is_timestamp
    assert not _col("x", "bigint").is_timestamp


def test_categorical_types():
    assert _col("x", "varchar(255)").is_categorical
    assert _col("x", "boolean").is_categorical
    assert not _col("x", "bigint").is_categorical


def test_pk_fk_heuristics():
    assert _col("id", "bigint").looks_like_pk
    assert not _col("user_id", "bigint").looks_like_pk
    assert _col("user_id", "bigint").looks_like_fk
    assert not _col("id", "bigint").looks_like_fk
    assert _col("order_id", "bigint").fk_target == "order"


# ── TableMeta helpers ─────────────────────────────────────────────────────────

def test_table_column_filters():
    tbl = _table("cat", "sch", "orders", [
        _col("id", "bigint", 1),
        _col("user_id", "bigint", 2),
        _col("status", "varchar", 3),
        _col("total", "decimal(10,2)", 4),
        _col("created_at", "timestamp", 5),
    ], rows=100_000)

    assert tbl.pk_column.name == "id"
    assert [c.name for c in tbl.fk_columns] == ["user_id"]
    assert len(tbl.numeric_columns) == 3   # id, user_id, total are all bigint/decimal
    assert len(tbl.categorical_columns) == 1
    assert len(tbl.timestamp_columns) == 1
    assert tbl.estimated_size == 100_000
    assert tbl.full_name == "cat.sch.orders"


# ── Join detection ────────────────────────────────────────────────────────────

def test_join_detection():
    users = _table("cat", "pub", "users", [_col("id", "bigint")], 50_000)
    orders = _table("cat", "pub", "orders", [
        _col("id", "bigint", 1),
        _col("user_id", "bigint", 2),
        _col("total", "decimal", 3),
    ], 100_000)

    disc = SchemaDiscovery(_MockConn())
    joins = disc._detect_joins([users, orders])

    assert len(joins) == 1
    assert joins[0].from_table is orders
    assert joins[0].from_column == "user_id"
    assert joins[0].to_table is users
    assert joins[0].to_column == "id"


def test_no_joins_when_no_fk_columns():
    a = _table("cat", "pub", "alpha", [_col("id", "bigint")], 1_000)
    b = _table("cat", "pub", "beta", [_col("id", "bigint"), _col("val", "double")], 500)

    disc = SchemaDiscovery(_MockConn())
    assert disc._detect_joins([a, b]) == []


# ── DiscoveryResult helpers ───────────────────────────────────────────────────

def test_tables_by_size_ordering():
    small = _table("c", "s", "small_tbl", [], 100)
    large = _table("c", "s", "large_tbl", [], 100_000)
    medium = _table("c", "s", "medium_tbl", [], 5_000)
    result = DiscoveryResult(catalogs=["c"], tables=[small, large, medium])
    ordered = result.tables_by_size()
    assert [t.table for t in ordered] == ["large_tbl", "medium_tbl", "small_tbl"]


def test_largest_table_none_on_empty():
    result = DiscoveryResult(catalogs=[], tables=[])
    assert result.largest_table() is None


# ── Catalog filtering ─────────────────────────────────────────────────────────

def test_developer_catalogs_excluded_automatically():
    """Catalogs ending in _developer or _candidate are silently dropped."""
    catalogs = [
        {"Catalog": "sales"},
        {"Catalog": "sales_developer"},
        {"Catalog": "marketing"},
        {"Catalog": "marketing_candidate"},
        {"Catalog": "system"},   # system catalog — also excluded
    ]
    disc = SchemaDiscovery(_MockConn(catalog_rows=catalogs))
    found = disc._discover_catalogs()
    assert found == ["sales", "marketing"]


def test_include_catalogs_restricts_scan():
    """When include_catalogs is set, only those catalogs are returned."""
    catalogs = [
        {"Catalog": "sales"},
        {"Catalog": "salesforce"},
        {"Catalog": "support"},
        {"Catalog": "marketing"},
    ]
    disc = SchemaDiscovery(_MockConn(catalog_rows=catalogs), include_catalogs=["sales", "support"])
    found = disc._discover_catalogs()
    assert set(found) == {"sales", "support"}


def test_include_catalogs_case_insensitive():
    catalogs = [{"Catalog": "Sales"}, {"Catalog": "Support"}]
    disc = SchemaDiscovery(_MockConn(catalog_rows=catalogs), include_catalogs=["sales", "support"])
    found = disc._discover_catalogs()
    assert set(found) == {"Sales", "Support"}


# ── Schema filtering ──────────────────────────────────────────────────────────

def test_airbyte_internal_excluded_by_default():
    """airbyte_internal is in the default exclusion list without any configuration."""
    from meshops_copilot.skills.trino_stress.discovery import _DEFAULT_EXCLUDE_SCHEMAS
    assert "airbyte_internal" in _DEFAULT_EXCLUDE_SCHEMAS
    disc = SchemaDiscovery(_MockConn())
    assert "airbyte_internal" in disc._exclude_schemas


def test_user_exclude_schemas_merged_with_defaults():
    """User-supplied exclude_schemas are merged with system + default schemas."""
    disc = SchemaDiscovery(_MockConn(), exclude_schemas=["raw", "staging"])
    assert "raw" in disc._exclude_schemas
    assert "staging" in disc._exclude_schemas
    assert "airbyte_internal" in disc._exclude_schemas
    assert "information_schema" in disc._exclude_schemas
