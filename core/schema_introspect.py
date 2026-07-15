"""
Reads the REAL schema (tables, columns, indexes) from the connected
database before any LLM is involved.

This is the core anti-hallucination measure: the LLM is only ever shown
column/index names that we already confirmed exist. If the LLM later
mentions something outside this set, core.validator rejects it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    name: str
    data_type: str


@dataclass
class IndexInfo:
    name: str
    definition: str


@dataclass
class TableSchema:
    table_name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    row_estimate: int | None = None
    partition_key: list[str] | None = None

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    @property
    def index_names(self) -> set[str]:
        return {i.name for i in self.indexes}

    def as_prompt_block(self) -> str:
        """Compact, factual description to embed directly in an LLM prompt."""
        cols = "\n".join(f"  - {c.name} ({c.data_type})" for c in self.columns)
        idxs = "\n".join(f"  - {i.name}: {i.definition}" for i in self.indexes) or "  (none)"
        return (
            f"Table: {self.table_name}\n"
            f"Estimated rows: {self.row_estimate if self.row_estimate is not None else 'unknown'}\n"
            f"Columns:\n{cols}\n"
            f"Indexes:\n{idxs}"
        )


def get_table_row_counts(conn, table_names: list[str]) -> dict[str, int]:
    """
    Cheap, single-query lookup of approximate table sizes (from
    pg_class.reltuples, the same estimate the planner itself uses — no
    COUNT(*) needed). Used to judge filter selectivity for skills like
    missing_index, independently of whether the LLM fallback ever runs.
    """
    if not table_names:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, reltuples::bigint FROM pg_class WHERE relname = ANY(%s)",
            (table_names,),
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def introspect_table(conn, table_name: str, schema: str = "public") -> TableSchema:
    """
    conn: a live psycopg2 connection.
    Pure read-only queries against information_schema / pg_catalog —
    never touches user data, only metadata.
    """
    result = TableSchema(table_name=table_name)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table_name),
        )
        result.columns = [ColumnInfo(name=r[0], data_type=r[1]) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = %s AND tablename = %s
            """,
            (schema, table_name),
        )
        result.indexes = [IndexInfo(name=r[0], definition=r[1]) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT reltuples::bigint
            FROM pg_class
            WHERE relname = %s
            """,
            (table_name,),
        )
        row = cur.fetchone()
        result.row_estimate = int(row[0]) if row else None

        # Path A: this table is itself the partitioned parent.
        cur.execute(
            """
            SELECT a.attname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_partitioned_table pt ON pt.partrelid = c.oid
            CROSS JOIN LATERAL unnest(
                string_to_array(pt.partattrs::text, ' ')::smallint[]
            ) WITH ORDINALITY u(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = u.attnum
            WHERE n.nspname = %s AND c.relname = %s
            ORDER BY u.ord
            """,
            (schema, table_name),
        )
        pk_rows = cur.fetchall()

        if not pk_rows:
            # Path B: this table is a partition child — resolve via pg_inherits.
            cur.execute(
                """
                SELECT a.attname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_partitioned_table pt ON pt.partrelid = i.inhparent
                CROSS JOIN LATERAL unnest(
                    string_to_array(pt.partattrs::text, ' ')::smallint[]
                ) WITH ORDINALITY u(attnum, ord)
                JOIN pg_attribute a
                    ON a.attrelid = i.inhparent AND a.attnum = u.attnum
                WHERE n.nspname = %s AND c.relname = %s
                ORDER BY u.ord
                """,
                (schema, table_name),
            )
            pk_rows = cur.fetchall()

        result.partition_key = [r[0] for r in pk_rows] if pk_rows else None

    return result


def introspect_query_tables(conn, table_names: list[str], schema: str = "public") -> dict[str, TableSchema]:
    """Convenience wrapper: introspect every table referenced by a query."""
    return {t: introspect_table(conn, t, schema=schema) for t in table_names}
