"""Unit tests for QueryBuilder (no live Trino required)."""

from __future__ import annotations

import pytest

from meshops_copilot.skills.trino_stress.discovery import (
    ColumnMeta,
    DiscoveryResult,
    JoinPath,
    TableMeta,
)
from meshops_copilot.skills.trino_stress.query_builder import QueryBuilder


# ── Helpers ───────────────────────────────────────────────────────────────────

def _col(name: str, dtype: str, ordinal: int = 1) -> ColumnMeta:
    return ColumnMeta(name=name, data_type=dtype, ordinal=ordinal)


def _table(catalog: str, schema: str, name: str, cols: list[ColumnMeta], rows: int = 0) -> TableMeta:
    t = TableMeta(catalog=catalog, schema=schema, table=name, columns=cols, row_count=rows)
    return t


# ── Minimal schema (single table, no FK) ─────────────────────────────────────

def test_light_count_always_generated():
    tbl = _table("cat", "pub", "events", [_col("id", "bigint")], rows=50_000)
    result = DiscoveryResult(catalogs=["cat"], tables=[tbl])
    queries = QueryBuilder(result).build()
    assert "light_count" in queries
    assert "cat.pub.events" in queries["light_count"]


def test_medium_agg_requires_categorical():
    tbl_no_cat = _table("cat", "pub", "numbers", [_col("val", "bigint")], rows=1_000)
    result = DiscoveryResult(catalogs=["cat"], tables=[tbl_no_cat])
    queries = QueryBuilder(result).build()
    assert "medium_agg" not in queries

    tbl_with_cat = _table("cat", "pub", "events", [
        _col("id", "bigint", 1),
        _col("event_type", "varchar", 2),
    ], rows=10_000)
    result2 = DiscoveryResult(catalogs=["cat"], tables=[tbl_with_cat])
    queries2 = QueryBuilder(result2).build()
    assert "medium_agg" in queries2
    assert "event_type" in queries2["medium_agg"]


# ── Star schema (fact + dimensions) ──────────────────────────────────────────

def _workshop_schema() -> DiscoveryResult:
    users = _table("db", "pub", "users", [
        _col("id", "bigint", 1),
        _col("country", "varchar", 2),
    ], rows=50_000)
    products = _table("db", "pub", "products", [
        _col("id", "bigint", 1),
        _col("category", "varchar", 2),
        _col("price", "decimal", 3),
    ], rows=5_000)
    orders = _table("db", "pub", "orders", [
        _col("id", "bigint", 1),
        _col("user_id", "bigint", 2),
        _col("status", "varchar", 3),
        _col("total_amount", "decimal", 4),
        _col("created_at", "timestamp", 5),
    ], rows=100_000)
    order_items = _table("db", "pub", "order_items", [
        _col("id", "bigint", 1),
        _col("order_id", "bigint", 2),
        _col("product_id", "bigint", 3),
        _col("quantity", "bigint", 4),
        _col("total_price", "decimal", 5),
    ], rows=400_000)

    # Manually wire joins (normally detected by SchemaDiscovery)
    joins = [
        JoinPath(from_table=orders,      from_column="user_id",    to_table=users,    to_column="id"),
        JoinPath(from_table=order_items, from_column="order_id",   to_table=orders,   to_column="id"),
        JoinPath(from_table=order_items, from_column="product_id", to_table=products, to_column="id"),
    ]
    return DiscoveryResult(
        catalogs=["db"],
        tables=[users, products, orders, order_items],
        joins=joins,
    )


def test_heavy_join_uses_most_fk_table():
    result = _workshop_schema()
    queries = QueryBuilder(result).build()
    assert "heavy_join" in queries
    # order_items has 2 FKs → should be the fact table
    assert "order_items" in queries["heavy_join"]


def test_window_functions_uses_numeric_and_categorical():
    result = _workshop_schema()
    queries = QueryBuilder(result).build()
    assert "window_functions" in queries
    sql = queries["window_functions"]
    assert "ROW_NUMBER()" in sql or "ROW_NUMBER" in sql


def test_high_cardinality_generated():
    result = _workshop_schema()
    queries = QueryBuilder(result).build()
    assert "high_cardinality" in queries
    assert "LIMIT 500" in queries["high_cardinality"]


def test_cross_catalog_requires_multiple_catalogs():
    result = _workshop_schema()  # only one catalog
    queries = QueryBuilder(result).build()
    assert "cross_catalog" not in queries


def test_cross_catalog_generated_for_two_catalogs():
    users = _table("db", "pub", "users", [
        _col("id", "bigint", 1),
        _col("country", "varchar", 2),
    ], rows=50_000)
    segments = _table("crm", "pub", "customer_segments", [
        _col("id", "bigint", 1),
        _col("name", "varchar", 2),
    ], rows=10)
    members = _table("crm", "pub", "segment_members", [
        _col("id", "bigint", 1),
        _col("user_id", "bigint", 2),
        _col("segment_id", "bigint", 3),
    ], rows=50_000)

    joins = [
        JoinPath(from_table=members, from_column="user_id",    to_table=users,    to_column="id"),
        JoinPath(from_table=members, from_column="segment_id", to_table=segments, to_column="id"),
    ]
    result = DiscoveryResult(
        catalogs=["db", "crm"],
        tables=[users, segments, members],
        joins=joins,
    )
    queries = QueryBuilder(result).build()
    assert "cross_catalog" in queries
    # Should join across db and crm catalogs
    sql = queries["cross_catalog"]
    assert "db.pub.users" in sql or "crm.pub" in sql


# ── Empty schema edge case ────────────────────────────────────────────────────

def test_empty_schema_returns_no_queries():
    result = DiscoveryResult(catalogs=[], tables=[])
    queries = QueryBuilder(result).build()
    assert queries == {}
