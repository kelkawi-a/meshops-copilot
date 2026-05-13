"""SQL query synthesiser for the trino_stress skill.

Takes a :class:`DiscoveryResult` and builds up to six named stress queries
tailored to the actual schema — no prior knowledge of table names required.

Query types attempted
---------------------
``light_count``      — COUNT(*) on the largest table.
``medium_agg``       — GROUP BY a categorical column + COUNT / COUNT DISTINCT.
``heavy_join``       — Star-schema join via detected FK relationships.
``window_functions`` — ROW_NUMBER / RANK / SUM / AVG over a table with numeric +
                       categorical columns.
``high_cardinality`` — High-cardinality GROUP BY with optional timestamp range.
``cross_catalog``    — Join tables from two different catalogs via a shared key.

Each builder returns ``(name, sql)`` or ``None`` if the schema doesn't provide
enough structure to construct that query type.
"""

from __future__ import annotations

import logging
import textwrap

from meshops_copilot.skills.trino_stress.discovery import (
    DiscoveryResult,
    JoinPath,
    TableMeta,
)

log = logging.getLogger(__name__)


class QueryBuilder:
    """Build named stress-test queries from a :class:`DiscoveryResult`."""

    def __init__(self, discovery: DiscoveryResult) -> None:
        self._d = discovery

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self) -> dict[str, str]:
        """Return all query types that could be constructed from the schema."""
        queries: dict[str, str] = {}
        for method in (
            self._light_count,
            self._medium_agg,
            self._heavy_join,
            self._window_functions,
            self._high_cardinality,
            self._cross_catalog,
        ):
            result = method()
            if result:
                name, sql = result
                queries[name] = sql
                log.debug("Built query '%s' for table(s) in SQL.", name)

        if not queries:
            log.warning("QueryBuilder could not construct any queries from the discovered schema.")
        else:
            log.info("Generated %d query type(s): %s", len(queries), list(queries.keys()))

        return queries

    # ── Individual query builders ─────────────────────────────────────────────

    def _light_count(self) -> tuple[str, str] | None:
        tbl = self._d.largest_table()
        if not tbl:
            return None
        return "light_count", f"SELECT COUNT(*) FROM {tbl.full_name}"

    def _medium_agg(self) -> tuple[str, str] | None:
        """COUNT + optional COUNT DISTINCT on a table with a categorical column."""
        tbl = self._pick(need_categorical=True)
        if not tbl:
            return None

        cat = tbl.categorical_columns[0]
        pk_or_fk = tbl.pk_column or (tbl.fk_columns[0] if tbl.fk_columns else None)

        if pk_or_fk:
            sql = textwrap.dedent(f"""\
                SELECT {cat.name},
                       COUNT(*)                       AS occurrences,
                       COUNT(DISTINCT {pk_or_fk.name}) AS unique_{pk_or_fk.name}
                FROM {tbl.full_name}
                GROUP BY {cat.name}
                ORDER BY occurrences DESC""")
        else:
            sql = textwrap.dedent(f"""\
                SELECT {cat.name},
                       COUNT(*) AS occurrences
                FROM {tbl.full_name}
                GROUP BY {cat.name}
                ORDER BY occurrences DESC""")

        return "medium_agg", sql

    def _heavy_join(self) -> tuple[str, str] | None:
        """Star-schema join: fact table + up to three dimension tables."""
        if not self._d.joins:
            return None

        # Pick the table with the most FK columns as the fact table
        fact = max(
            (t for t in self._d.tables if t.fk_columns),
            key=lambda t: (len(t.fk_columns), t.estimated_size),
            default=None,
        )
        if not fact:
            return None

        relevant = [j for j in self._d.joins if j.from_table is fact][:3]
        if not relevant:
            return None

        # Build SELECT clause
        select_parts: list[str] = []
        join_clauses: list[str] = []

        for i, jp in enumerate(relevant):
            alias = f"d{i}"
            join_clauses.append(
                f"JOIN {jp.to_table.full_name} {alias}"
                f" ON f.{jp.from_column} = {alias}.{jp.to_column}"
            )
            if jp.to_table.categorical_columns:
                select_parts.append(f"{alias}.{jp.to_table.categorical_columns[0].name}")

        # Fall back to the FK column itself if no categorical dim columns found
        if not select_parts:
            select_parts = [f"f.{relevant[0].from_column}"]

        # Aggregate columns from fact table
        agg_parts: list[str] = []
        if fact.pk_column:
            agg_parts.append(f"COUNT(DISTINCT f.{fact.pk_column.name}) AS total_records")
        for nc in fact.numeric_columns[:2]:
            agg_parts.append(f"SUM(f.{nc.name}) AS total_{nc.name}")
        if not agg_parts:
            agg_parts = ["COUNT(*) AS total_records"]

        group_by = ", ".join(select_parts)
        sel = ",\n       ".join(select_parts + agg_parts)
        joins_sql = "\n        ".join(join_clauses)

        sql = textwrap.dedent(f"""\
            SELECT {sel}
            FROM {fact.full_name} f
            {joins_sql}
            GROUP BY {group_by}
            ORDER BY total_records DESC""")

        return "heavy_join", sql

    def _window_functions(self) -> tuple[str, str] | None:
        """ROW_NUMBER / RANK / SUM / AVG window functions."""
        tbl = (
            self._pick(need_numeric=True, need_categorical=True, min_rows=1_000)
            or self._pick(need_numeric=True, min_rows=100)
        )
        if not tbl:
            return None

        num = tbl.numeric_columns[0]
        cat = tbl.categorical_columns[0] if tbl.categorical_columns else None
        pk = tbl.pk_column

        sel_cols = []
        if pk:
            sel_cols.append(pk.name)
        sel_cols.append(num.name)
        if cat:
            sel_cols.append(cat.name)

        win_parts = [
            f"ROW_NUMBER() OVER (ORDER BY {num.name} DESC) AS global_rank",
        ]
        if cat:
            win_parts += [
                f"RANK()       OVER (PARTITION BY {cat.name} ORDER BY {num.name} DESC) AS cat_rank",
                f"SUM({num.name})  OVER (PARTITION BY {cat.name}) AS cat_total",
                f"AVG({num.name})  OVER (PARTITION BY {cat.name}) AS cat_avg",
            ]

        sel = ",\n       ".join(sel_cols + win_parts)
        sql = f"SELECT {sel}\nFROM {tbl.full_name}"
        return "window_functions", sql

    def _high_cardinality(self) -> tuple[str, str] | None:
        """High-cardinality GROUP BY with optional timestamp range bookends."""
        tbl = self._pick(min_rows=5_000) or self._d.largest_table()
        if not tbl:
            return None

        # Pick the GROUP BY column: prefer PK, then FK, then first column
        group_col = (
            tbl.pk_column
            or (tbl.fk_columns[0] if tbl.fk_columns else None)
            or (tbl.columns[0] if tbl.columns else None)
        )
        if not group_col:
            return None

        ts_cols = tbl.timestamp_columns
        parts = [group_col.name, "COUNT(*) AS cnt"]
        if ts_cols:
            parts += [
                f"MIN({ts_cols[0].name}) AS first_seen",
                f"MAX({ts_cols[0].name}) AS last_seen",
            ]

        sel = ",\n       ".join(parts)
        sql = textwrap.dedent(f"""\
            SELECT {sel}
            FROM {tbl.full_name}
            GROUP BY {group_col.name}
            ORDER BY cnt DESC
            LIMIT 500""")

        return "high_cardinality", sql

    def _cross_catalog(self) -> tuple[str, str] | None:
        """Join two tables from different catalogs via a shared column."""
        if len(self._d.catalogs) < 2:
            return None

        # Prefer a directly detected cross-catalog join path
        cross = next(
            (j for j in self._d.joins if j.from_table.catalog != j.to_table.catalog),
            None,
        )

        # Fall back: find any two tables in different catalogs sharing a column name
        if not cross:
            col_to_tables: dict[str, list[TableMeta]] = {}
            for t in self._d.tables:
                for c in t.columns:
                    if c.looks_like_fk or c.looks_like_pk:
                        col_to_tables.setdefault(c.name, []).append(t)

            for col_name, tbls in col_to_tables.items():
                cross_tbls = [t for t in tbls if t.catalog != tbls[0].catalog]
                if cross_tbls:
                    cross = JoinPath(
                        from_table=tbls[0],
                        from_column=col_name,
                        to_table=cross_tbls[0],
                        to_column=col_name,
                    )
                    break

        if not cross:
            return None

        a, b = cross.from_table, cross.to_table

        # GROUP BY a categorical column from the "a" side
        group_expr = (
            f"a.{a.categorical_columns[0].name}"
            if a.categorical_columns
            else f"a.{cross.from_column}"
        )

        # Aggregate numerics from both sides where available
        agg_parts: list[str] = ["COUNT(DISTINCT a.{}) AS unique_keys".format(cross.from_column)]
        for nc in a.numeric_columns[:1]:
            agg_parts.append(f"SUM(a.{nc.name}) AS total_{nc.name}")
        for nc in b.numeric_columns[:1]:
            agg_parts.append(f"SUM(b.{nc.name}) AS total_{nc.name}_b")

        agg_sql = ",\n       ".join(agg_parts)
        sql = textwrap.dedent(f"""\
            SELECT {group_expr},
                   {agg_sql}
            FROM {a.full_name} a
            JOIN {b.full_name} b ON a.{cross.from_column} = b.{cross.to_column}
            GROUP BY {group_expr}
            ORDER BY unique_keys DESC""")

        return "cross_catalog", sql

    # ── Selection helper ──────────────────────────────────────────────────────

    def _pick(
        self,
        need_numeric: bool = False,
        need_categorical: bool = False,
        need_timestamp: bool = False,
        min_rows: int = 0,
    ) -> TableMeta | None:
        """Return the largest table satisfying the column requirements."""
        for tbl in self._d.tables_by_size():
            if min_rows and (tbl.row_count or 0) < min_rows:
                continue
            if need_numeric and not tbl.numeric_columns:
                continue
            if need_categorical and not tbl.categorical_columns:
                continue
            if need_timestamp and not tbl.timestamp_columns:
                continue
            return tbl
        return None
