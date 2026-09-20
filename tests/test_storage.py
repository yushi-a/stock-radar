"""DuckDB スキーマ。

見るのは「冪等に作れること」「捨ててよいものと捨ててはいけないものを取り違えないこと」
「壊れた値を弾く制約が実際に効いていること」の3点。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest

from stock_radar.config import Market, load_criteria
from stock_radar.storage import (
    ALL_TABLES,
    PERSISTENT_TABLES,
    REBUILDABLE_TABLES,
    SCHEMA_VERSION,
    SchemaVersionError,
    StorageError,
    apply_schema,
    connect,
    drop_rebuildable,
    open_database,
    schema_version,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CRITERIA_PATH = REPO_ROOT / "config" / "criteria.yaml"


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = connect(":memory:")
    apply_schema(connection)
    yield connection
    connection.close()


def _table_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {row[0] for row in rows}


# --- スキーマの適用 ---------------------------------------------------------


def test_apply_schema_creates_every_table(con: duckdb.DuckDBPyConnection) -> None:
    assert set(ALL_TABLES) <= _table_names(con)


def test_table_groups_do_not_overlap() -> None:
    assert not set(REBUILDABLE_TABLES) & set(PERSISTENT_TABLES)


def test_apply_schema_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 100)")
    apply_schema(con)
    apply_schema(con)
    assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 1
    assert schema_version(con) == SCHEMA_VERSION


def test_schema_version_is_none_before_apply() -> None:
    with connect(":memory:") as con:
        assert schema_version(con) is None


def test_version_mismatch_refuses_instead_of_rebuilding(con: duckdb.DuckDBPyConnection) -> None:
    """黙って作り直さないこと。prices_daily のフル取得には丸1日かかる。"""
    con.execute("INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 100)")
    con.execute("UPDATE schema_meta SET value = '999' WHERE key = 'version'")
    with pytest.raises(SchemaVersionError, match="999"):
        apply_schema(con)
    assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 1


# --- ファイルとして開く -----------------------------------------------------


def test_open_database_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "stock_radar.duckdb"
    with open_database(path) as con:
        assert set(ALL_TABLES) <= _table_names(con)
    assert path.exists()


def test_data_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "stock_radar.duckdb"
    with open_database(path) as con:
        con.execute(
            "INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 9)"
        )
    with open_database(path) as con:
        assert con.execute("SELECT volume FROM prices_daily").fetchone()[0] == 9


def test_open_database_read_only_rejects_version_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "stock_radar.duckdb"
    with open_database(path) as con:
        con.execute("UPDATE schema_meta SET value = '999' WHERE key = 'version'")
    with pytest.raises(SchemaVersionError), open_database(path, read_only=True):
        pass


def test_connect_reports_unopenable_file(tmp_path: Path) -> None:
    path = tmp_path / "not_a_database.duckdb"
    path.write_text("これは DuckDB ファイルではない", encoding="utf-8")
    with pytest.raises(StorageError, match="開けない"):
        connect(path)


# --- 再生成可 / 永続 --------------------------------------------------------


def test_drop_rebuildable_keeps_persistent_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 100)")
    con.execute("INSERT INTO universe (market, ticker, updated_at) VALUES ('us', 'AAPL', now())")

    drop_rebuildable(con)

    remaining = _table_names(con)
    assert not set(REBUILDABLE_TABLES) & remaining
    assert set(PERSISTENT_TABLES) <= remaining
    assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 1

    # 捨てた後にもう一度適用すれば空で復活する。
    apply_schema(con)
    assert set(ALL_TABLES) <= _table_names(con)
    assert con.execute("SELECT count(*) FROM universe").fetchone()[0] == 0


# --- 制約 -------------------------------------------------------------------


def test_prices_daily_rejects_duplicate_ticker_date(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 100)")
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 100)"
        )


def test_universe_rejects_unknown_market(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO universe (market, ticker, updated_at) VALUES ('uk', 'X', now())")


def test_universe_accepts_every_configured_market(con: duckdb.DuckDBPyConnection) -> None:
    """config.Market に市場を足したらスキーマの CHECK も一緒に広がること。"""
    for market in Market:
        con.execute(
            "INSERT INTO universe (market, ticker, updated_at) VALUES (?, 'X', now())",
            [market.value],
        )
    assert con.execute("SELECT count(*) FROM universe").fetchone()[0] == len(list(Market))


def test_fetch_failures_rejects_unknown_error_class(con: duckdb.DuckDBPyConnection) -> None:
    """一時的と恒久的を区別しないと上場廃止銘柄を毎週リトライし続ける。"""
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO fetch_failures (ticker, error_class, last_attempt) "
            "VALUES ('AAPL', 'typo', now())"
        )


def test_screen_results_rejects_unknown_track(con: duckdb.DuckDBPyConnection) -> None:
    run_id = _insert_run(con)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO screen_results (run_id, market, ticker, track) VALUES (?, 'us', 'X', 'C')",
            [run_id],
        )


def test_screen_results_requires_an_existing_run(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO screen_results (run_id, market, ticker, track) "
            "VALUES (999, 'us', 'X', 'A')"
        )


def test_facts_annual_keeps_both_filings_of_one_fiscal_year(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """同じ年度が元の10-K と修正再提出で2回出ても、どちらも残ること。

    どちらを採るかは Phase 2a で決める。ここで先に潰してしまうと判断できなくなる。
    """
    for accn, value in (("0000-24-000001", 100.0), ("0000-25-000002", 105.0)):
        con.execute(
            "INSERT INTO facts_annual "
            "(cik, fiscal_year, period_start, period_end, concept, value, unit, accn, filed_at) "
            "VALUES (320193, 2024, DATE '2023-10-01', DATE '2024-09-28', 'Revenues', ?, "
            "'USD', ?, DATE '2024-11-01')",
            [value, accn],
        )
    assert con.execute("SELECT count(*) FROM facts_annual").fetchone()[0] == 2


# --- JSON / リスト列 --------------------------------------------------------


def _insert_run(con: duckdb.DuckDBPyConnection, snapshot: dict | None = None) -> int:
    row = con.execute(
        "INSERT INTO screen_runs (run_at, market, criteria_snapshot) VALUES (?, 'us', ?) "
        "RETURNING run_id",
        [dt.datetime(2026, 9, 20, tzinfo=dt.UTC), json.dumps(snapshot or {})],
    ).fetchone()
    return int(row[0])


def test_run_id_is_assigned_automatically(con: duckdb.DuckDBPyConnection) -> None:
    first = _insert_run(con)
    second = _insert_run(con)
    assert second > first


def test_criteria_snapshot_round_trips(con: duckdb.DuckDBPyConnection) -> None:
    """screen_runs に入れた閾値をそのまま読み戻せること。

    これが崩れると「この結果はどの閾値で出たか」を後から再現できない。
    """
    snapshot = load_criteria(CRITERIA_PATH).snapshot()
    run_id = _insert_run(con, snapshot)
    stored = con.execute(
        "SELECT criteria_snapshot FROM screen_runs WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    assert json.loads(stored) == snapshot


def test_passed_filters_round_trips_as_a_list(con: duckdb.DuckDBPyConnection) -> None:
    run_id = _insert_run(con)
    con.execute(
        "INSERT INTO screen_results (run_id, market, ticker, track, passed_filters) "
        "VALUES (?, 'us', 'AAPL', 'A', ?)",
        [run_id, ["revenue_cagr_3y", "op_margin"]],
    )
    stored = con.execute("SELECT passed_filters FROM screen_results").fetchone()[0]
    assert stored == ["revenue_cagr_3y", "op_margin"]
