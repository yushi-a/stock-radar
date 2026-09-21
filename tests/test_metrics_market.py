"""株価から出す指標。境界値が本番なのは `fundamentals` と同じ。"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from stock_radar.metrics.market import (
    TRADING_DAYS_52W,
    DailyBar,
    avg_daily_value,
    compute_market_metrics,
    drawdown_from_high,
    fcf_yield,
    market_cap,
    multi_class_ciks,
    psr,
    range_position,
    rebuild_market_metrics,
    store_market_metrics,
)
from stock_radar.sources.sec.universe import TickerRow, replace_ticker_rows

D = dt.date
AS_OF = D(2026, 9, 21)
START = D(2026, 1, 1)


def bars(closes: list[float], *, start: dt.date = START) -> list[DailyBar]:
    return [
        DailyBar(date=start + dt.timedelta(days=i), high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


# --- 時価総額 ---------------------------------------------------------------


def test_market_cap() -> None:
    assert market_cap(1_000_000.0, 25.0) == 25_000_000.0


@pytest.mark.parametrize(
    ("shares", "close"),
    [(None, 25.0), (1_000.0, None), (0.0, 25.0), (1_000.0, 0.0), (-1.0, 25.0)],
)
def test_market_cap_is_none_when_undefined(shares: float | None, close: float | None) -> None:
    assert market_cap(shares, close) is None


# --- 平均売買代金 -----------------------------------------------------------


def test_avg_daily_value() -> None:
    rows = [
        DailyBar(D(2026, 1, 1), 10.0, 10.0, 10.0, 100),
        DailyBar(D(2026, 1, 2), 20.0, 20.0, 20.0, 200),
    ]
    assert avg_daily_value(rows) == pytest.approx((10 * 100 + 20 * 200) / 2)


def test_avg_daily_value_skips_missing_rows() -> None:
    rows = [
        DailyBar(D(2026, 1, 1), 10.0, 10.0, 10.0, 100),
        DailyBar(D(2026, 1, 2), None, None, None, None),
    ]
    assert avg_daily_value(rows) == pytest.approx(1000.0)


def test_avg_daily_value_without_usable_rows() -> None:
    assert avg_daily_value([]) is None
    assert avg_daily_value([DailyBar(D(2026, 1, 1), None, None, None, None)]) is None


def test_zero_volume_counts_as_zero_not_missing() -> None:
    """出来高ゼロは「取引が無かった」であって欠損ではない。"""
    rows = [
        DailyBar(D(2026, 1, 1), 10.0, 10.0, 10.0, 0),
        DailyBar(D(2026, 1, 2), 10.0, 10.0, 10.0, 200),
    ]
    assert avg_daily_value(rows) == pytest.approx(1000.0)


# --- レンジ内位置 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("close", "low", "high", "expected"),
    [(150.0, 100.0, 150.0, 1.0), (100.0, 100.0, 150.0, 0.0), (125.0, 100.0, 150.0, 0.5)],
)
def test_range_position(close: float, low: float, high: float, expected: float) -> None:
    assert range_position(close, low, high) == pytest.approx(expected)


def test_range_position_without_a_range() -> None:
    """高値と安値が同じ。位置を定義できない。"""
    assert range_position(100.0, 100.0, 100.0) is None


def test_range_position_with_missing_input() -> None:
    assert range_position(None, 100.0, 150.0) is None


# --- 高値からの下落率 -------------------------------------------------------


def test_drawdown_from_high() -> None:
    assert drawdown_from_high(50.0, 100.0) == pytest.approx(-0.5)
    assert drawdown_from_high(100.0, 100.0) == pytest.approx(0.0)


def test_drawdown_with_zero_high() -> None:
    assert drawdown_from_high(50.0, 0.0) is None


# --- 割安度 -----------------------------------------------------------------


def test_psr_and_fcf_yield() -> None:
    assert psr(1_000.0, 250.0) == pytest.approx(4.0)
    assert fcf_yield(50.0, 1_000.0) == pytest.approx(0.05)


def test_psr_with_zero_revenue() -> None:
    assert psr(1_000.0, 0.0) is None


# --- まとめて計算 -----------------------------------------------------------


def test_compute_market_metrics() -> None:
    metrics = compute_market_metrics(
        "AAA", bars([100.0, 150.0, 120.0]), shares_outstanding=1_000.0, as_of=AS_OF
    )
    assert metrics is not None
    assert metrics.high_52w == 150.0
    assert metrics.low_52w == 100.0
    assert metrics.market_cap == pytest.approx(120_000.0)
    assert metrics.range_position_52w == pytest.approx(0.4)
    assert metrics.drawdown_from_52w_high == pytest.approx(-0.2)
    assert metrics.latest_price_date == D(2026, 1, 3)


def test_only_the_last_52_weeks_are_used() -> None:
    """ウィンドウより古い高値を52週高値にしない。"""
    old_spike = [999.0] + [100.0] * TRADING_DAYS_52W
    metrics = compute_market_metrics("AAA", bars(old_spike), shares_outstanding=1.0, as_of=AS_OF)
    assert metrics is not None
    assert metrics.high_52w == 100.0


def test_short_history_still_computes() -> None:
    """52週の窓に満たなくても落とさず、あるぶんで出す。"""
    metrics = compute_market_metrics(
        "AAA", bars([100.0, 110.0]), shares_outstanding=1_000.0, as_of=AS_OF
    )
    assert metrics is not None
    assert metrics.high_52w == 110.0
    assert metrics.latest_price_date == D(2026, 1, 2)


def test_missing_shares_leaves_market_cap_none() -> None:
    metrics = compute_market_metrics("AAA", bars([100.0]), shares_outstanding=None, as_of=AS_OF)
    assert metrics is not None
    assert metrics.market_cap is None
    assert metrics.high_52w == 100.0


def test_no_bars_returns_none() -> None:
    assert compute_market_metrics("AAA", [], shares_outstanding=1.0, as_of=AS_OF) is None


def test_bars_are_sorted_before_use() -> None:
    unordered = list(reversed(bars([100.0, 150.0, 120.0])))
    metrics = compute_market_metrics("AAA", unordered, shares_outstanding=1.0, as_of=AS_OF)
    assert metrics is not None
    assert metrics.latest_price_date == D(2026, 1, 3)
    assert metrics.market_cap == pytest.approx(120.0)


# --- 複数クラス株 -----------------------------------------------------------


def test_multi_class_ciks_needs_no_extra_column(con: duckdb.DuckDBPyConnection) -> None:
    """同じ CIK に複数ティッカーがあれば複数クラス株。列を足さずに導出できる。"""
    replace_ticker_rows(
        con,
        [
            TickerRow(1652044, "Alphabet", "GOOGL", "Nasdaq"),
            TickerRow(1652044, "Alphabet", "GOOG", "Nasdaq"),
            TickerRow(320193, "Apple", "AAPL", "Nasdaq"),
        ],
    )
    assert multi_class_ciks(con) == {1652044}


# --- DuckDB との受け渡し ----------------------------------------------------


def test_store_and_rebuild(con: duckdb.DuckDBPyConnection) -> None:
    replace_ticker_rows(con, [TickerRow(1, "A", "AAA", "Nasdaq")])
    con.execute(
        "INSERT INTO fundamentals (cik, fiscal_year, period_end, shares_outstanding) "
        "VALUES (1, 2024, DATE '2024-12-31', 1000)"
    )
    con.executemany(
        "INSERT INTO prices_daily VALUES ('AAA', ?, ?, ?, ?, ?, 100)",
        [(b.date, b.close, b.high, b.low, b.close) for b in bars([100.0, 150.0, 120.0])],
    )

    assert rebuild_market_metrics(con, as_of=AS_OF) == 1
    row = con.execute(
        "SELECT market_cap, high_52w, low_52w, latest_price_date FROM market_metrics"
    ).fetchone()
    assert row == (120_000.0, 150.0, 100.0, D(2026, 1, 3))


def test_rebuild_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    replace_ticker_rows(con, [TickerRow(1, "A", "AAA", "Nasdaq")])
    con.executemany(
        "INSERT INTO prices_daily VALUES ('AAA', ?, ?, ?, ?, ?, 100)",
        [(b.date, b.close, b.high, b.low, b.close) for b in bars([100.0, 150.0])],
    )
    assert rebuild_market_metrics(con, as_of=AS_OF) == 1
    assert rebuild_market_metrics(con, as_of=AS_OF) == 1
    assert con.execute("SELECT count(*) FROM market_metrics").fetchone()[0] == 1


def test_store_empty(con: duckdb.DuckDBPyConnection) -> None:
    assert store_market_metrics(con, []) == 0
