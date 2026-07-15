"""
Integration tests for schema_introspect.introspect_table() against a real PostgreSQL database.
All tests skip gracefully when SQLDOCTOR_TEST_DSN is not set.

To run locally:
    $env:SQLDOCTOR_TEST_DSN="postgresql://postgres:123@localhost:5432/dbtest"
    pytest tests/test_introspect_integration.py -v
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.schema_introspect import introspect_table

_TEST_SCHEMA = "source"


def _get_conn():
    dsn = os.environ.get("SQLDOCTOR_TEST_DSN")
    if not dsn:
        pytest.skip("SQLDOCTOR_TEST_DSN not set — skipping live-DB integration test")
    return psycopg2.connect(dsn)


def test_partitioned_table_has_partition_key():
    """
    introspect_table() against transactions_partitioned (range-partitioned on txn_date)
    must return partition_key == ["txn_date"]. Confirms catalog query path A
    (table is itself the partitioned parent).
    """
    conn = _get_conn()
    try:
        result = introspect_table(conn, "transactions_partitioned", schema=_TEST_SCHEMA)
    finally:
        conn.close()
    assert result.partition_key == ["txn_date"], (
        f"expected partition_key=['txn_date'] for transactions_partitioned, "
        f"got {result.partition_key!r}"
    )


def test_non_partitioned_table_has_no_partition_key():
    """
    introspect_table() against a regular (non-partitioned) table must return
    partition_key=None — preserving the distinction between 'not partitioned'
    and 'partitioned with zero columns'.
    """
    conn = _get_conn()
    try:
        result = introspect_table(conn, "transactions", schema=_TEST_SCHEMA)
    finally:
        conn.close()
    assert result.partition_key is None, (
        f"expected partition_key=None for non-partitioned transactions table, "
        f"got {result.partition_key!r}"
    )


def test_partition_child_inherits_partition_key():
    """
    introspect_table() against a child partition of transactions_partitioned must also
    return partition_key == ["txn_date"]. The skill matcher uses child relation names
    from the EXPLAIN plan to look up partition keys — this confirms catalog query
    path B (table is a partition child).
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                FROM pg_class p
                JOIN pg_namespace n ON n.oid = p.relnamespace
                JOIN pg_inherits i ON i.inhparent = p.oid
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE n.nspname = %s AND p.relname = 'transactions_partitioned'
                ORDER BY c.relname
                LIMIT 1
                """,
                (_TEST_SCHEMA,),
            )
            row = cur.fetchone()
        if not row:
            pytest.skip(
                "No child partitions found under transactions_partitioned — skipping child path test"
            )
        child_name = row[0]
        result = introspect_table(conn, child_name, schema=_TEST_SCHEMA)
    finally:
        conn.close()
    assert result.partition_key == ["txn_date"], (
        f"expected partition_key=['txn_date'] for child partition {child_name!r}, "
        f"got {result.partition_key!r}"
    )
