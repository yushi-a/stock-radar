"""ユニバース確定（前半：ティッカーと取引所）。

フィクスチャは実物の ``company_tickers_exchange.json`` から9行だけ抜いたもの。
SEC データに再配布制限は無い（docs/architecture.md）。
Nasdaq / NYSE / CBOE / OTC / 取引所なし / 複数クラス株をすべて含む。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest

from stock_radar.config import Market
from stock_radar.sources.sec.client import Response, SecError
from stock_radar.sources.sec.universe import (
    COMPANY_TICKERS_EXCHANGE_URL,
    ExclusionReason,
    TickerRow,
    exchange_exclusion,
    exclusion_counts,
    fetch_ticker_rows,
    parse_company_tickers_exchange,
    replace_ticker_rows,
)
from tests.helpers import FakeTransport, make_client

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "sec" / "company_tickers_exchange_sample.json"
)


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- 取引所による除外 -------------------------------------------------------


@pytest.mark.parametrize("exchange", ["Nasdaq", "NYSE", "CBOE", "nasdaq", " NYSE "])
def test_listed_exchanges_are_kept(exchange: str) -> None:
    assert exchange_exclusion(exchange) is None


def test_otc_is_excluded() -> None:
    assert exchange_exclusion("OTC") is ExclusionReason.OTC


@pytest.mark.parametrize("exchange", [None, "", "   "])
def test_missing_exchange_is_excluded(exchange: str | None) -> None:
    """実データに219件ある。上場取引所が分からないので残さない。"""
    assert exchange_exclusion(exchange) is ExclusionReason.NO_EXCHANGE


def test_unknown_exchange_is_treated_as_otc() -> None:
    """知らない取引所を黙って残さない。"""
    assert exchange_exclusion("Some New Venue") is ExclusionReason.OTC


# --- パース -----------------------------------------------------------------


def test_parses_the_fixture(payload: dict) -> None:
    rows = parse_company_tickers_exchange(payload)
    assert len(rows) == 9
    assert TickerRow(320193, "Apple Inc.", "AAPL", "Nasdaq") in rows
    assert TickerRow(2103884, "Compass Sub North, Inc.", "CONE", None) in rows


def test_multi_class_shares_stay_as_separate_rows(payload: dict) -> None:
    """同じ CIK に複数ティッカー。universe の主キーは (market, ticker) なので行は分かれる。"""
    rows = parse_company_tickers_exchange(payload)
    alphabet = [row for row in rows if row.cik == 1652044]
    assert {row.ticker for row in alphabet} == {"GOOG", "GOOGL"}


def test_columns_are_looked_up_by_name(payload: dict) -> None:
    """SEC が列順を変えても静かに壊れないこと。"""
    order = [3, 0, 2, 1]
    reordered = {
        "fields": [payload["fields"][i] for i in order],
        "data": [[row[i] for i in order] for row in payload["data"]],
    }
    assert parse_company_tickers_exchange(reordered) == parse_company_tickers_exchange(payload)


@pytest.mark.parametrize(
    "broken",
    [
        [],
        {"data": []},
        {"fields": ["cik", "name", "ticker"], "data": []},
        {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "n"]]},
        {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "n", "", "NYSE"]]},
    ],
)
def test_unexpected_shapes_fail_loudly(broken: object) -> None:
    """形が変わったときに空を返さないこと。静かに0件になるのが一番困る。"""
    with pytest.raises(SecError):
        parse_company_tickers_exchange(broken)


def test_fetch_uses_the_documented_url(payload: dict) -> None:
    transport = FakeTransport([Response(200, json.dumps(payload).encode())])
    rows = fetch_ticker_rows(make_client(transport))
    assert transport.requests[0][0] == COMPANY_TICKERS_EXCHANGE_URL
    assert len(rows) == 9


# --- universe への書き込み --------------------------------------------------


def test_replace_writes_every_row_with_its_reason(
    con: duckdb.DuckDBPyConnection, payload: dict
) -> None:
    """除外した銘柄も行として残ること。落とすと除外理由別の件数が出せない。"""
    rows = parse_company_tickers_exchange(payload)
    assert replace_ticker_rows(con, rows) == 9

    counts = exclusion_counts(con)
    assert counts == {None: 7, "otc": 1, "no_exchange": 1}


def test_replace_drops_rows_that_left_the_listing(
    con: duckdb.DuckDBPyConnection, payload: dict
) -> None:
    """上場廃止で SEC の一覧から消えたティッカーが残り続けないこと。"""
    rows = parse_company_tickers_exchange(payload)
    replace_ticker_rows(con, rows)
    replace_ticker_rows(con, [row for row in rows if row.ticker != "AAPL"])

    tickers = {row[0] for row in con.execute("SELECT ticker FROM universe").fetchall()}
    assert "AAPL" not in tickers
    assert len(tickers) == 8


def test_replace_is_idempotent(con: duckdb.DuckDBPyConnection, payload: dict) -> None:
    rows = parse_company_tickers_exchange(payload)
    replace_ticker_rows(con, rows)
    replace_ticker_rows(con, rows)
    assert con.execute("SELECT count(*) FROM universe").fetchone()[0] == 9


def test_replace_only_touches_its_own_market(con: duckdb.DuckDBPyConnection, payload: dict) -> None:
    con.execute("INSERT INTO universe (market, ticker, updated_at) VALUES ('jp', '7203', now())")
    replace_ticker_rows(con, parse_company_tickers_exchange(payload), market=Market.US)
    assert con.execute("SELECT count(*) FROM universe WHERE market = 'jp'").fetchone()[0] == 1


def test_replace_records_the_timestamp(con: duckdb.DuckDBPyConnection, payload: dict) -> None:
    replace_ticker_rows(
        con,
        parse_company_tickers_exchange(payload),
        now=dt.datetime(2026, 9, 20, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
    )
    stored = con.execute("SELECT DISTINCT updated_at FROM universe").fetchall()
    # tz 付きで渡しても UTC の naive として入る。JST 21:00 == UTC 12:00。
    assert stored == [(dt.datetime(2026, 9, 20, 12, 0),)]


def test_failed_replace_leaves_the_previous_rows(
    con: duckdb.DuckDBPyConnection, payload: dict
) -> None:
    """途中で落ちてユニバースが空のまま残らないこと。"""
    rows = parse_company_tickers_exchange(payload)
    replace_ticker_rows(con, rows)

    broken = [*rows, TickerRow(1, "dup", "AAPL", "Nasdaq")]  # 主キー重複
    with pytest.raises(duckdb.ConstraintException):
        replace_ticker_rows(con, broken)

    assert con.execute("SELECT count(*) FROM universe").fetchone()[0] == 9
