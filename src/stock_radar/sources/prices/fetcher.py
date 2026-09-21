"""株価の差分取得ループ。

設計の要点（docs/architecture.md）。

- **進捗管理テーブルは作らない。** 再開対象は `prices_daily` と `fetch_failures` の
  引き算で導出できる。進捗テーブルはこの引き算を書き写すだけで情報が増えない
- **差分取得の開始日を重複させて、株式分割の遡及調整を検知する。**
  yfinance が返す `close` は分割調整済みなので、分割が起きると過去がすべて
  書き換わる。差分追記では古い行が調整前のまま残り、52週高値が実態とかけ離れる
- **失敗した銘柄で全体を止めない。** `fetch_failures` に積んで次の銘柄へ進む
- **時間予算を超えたら打ち切る。** 取れなかった分は次の run に持ち越される
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stock_radar.config import Market, PricesRuntime
from stock_radar.sources.prices.base import Bar, ErrorClass, PriceFetchError, PriceSource
from stock_radar.sources.prices.throttle import (
    PaceState,
    circuit_pause_sec,
    next_interval_sec,
    should_stop,
    sleep_seconds,
)
from stock_radar.storage import bulk_writer, utc_now

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "HOLIDAY_BUFFER_DAYS",
    "SPLIT_TOLERANCE",
    "FetchOutcome",
    "FetchReport",
    "fetch_prices",
    "needs_full_refetch",
    "select_targets",
    "start_date_for",
    "window_start",
]

log = logging.getLogger(__name__)

# 重複期間の終値がこの割合を超えてずれていたら、遡及調整が入ったとみなす。
# 株式分割は2倍以上動くので、丸め誤差（1e-6 程度）とは桁が違う。
SPLIT_TOLERANCE = 0.001

# 営業日を暦日に直すときの祝日ぶんの余裕。米国市場の休場は年9〜10日。
HOLIDAY_BUFFER_DAYS = 14


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    ticker: str
    bars: int
    # 履歴が無くてウィンドウ全体を取った。初回はこれになる。
    initial_fetch: bool = False
    # 重複期間の終値が食い違ったので取り直した。株式分割の遡及調整。
    split_refetch: bool = False
    error_class: ErrorClass | None = None


@dataclass
class FetchReport:
    targets: int = 0
    succeeded: int = 0
    failed: int = 0
    initial_fetches: int = 0
    split_refetches: int = 0
    bars_written: int = 0
    stopped_on_budget: bool = False
    outcomes: list[FetchOutcome] = field(default_factory=list)

    @property
    def coverage(self) -> float | None:
        """株価取得の成功率。

        これが無いと「今週は候補が5件しか出なかった」のが相場のせいなのか
        取得失敗のせいなのか判別できない（docs/architecture.md）。
        """
        if self.targets == 0:
            return None
        return self.succeeded / self.targets


def window_start(today: dt.date, business_days: int) -> dt.date:
    """``business_days`` 営業日ぶんさかのぼった暦日。

    **`window_days` は営業日**（docs/architecture.md「日次1年3ヶ月分（315営業日）」）。
    これを暦日として引くと 315日 ≒ 45週にしかならず、**52週高安が計算できない**。
    週5営業日なので 7/5 倍し、祝日ぶんの余裕を足す。
    """
    return today - dt.timedelta(days=math.ceil(business_days * 7 / 5) + HOLIDAY_BUFFER_DAYS)


# --- 対象の決め方 -----------------------------------------------------------


def select_targets(
    con: duckdb.DuckDBPyConnection,
    runtime: PricesRuntime,
    *,
    market: Market = Market.US,
    today: dt.date,
    limit: int | None = None,
    tickers: Sequence[str] | None = None,
    permanent_error_threshold: int,
) -> list[str]:
    """取得対象の銘柄。

    ``対象 = ユニバース通過銘柄
             − 最新の株価が手元にあるもの
             − 恒久的失敗と判定済みのもの``

    ``limit`` / ``tickers`` で外から絞れる。**全銘柄の実行はデプロイ後**に行うため、
    それまではここで少数に絞って試す。
    """
    fresh_since = today - dt.timedelta(days=runtime.up_to_date_within_days)
    rows = con.execute(
        """
        SELECT u.ticker
        FROM universe u
        LEFT JOIN (
            SELECT ticker, max(date) AS latest FROM prices_daily GROUP BY ticker
        ) p ON p.ticker = u.ticker
        LEFT JOIN fetch_failures f ON f.ticker = u.ticker
        WHERE u.market = ?
          AND u.excluded_reason IS NULL
          AND (p.latest IS NULL OR p.latest < ?)
          -- coalesce が要る。fetch_failures に行が無いと f.error_class が NULL になり、
          -- NOT (NULL AND ...) も NULL になって、失敗していない銘柄まで落ちる。
          AND NOT coalesce(f.error_class = ? AND f.attempt_count >= ?, false)
        ORDER BY u.ticker
        """,
        [market.value, fresh_since, ErrorClass.INVALID_SYMBOL.value, permanent_error_threshold],
    ).fetchall()
    found = [row[0] for row in rows]

    if tickers:
        wanted = {t.strip().upper() for t in tickers if t.strip()}
        found = [t for t in found if t.upper() in wanted]
    if limit is not None:
        found = found[:limit]
    return found


def start_date_for(
    con: duckdb.DuckDBPyConnection, ticker: str, runtime: PricesRuntime, *, today: dt.date
) -> tuple[dt.date, bool]:
    """取得の開始日と、フル取得かどうか。

    履歴があれば `overlap_days` 営業日ぶん重ねて取る。重複期間の終値を既存と
    突き合わせて、遡及調整（株式分割）が入っていないか確かめるため。
    **1銘柄1リクエストなので、5営業日ぶん余計に取ってもコストは変わらない。**
    """
    row = con.execute(
        "SELECT date FROM prices_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1 OFFSET ?",
        [ticker, runtime.overlap_days],
    ).fetchone()
    if row is None:
        # 履歴が無い、または重複ぶんに足りない。ウィンドウ全体を取る。
        return window_start(today, runtime.window_days), True
    return row[0], False


def needs_full_refetch(existing: dict[dt.date, float], bars: Iterable[Bar]) -> bool:
    """重複期間の終値が食い違うか。

    食い違えば遡及調整が入ったということなので、その銘柄だけフル再取得する。
    放置すると古い行が調整前の値のまま残り、**存在しない暴落**として52週高値に
    記録されてタイミング加点が誤る。`volume` も同じ理由で壊れる。
    """
    for bar in bars:
        previous = existing.get(bar.date)
        if previous is None or bar.close is None:
            continue
        scale = max(abs(previous), abs(bar.close))
        if scale == 0:
            continue
        if abs(previous - bar.close) / scale > SPLIT_TOLERANCE:
            return True
    return False


# --- 取得ループ -------------------------------------------------------------

_PRICE_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume")


def fetch_prices(
    con: duckdb.DuckDBPyConnection,
    source: PriceSource,
    runtime: PricesRuntime,
    *,
    market: Market = Market.US,
    today: dt.date | None = None,
    limit: int | None = None,
    tickers: Sequence[str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] = random.random,
) -> FetchReport:
    """対象銘柄を1件ずつ取得して `prices_daily` を更新する。

    ``sleeper`` / ``monotonic`` / ``jitter`` を差し替えられるのはテストのため。
    待ち時間の**計算**は `throttle` の純粋関数が持っていて、ここは結果を渡すだけ。
    """
    day = today if today is not None else dt.date.today()
    targets = select_targets(
        con,
        runtime,
        market=market,
        today=day,
        limit=limit,
        tickers=tickers,
        permanent_error_threshold=runtime.retry.permanent_error_threshold,
    )
    report = FetchReport(targets=len(targets))
    if not targets:
        return report

    pace = PaceState.initial(runtime.throttle)
    started = monotonic()

    for index, ticker in enumerate(targets):
        if should_stop(monotonic() - started, runtime.budget.max_wall_clock_sec):
            report.stopped_on_budget = True
            log.warning(
                "時間予算を超えたので打ち切る（%s/%s 件）。残りは次の run に持ち越す",
                index,
                len(targets),
            )
            break

        if index:
            sleeper(sleep_seconds(pace, runtime.throttle, jitter()))

        outcome = _fetch_one(con, source, runtime, ticker, day)
        report.outcomes.append(outcome)

        if outcome.error_class is None:
            report.succeeded += 1
            report.bars_written += outcome.bars
            report.initial_fetches += int(outcome.initial_fetch)
            report.split_refetches += int(outcome.split_refetch)
        else:
            report.failed += 1

        pace = next_interval_sec(
            pace,
            rate_limited=outcome.error_class is ErrorClass.RATE_LIMITED,
            throttle=runtime.throttle,
        )
        tripped = circuit_pause_sec(pace, runtime.circuit_breaker)
        if tripped is not None:
            pause, pace = tripped
            log.warning("連続して 429 を踏んだので %.0f 分休む", pause / 60)
            sleeper(pause)

    return report


def _fetch_one(
    con: duckdb.DuckDBPyConnection,
    source: PriceSource,
    runtime: PricesRuntime,
    ticker: str,
    today: dt.date,
) -> FetchOutcome:
    start, initial = start_date_for(con, ticker, runtime, today=today)
    full = initial
    split_refetch = False
    try:
        bars = source.fetch(ticker, start, today)
    except PriceFetchError as exc:
        _record_failure(con, ticker, exc)
        return FetchOutcome(ticker, 0, error_class=exc.error_class)

    if not full:
        existing = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT date, close FROM prices_daily WHERE ticker = ? AND date >= ?",
                [ticker, start],
            ).fetchall()
            if row[1] is not None
        }
        if needs_full_refetch(existing, bars):
            log.info("%s は遡及調整が入っている。フル再取得する", ticker)
            full = True
            split_refetch = True
            start = window_start(today, runtime.window_days)
            try:
                bars = source.fetch(ticker, start, today)
            except PriceFetchError as exc:
                _record_failure(con, ticker, exc)
                return FetchOutcome(ticker, 0, error_class=exc.error_class)

    written = _store_bars(con, ticker, bars, replace_from=start if full else None)
    _clear_failure(con, ticker)
    return FetchOutcome(ticker, written, initial_fetch=initial, split_refetch=split_refetch)


def _store_bars(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    bars: Sequence[Bar],
    *,
    replace_from: dt.date | None,
) -> int:
    if not bars and replace_from is None:
        return 0
    con.execute("BEGIN TRANSACTION")
    try:
        if replace_from is not None:
            # 遡及調整の置き換え。調整前の行を残さない。
            con.execute("DELETE FROM prices_daily WHERE ticker = ?", [ticker])
        else:
            con.execute(
                "DELETE FROM prices_daily WHERE ticker = ? AND date >= ?",
                [ticker, min(bar.date for bar in bars)],
            )
        with bulk_writer(con) as make:
            sink = make("prices_daily", _PRICE_COLUMNS)
            for bar in bars:
                sink.write((ticker, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume))
            written = sink.count
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return written


def _record_failure(con: duckdb.DuckDBPyConnection, ticker: str, exc: PriceFetchError) -> None:
    con.execute(
        "INSERT INTO fetch_failures (ticker, error_class, attempt_count, last_attempt, "
        "last_error) VALUES (?, ?, 1, ?, ?) "
        "ON CONFLICT (ticker) DO UPDATE SET "
        "  error_class = excluded.error_class, "
        # 同じ理由で失敗し続けたときだけ数える。理由が変われば数え直す。
        "  attempt_count = CASE WHEN fetch_failures.error_class = excluded.error_class "
        "                      THEN fetch_failures.attempt_count + 1 ELSE 1 END, "
        "  last_attempt = excluded.last_attempt, "
        "  last_error = excluded.last_error",
        [ticker, exc.error_class.value, utc_now(), exc.message[:500]],
    )


def _clear_failure(con: duckdb.DuckDBPyConnection, ticker: str) -> None:
    con.execute("DELETE FROM fetch_failures WHERE ticker = ?", [ticker])
