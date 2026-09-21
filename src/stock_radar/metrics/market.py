"""株価から出す指標。

`metrics/fundamentals.py` と同じで**純粋関数**。DB も設定も触らない。
書き込みだけが DuckDB を見る。

時価総額は `dei` ではなく `fundamentals.shares_outstanding` × 直近終値で自前計算する。
yfinance の `marketCap` には依存せず、照合にのみ使う（「一次情報優先」）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stock_radar.metrics.fundamentals import ratio
from stock_radar.storage import bulk_writer

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "TRADING_DAYS_52W",
    "DailyBar",
    "MarketMetrics",
    "avg_daily_value",
    "compute_market_metrics",
    "drawdown_from_high",
    "fcf_yield",
    "market_cap",
    "multi_class_ciks",
    "psr",
    "range_position",
    "rebuild_market_metrics",
    "store_market_metrics",
]

# 52週 ≒ 252営業日。取得ウィンドウ（315営業日）はこれを含む長さにしてある。
TRADING_DAYS_52W = 252


@dataclass(frozen=True, slots=True)
class DailyBar:
    date: dt.date
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    ticker: str
    as_of: dt.date
    market_cap: float | None = None
    avg_daily_value: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    range_position_52w: float | None = None
    drawdown_from_52w_high: float | None = None
    latest_price_date: dt.date | None = None


def market_cap(shares_outstanding: float | None, close: float | None) -> float | None:
    """時価総額 = 発行済株式数 × 直近終値。

    **複数クラス株では近似になる。** `companyfacts` に軸付きファクトが無いため
    株数は全クラス合計しか取れず、株価は片方のクラスのものになる
    （docs/xbrl-findings.md の C）。GOOG と GOOGL は1%以内なので実害は小さいが、
    どの銘柄が近似かは `multi_class_ciks()` で分かるようにしてある。
    """
    if shares_outstanding is None or close is None:
        return None
    if shares_outstanding <= 0 or close <= 0:
        return None
    return shares_outstanding * close


def avg_daily_value(bars: Sequence[DailyBar]) -> float | None:
    """平均売買代金 = 終値 × 出来高 の平均。"""
    values = [
        bar.close * bar.volume for bar in bars if bar.close is not None and bar.volume is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def range_position(close: float | None, low: float | None, high: float | None) -> float | None:
    """52週レンジのどこにいるか。高値で 1.0、安値で 0.0。"""
    if close is None or low is None or high is None:
        return None
    span = high - low
    if span <= 0:
        return None
    return (close - low) / span


def drawdown_from_high(close: float | None, high: float | None) -> float | None:
    """52週高値からの下落率。高値の半分なら -0.5。"""
    if close is None or high is None or high <= 0:
        return None
    return close / high - 1


def psr(market_cap_value: float | None, revenue: float | None) -> float | None:
    return ratio(market_cap_value, revenue)


def fcf_yield(fcf: float | None, market_cap_value: float | None) -> float | None:
    return ratio(fcf, market_cap_value)


def compute_market_metrics(
    ticker: str,
    bars: Sequence[DailyBar],
    *,
    shares_outstanding: float | None,
    as_of: dt.date,
    window: int = TRADING_DAYS_52W,
) -> MarketMetrics | None:
    """1銘柄ぶん。``bars`` は日付順（古い順）。

    52週の窓に満たないデータ量でも落とさず、あるぶんで計算する。
    どこまでのデータで出したかは ``latest_price_date`` で分かる。
    """
    ordered = sorted(bars, key=lambda b: b.date)
    if not ordered:
        return None
    recent = ordered[-window:]
    closes = [b.close for b in recent if b.close is not None]
    highs = [b.high for b in recent if b.high is not None]
    lows = [b.low for b in recent if b.low is not None]

    latest_close = closes[-1] if closes else None
    high = max(highs) if highs else None
    low = min(lows) if lows else None

    return MarketMetrics(
        ticker=ticker,
        as_of=as_of,
        market_cap=market_cap(shares_outstanding, latest_close),
        avg_daily_value=avg_daily_value(recent),
        high_52w=high,
        low_52w=low,
        range_position_52w=range_position(latest_close, low, high),
        drawdown_from_52w_high=drawdown_from_high(latest_close, high),
        latest_price_date=ordered[-1].date,
    )


# --- DuckDB との受け渡し ----------------------------------------------------

_COLUMNS = (
    "ticker",
    "as_of",
    "market_cap",
    "avg_daily_value",
    "high_52w",
    "low_52w",
    "range_position_52w",
    "drawdown_from_52w_high",
    "latest_price_date",
)


def multi_class_ciks(con: duckdb.DuckDBPyConnection) -> set[int]:
    """複数クラス株の CIK。

    テーブルに列を足さずに導出できる。同じ CIK に複数ティッカーがあれば複数クラス株。
    これらの時価総額は近似なので、評価スキルに渡すときにフラグを立てる。
    """
    rows = con.execute(
        "SELECT cik FROM universe WHERE excluded_reason IS NULL AND cik IS NOT NULL "
        "GROUP BY cik HAVING count(DISTINCT ticker) > 1"
    ).fetchall()
    return {int(row[0]) for row in rows}


def store_market_metrics(con: duckdb.DuckDBPyConnection, rows: Iterable[MarketMetrics]) -> int:
    """`market_metrics` を入れ替える。再生成可なので差分にしない。"""
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM market_metrics")
        with bulk_writer(con) as make:
            sink = make("market_metrics", _COLUMNS)
            for row in rows:
                sink.write(
                    (
                        row.ticker,
                        row.as_of,
                        row.market_cap,
                        row.avg_daily_value,
                        row.high_52w,
                        row.low_52w,
                        row.range_position_52w,
                        row.drawdown_from_52w_high,
                        row.latest_price_date,
                    )
                )
            written = sink.count
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return written


_BARS_SQL = (
    "SELECT p.ticker, p.date, p.high, p.low, p.close, p.volume, f.shares_outstanding "
    "FROM prices_daily p "
    "LEFT JOIN universe u ON u.ticker = p.ticker AND u.market = ? "
    "LEFT JOIN ( "
    "  SELECT cik, shares_outstanding FROM fundamentals f1 "
    "  WHERE f1.period_end = (SELECT max(period_end) FROM fundamentals f2 WHERE f2.cik = f1.cik) "
    ") f ON f.cik = u.cik "
    "ORDER BY p.ticker, p.date"
)


def rebuild_market_metrics(
    con: duckdb.DuckDBPyConnection, *, market: str = "us", as_of: dt.date | None = None
) -> int:
    """`prices_daily` と `fundamentals` から `market_metrics` を作り直す。"""
    day = as_of if as_of is not None else dt.date.today()
    produced: list[MarketMetrics] = []
    current: str | None = None
    bars: list[DailyBar] = []
    shares: float | None = None

    for ticker, date, high, low, close, volume, share_count in con.execute(
        _BARS_SQL, [market]
    ).fetchall():
        if current is not None and ticker != current:
            metrics = compute_market_metrics(current, bars, shares_outstanding=shares, as_of=day)
            if metrics is not None:
                produced.append(metrics)
            bars = []
        current = ticker
        shares = share_count
        bars.append(DailyBar(date=date, high=high, low=low, close=close, volume=volume))

    if current is not None:
        metrics = compute_market_metrics(current, bars, shares_outstanding=shares, as_of=day)
        if metrics is not None:
            produced.append(metrics)

    return store_market_metrics(con, produced)
