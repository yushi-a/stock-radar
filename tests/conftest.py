"""複数のテストで共有するフィクスチャ。"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from stock_radar.storage import apply_schema, connect


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    """スキーマ適用済みのインメモリ DuckDB。"""
    connection = connect(":memory:")
    apply_schema(connection)
    yield connection
    connection.close()
