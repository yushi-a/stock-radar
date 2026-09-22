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

    どちらを採るかは normalize.py の責務。ここで先に潰すと判断できなくなる。
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


def test_facts_annual_keeps_two_periods_that_share_an_end_and_filing(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """同じ提出書類が period_start 違いで2回報告してきても、両方残ること。

    Coeur Mining の2011年度は同じ10-K に 356日（2.4億ドル）と 365日（10.2億ドル）が
    併記されている。一意制約を付けると片方を黙って捨てることになる。
    """
    for start, value in (("2011-01-01", 1_021_200_000.0), ("2011-01-10", 246_911_000.0)):
        con.execute(
            "INSERT INTO facts_annual "
            "(cik, fiscal_year, period_start, period_end, concept, value, unit, accn, filed_at) "
            "VALUES (215466, 2011, ?, DATE '2011-12-31', 'SalesRevenueGoodsNet', ?, "
            "'USD', '0001193125-12-075636', DATE '2012-02-23')",
            [start, value],
        )
    values = {row[0] for row in con.execute("SELECT value FROM facts_annual").fetchall()}
    assert values == {1_021_200_000.0, 246_911_000.0}


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


# --- 時刻の扱い -------------------------------------------------------------


def test_timestamps_round_trip_without_pytz(con: duckdb.DuckDBPyConnection) -> None:
    """TIMESTAMP 列を Python に読み戻せること。

    TIMESTAMPTZ だと DuckDB が pytz を要求して落ちる。依存を増やさないために
    tz 無しの TIMESTAMP に UTC を入れる方針にしてある。
    """
    from stock_radar.storage import utc_now

    stamp = utc_now()
    con.execute(
        "INSERT INTO universe (market, ticker, updated_at) VALUES ('us', 'AAPL', ?)", [stamp]
    )
    assert con.execute("SELECT updated_at FROM universe").fetchone()[0] == stamp


def test_utc_now_is_naive() -> None:
    from stock_radar.storage import utc_now

    assert utc_now().tzinfo is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # JST 21:00 == UTC 12:00
        (
            dt.datetime(2026, 9, 20, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
            dt.datetime(2026, 9, 20, 12, 0),
        ),
        (dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC), dt.datetime(2026, 9, 20, 12, 0)),
        # naive はすでに UTC として扱う。
        (dt.datetime(2026, 9, 20, 12, 0), dt.datetime(2026, 9, 20, 12, 0)),
    ],
)
def test_as_utc_naive(value: dt.datetime, expected: dt.datetime) -> None:
    from stock_radar.storage import as_utc_naive

    assert as_utc_naive(value) == expected


# --- ファイルの作り直し -----------------------------------------------------


def test_compact_preserves_everything(tmp_path: Path) -> None:
    """作り直してもデータとスキーマバージョンが残ること。"""
    from stock_radar.storage import compact

    path = tmp_path / "stock_radar.duckdb"
    with open_database(path) as con:
        con.execute(
            "INSERT INTO prices_daily VALUES ('AAPL', DATE '2026-09-18', 1, 2, 0.5, 1.5, 9)"
        )

    compact(path)

    with open_database(path) as con:
        assert con.execute("SELECT volume FROM prices_daily").fetchone()[0] == 9
        assert schema_version(con) == SCHEMA_VERSION
        assert set(ALL_TABLES) <= _table_names(con)


def test_compact_shrinks_after_a_bulk_replace(tmp_path: Path) -> None:
    """DELETE だけではファイルが縮まないので、作り直しで回収できること。

    DuckDB は解放ブロックをファイルに返さず、VACUUM も CHECKPOINT も効かない。
    週次で 125万行を入れ替える facts テーブルではこれが効いてくる。
    """
    from stock_radar.storage import compact

    path = tmp_path / "stock_radar.duckdb"
    # 行単位の INSERT は DuckDB では遅いので SQL 側で作る。
    fill = (
        "INSERT INTO prices_daily "
        "SELECT 'T' || i, DATE '2026-01-01', 1, 2, 0.5, 1.5, i FROM range(200000) t(i)"
    )
    with open_database(path) as con:
        for _ in range(3):
            con.execute("DELETE FROM prices_daily")
            con.execute(fill)
        con.execute("CHECKPOINT")

    before, after = compact(path)
    assert after < before

    with open_database(path) as con:
        assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 200_000


def _with_a_screen_run(path: Path) -> None:
    """スクリーニング結果が1件ある DB を作る（screen_results → screen_runs の参照つき）。"""
    with open_database(path) as con:
        con.execute(
            "INSERT INTO screen_runs (run_at, market, criteria_snapshot) "
            "VALUES (TIMESTAMP '2026-09-21 12:00:00', 'us', '{}')"
        )
        con.execute(
            "INSERT INTO screen_results (run_id, market, ticker, track) "
            "VALUES (1, 'us', 'AAPL', 'A')"
        )


def test_compact_keeps_the_screening_history(tmp_path: Path) -> None:
    """外部キーのある行を持つ DB を作り直せること。

    `COPY FROM DATABASE` はテーブルを外部キーの順に並べてくれず、`screen_results` を
    `screen_runs` より先に写して落ちていた（issue #49）。スクリーニングを1回でも
    実行した後は毎回踏むため、週次運用では2回目以降が必ず失敗していた。
    """
    from stock_radar.storage import compact

    path = tmp_path / "stock_radar.duckdb"
    _with_a_screen_run(path)

    compact(path)

    with open_database(path) as con:
        assert con.execute("SELECT count(*) FROM screen_runs").fetchone()[0] == 1
        assert con.execute("SELECT ticker FROM screen_results").fetchone()[0] == "AAPL"


def test_compact_continues_the_run_id_sequence(tmp_path: Path) -> None:
    """作り直した後の採番が続きから始まること。

    1 に戻ると既存の run_id と主キーで衝突する。
    """
    from stock_radar.storage import compact

    path = tmp_path / "stock_radar.duckdb"
    _with_a_screen_run(path)

    compact(path)

    with open_database(path) as con:
        run_id = con.execute(
            "INSERT INTO screen_runs (run_at, market, criteria_snapshot) "
            "VALUES (TIMESTAMP '2026-09-28 12:00:00', 'us', '{}') RETURNING run_id"
        ).fetchone()[0]
    assert run_id == 2


def test_compact_leaves_the_original_alone_on_failure(tmp_path: Path) -> None:
    from stock_radar.storage import StorageError, compact

    path = tmp_path / "missing.duckdb"
    with pytest.raises((StorageError, OSError)):
        compact(path)
