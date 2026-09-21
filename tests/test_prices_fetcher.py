"""株価の差分取得ループ。

実 API は叩かない。偽の `PriceSource` に対して、差分・フル取得の分岐、
遡及調整の検知、失敗の積み方、時間予算での打ち切りを確かめる。
待たないよう `sleeper` を差し替える。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from stock_radar.config import PricesRuntime, load_runtime
from stock_radar.sources.prices.base import Bar, ErrorClass, PriceFetchError
from stock_radar.sources.prices.fetcher import (
    fetch_prices,
    needs_full_refetch,
    select_targets,
    start_date_for,
)
from stock_radar.sources.sec.universe import TickerRow, replace_ticker_rows

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.yaml"
TODAY = dt.date(2026, 9, 21)
D = dt.date


@pytest.fixture
def runtime() -> PricesRuntime:
    return load_runtime(RUNTIME_PATH).prices


class FakeSource:
    """決めたバーを返す偽ソース。銘柄ごとに失敗も仕込める。"""

    def __init__(
        self,
        bars: dict[str, list[Bar]] | None = None,
        errors: dict[str, PriceFetchError] | None = None,
    ) -> None:
        self.bars = bars or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        self.calls.append((ticker, start, end))
        if ticker in self.errors:
            raise self.errors[ticker]
        return [b for b in self.bars.get(ticker, []) if start <= b.date <= end]


def bar(date: dt.date, close: float, volume: int = 1000) -> Bar:
    return Bar(date=date, open=close, high=close, low=close, close=close, volume=volume)


def seed(con: duckdb.DuckDBPyConnection, tickers: list[str]) -> None:
    replace_ticker_rows(con, [TickerRow(i + 1, t, t, "Nasdaq") for i, t in enumerate(tickers)])


def store(con: duckdb.DuckDBPyConnection, ticker: str, bars: list[Bar]) -> None:
    con.executemany(
        "INSERT INTO prices_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(ticker, b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars],
    )


def business_days(start: dt.date, count: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


# --- 対象の決め方 -----------------------------------------------------------


def test_targets_are_universe_survivors(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    replace_ticker_rows(
        con,
        [TickerRow(1, "keep", "KEEP", "Nasdaq"), TickerRow(2, "drop", "DROP", "OTC")],
    )
    targets = select_targets(con, runtime, today=TODAY, permanent_error_threshold=3)
    assert targets == ["KEEP"]


def test_targets_skip_tickers_already_up_to_date(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA", "BBB"])
    store(con, "AAA", [bar(TODAY - dt.timedelta(days=1), 10.0)])
    targets = select_targets(con, runtime, today=TODAY, permanent_error_threshold=3)
    assert targets == ["BBB"]


def test_targets_include_stale_tickers(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    store(con, "AAA", [bar(TODAY - dt.timedelta(days=60), 10.0)])
    assert select_targets(con, runtime, today=TODAY, permanent_error_threshold=3) == ["AAA"]


def test_targets_skip_permanently_failed_tickers(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """上場廃止銘柄を毎週リトライし続けない。"""
    seed(con, ["AAA", "BBB"])
    con.execute(
        "INSERT INTO fetch_failures (ticker, error_class, attempt_count, last_attempt) "
        "VALUES ('AAA', 'invalid_symbol', 3, now())"
    )
    assert select_targets(con, runtime, today=TODAY, permanent_error_threshold=3) == ["BBB"]


def test_targets_keep_temporarily_failed_tickers(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """429 は一時的。次の run でリトライする。"""
    seed(con, ["AAA"])
    con.execute(
        "INSERT INTO fetch_failures (ticker, error_class, attempt_count, last_attempt) "
        "VALUES ('AAA', 'rate_limited', 9, now())"
    )
    assert select_targets(con, runtime, today=TODAY, permanent_error_threshold=3) == ["AAA"]


def test_limit_takes_the_first_n(con: duckdb.DuckDBPyConnection, runtime: PricesRuntime) -> None:
    """全銘柄の実行はデプロイ後。それまでは外から絞る。"""
    seed(con, ["CCC", "AAA", "BBB"])
    targets = select_targets(con, runtime, today=TODAY, limit=2, permanent_error_threshold=3)
    assert targets == ["AAA", "BBB"]


def test_tickers_option_selects_named_symbols(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA", "BBB", "CCC"])
    targets = select_targets(
        con, runtime, today=TODAY, tickers=["ccc", " AAA "], permanent_error_threshold=3
    )
    assert targets == ["AAA", "CCC"]


# --- 開始日 -----------------------------------------------------------------


def test_start_date_is_a_full_window_without_history(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    start, full = start_date_for(con, "AAA", runtime, today=TODAY)
    assert full is True
    assert start == TODAY - dt.timedelta(days=runtime.window_days)


def test_start_date_overlaps_existing_history(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """重複期間を取るのは遡及調整を検知するため。1銘柄1リクエストなのでコストは変わらない。"""
    seed(con, ["AAA"])
    days = business_days(D(2026, 8, 3), 20)
    store(con, "AAA", [bar(d, 10.0) for d in days])
    start, full = start_date_for(con, "AAA", runtime, today=TODAY)
    assert full is False
    assert start == days[-1 - runtime.overlap_days]


def test_short_history_falls_back_to_a_full_fetch(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    store(con, "AAA", [bar(d, 10.0) for d in business_days(D(2026, 9, 14), 2)])
    _, full = start_date_for(con, "AAA", runtime, today=TODAY)
    assert full is True


# --- 遡及調整の検知 ---------------------------------------------------------


def test_matching_closes_do_not_trigger_a_refetch() -> None:
    existing = {D(2026, 9, 14): 100.0, D(2026, 9, 15): 101.0}
    assert needs_full_refetch(existing, [bar(D(2026, 9, 14), 100.0)]) is False


def test_rounding_noise_does_not_trigger_a_refetch() -> None:
    existing = {D(2026, 9, 14): 100.0}
    assert needs_full_refetch(existing, [bar(D(2026, 9, 14), 100.00001)]) is False


def test_a_split_triggers_a_refetch() -> None:
    """2分割なら過去の終値が半分に書き換わる。"""
    existing = {D(2026, 9, 14): 100.0}
    assert needs_full_refetch(existing, [bar(D(2026, 9, 14), 50.0)]) is True


def test_unknown_dates_are_ignored() -> None:
    assert needs_full_refetch({}, [bar(D(2026, 9, 14), 50.0)]) is False


# --- 取得ループ -------------------------------------------------------------


def run(
    con: duckdb.DuckDBPyConnection, source: FakeSource, runtime: PricesRuntime, **kwargs: object
):
    slept: list[float] = []
    return (
        fetch_prices(
            con,
            source,
            runtime,
            today=TODAY,
            sleeper=slept.append,
            jitter=lambda: 0.0,
            **kwargs,  # type: ignore[arg-type]
        ),
        slept,
    )


def test_full_fetch_writes_bars(con: duckdb.DuckDBPyConnection, runtime: PricesRuntime) -> None:
    seed(con, ["AAA"])
    days = business_days(D(2026, 9, 14), 5)
    source = FakeSource({"AAA": [bar(d, 10.0 + i) for i, d in enumerate(days)]})
    report, _ = run(con, source, runtime)

    assert report.targets == 1
    assert report.succeeded == 1
    assert report.bars_written == 5
    assert report.coverage == 1.0
    assert con.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == 5


def test_incremental_fetch_appends_without_duplicates(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    days = business_days(D(2026, 8, 3), 25)
    store(con, "AAA", [bar(d, 10.0) for d in days])
    later = business_days(D(2026, 9, 7), 10)
    source = FakeSource({"AAA": [bar(d, 10.0) for d in days + later]})

    report, _ = run(con, source, runtime)

    assert report.succeeded == 1
    stored = con.execute("SELECT count(*), count(DISTINCT date) FROM prices_daily").fetchone()
    assert stored[0] == stored[1], "同じ日付が二重に入っている"


def test_split_adjustment_replaces_the_whole_history(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """調整前の行が残ると、存在しない暴落として52週高値に記録される。"""
    seed(con, ["AAA"])
    days = business_days(D(2026, 8, 3), 25)
    store(con, "AAA", [bar(d, 100.0) for d in days])
    # 2分割後。全期間が半値で返ってくる。
    source = FakeSource({"AAA": [bar(d, 50.0) for d in days]})

    report, _ = run(con, source, runtime)

    assert report.split_refetches == 1
    assert report.initial_fetches == 0
    highs = con.execute("SELECT max(close) FROM prices_daily").fetchone()[0]
    assert highs == 50.0, "調整前の100.0 が残っている"


def test_a_failure_does_not_stop_the_run(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA", "BBB"])
    source = FakeSource(
        bars={"BBB": [bar(D(2026, 9, 18), 10.0)]},
        errors={"AAA": PriceFetchError("AAA", ErrorClass.RATE_LIMITED, "429")},
    )
    report, _ = run(con, source, runtime)

    assert report.failed == 1
    assert report.succeeded == 1
    assert report.coverage == pytest.approx(0.5)
    assert con.execute(
        "SELECT error_class FROM fetch_failures WHERE ticker = 'AAA'"
    ).fetchone() == ("rate_limited",)


def test_repeated_same_failures_accumulate(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    source = FakeSource(
        errors={"AAA": PriceFetchError("AAA", ErrorClass.INVALID_SYMBOL, "no data found")}
    )
    run(con, source, runtime)
    run(con, source, runtime)
    assert con.execute(
        "SELECT attempt_count FROM fetch_failures WHERE ticker = 'AAA'"
    ).fetchone() == (2,)


def test_a_success_clears_a_previous_failure(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA"])
    con.execute(
        "INSERT INTO fetch_failures (ticker, error_class, attempt_count, last_attempt) "
        "VALUES ('AAA', 'rate_limited', 2, now())"
    )
    run(con, FakeSource({"AAA": [bar(D(2026, 9, 18), 10.0)]}), runtime)
    assert con.execute("SELECT count(*) FROM fetch_failures").fetchone()[0] == 0


def test_time_budget_stops_the_run(con: duckdb.DuckDBPyConnection, runtime: PricesRuntime) -> None:
    """超過したら打ち切って、その時点のデータで以降を走らせる。"""
    seed(con, ["AAA", "BBB", "CCC"])
    source = FakeSource({t: [bar(D(2026, 9, 18), 10.0)] for t in ("AAA", "BBB", "CCC")})
    ticks = iter([0.0, 1.0, 999_999.0, 999_999.0])
    report = fetch_prices(
        con,
        source,
        runtime,
        today=TODAY,
        sleeper=lambda _: None,
        jitter=lambda: 0.0,
        monotonic=lambda: next(ticks),
    )
    assert report.stopped_on_budget is True
    assert report.succeeded < report.targets


def test_no_targets_returns_an_empty_report(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    report, _ = run(con, FakeSource(), runtime)
    assert report.targets == 0
    assert report.coverage is None


def test_the_loop_waits_between_requests(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    seed(con, ["AAA", "BBB"])
    source = FakeSource({t: [bar(D(2026, 9, 18), 10.0)] for t in ("AAA", "BBB")})
    _, slept = run(con, source, runtime)
    # 1件目の前は待たない。2件目の前に1回だけ待つ。
    assert slept == [runtime.throttle.base_interval_sec]


def test_resuming_skips_already_fetched_tickers(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """途中で中断して再実行すると、続きから走ること。"""
    seed(con, ["AAA", "BBB"])
    source = FakeSource({t: [bar(D(2026, 9, 18), 10.0)] for t in ("AAA", "BBB")})
    run(con, source, runtime, limit=1)
    assert [c[0] for c in source.calls] == ["AAA"]

    source.calls.clear()
    run(con, source, runtime)
    assert [c[0] for c in source.calls] == ["BBB"]


def test_initial_fetch_is_not_reported_as_a_split(
    con: duckdb.DuckDBPyConnection, runtime: PricesRuntime
) -> None:
    """履歴が無いだけのフル取得を「遡及調整」と数えない。"""
    seed(con, ["AAA"])
    source = FakeSource({"AAA": [bar(d, 10.0) for d in business_days(D(2026, 9, 14), 5)]})
    report, _ = run(con, source, runtime)
    assert report.initial_fetches == 1
    assert report.split_refetches == 0
