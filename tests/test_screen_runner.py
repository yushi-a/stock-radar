"""DuckDB 上でのスクリーニング。合成ユニバースを入れて結果を固定する。

判定そのもののテストは `test_screen_logic.py`。ここで見るのは**繋ぎ込み**。

- `universe` / `fundamentals` / `facts_quarterly` / `market_metrics` を1行に束ねられるか
- `screen_runs` / `screen_results` に何が残るか
- 株価が要らない条件だけを当てるモード（株価取得の前の足切り）
"""

from __future__ import annotations

import datetime as dt
import json

import duckdb
import pytest

from stock_radar.config import Criteria, Market, load_criteria
from stock_radar.metrics.market import MarketMetrics, store_market_metrics
from stock_radar.screen.evaluate import Track
from stock_radar.screen.runner import prescreen_tickers, screen, store_run
from stock_radar.sources.sec.normalize import Fundamentals, store_fundamentals
from stock_radar.sources.sec.universe import TickerRow, replace_ticker_rows
from tests.test_screen_logic import CRITERIA_PATH

AS_OF = dt.date(2026, 9, 21)
YEARS = (2022, 2023, 2024, 2025)


@pytest.fixture(scope="module")
def criteria() -> Criteria:
    return load_criteria(CRITERIA_PATH)


def year_end(year: int) -> dt.date:
    return dt.date(year, 12, 31)


def history(
    cik: int,
    *,
    revenue: float,
    growth: float,
    equity: float = 500_000_000.0,
    operating_income: float | None = None,
    gross_profit: float | None = None,
    cfo: float | None = 80_000_000.0,
    capex: float | None = 20_000_000.0,
    net_income: float | None = 50_000_000.0,
    total_assets: float = 625_000_000.0,
) -> list[Fundamentals]:
    """4年分の年次データ。``growth`` は毎年の売上成長率。

    直近年度が ``revenue`` になるように過去へ遡って割り戻す。
    """
    rows = []
    for offset, year in enumerate(reversed(YEARS)):
        scale = (1 + growth) ** -offset
        rows.append(
            Fundamentals(
                cik=cik,
                fiscal_year=year,
                period_start=dt.date(year, 1, 1),
                period_end=year_end(year),
                period_days=365,
                revenue=revenue * scale,
                gross_profit=None if gross_profit is None else gross_profit * scale,
                operating_income=(None if operating_income is None else operating_income * scale),
                net_income=None if net_income is None else net_income * scale,
                total_assets=total_assets * scale,
                equity=equity * scale,
                current_assets=200_000_000.0 * scale,
                current_liabilities=100_000_000.0 * scale,
                cfo=None if cfo is None else cfo * scale,
                capex=None if capex is None else capex * scale,
                shares_outstanding=10_000_000.0,
                currency="USD",
                accn=f"0000000000-{year}-000001",
                filed_at=dt.date(year + 1, 2, 15),
            )
        )
    return rows


def quarters(con: duckdb.DuckDBPyConnection, cik: int, *, latest: float, growth: float) -> None:
    """直近四半期とその前年同期。トラックB の YoY に要る。"""
    for period_end, value in (
        (dt.date(2025, 12, 31), latest),
        (dt.date(2024, 12, 31), latest / (1 + growth)),
    ):
        con.execute(
            "INSERT INTO facts_quarterly "
            "(cik, fiscal_year, fiscal_period, period_start, period_end, concept, value, "
            " unit, accn, filed_at) "
            "VALUES (?, ?, 'Q4', ?, ?, 'Revenues', ?, 'USD', 'x', ?)",
            [
                cik,
                period_end.year,
                period_end - dt.timedelta(days=90),
                period_end,
                value,
                period_end,
            ],
        )


def quotes(ticker: str, *, market_cap: float = 1_000_000_000.0) -> MarketMetrics:
    shares = 10_000_000.0
    return MarketMetrics(
        ticker=ticker,
        as_of=AS_OF,
        market_cap=market_cap,
        avg_daily_value=10_000_000.0,
        high_52w=market_cap / shares / 0.7,
        low_52w=market_cap / shares / 1.4,
        range_position_52w=0.40,
        drawdown_from_52w_high=-0.30,
        latest_price_date=AS_OF,
    )


@pytest.fixture
def universe(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """3社の合成ユニバース。

    - ``AAA`` トラックA を通る
    - ``BBB`` トラックB を通る（営業赤字・高成長）
    - ``CCC`` 成長が足りずどちらも通らない
    - ``ZZZ`` ユニバースにはいるが財務データが無い
    """
    replace_ticker_rows(
        con,
        [
            TickerRow(cik=1, name="Alpha Inc", ticker="AAA", exchange="Nasdaq"),
            TickerRow(cik=2, name="Beta Corp", ticker="BBB", exchange="NYSE"),
            TickerRow(cik=3, name="Gamma Ltd", ticker="CCC", exchange="Nasdaq"),
            TickerRow(cik=4, name="Zeta Plc", ticker="ZZZ", exchange="Nasdaq"),
        ],
    )
    store_fundamentals(
        con,
        [
            *history(1, revenue=800_000_000.0, growth=0.15, operating_income=96_000_000.0),
            *history(
                2,
                revenue=200_000_000.0,
                growth=0.35,
                operating_income=-20_000_000.0,
                gross_profit=110_000_000.0,
                net_income=-20_000_000.0,
                cfo=-10_000_000.0,
            ),
            *history(3, revenue=800_000_000.0, growth=0.02, operating_income=96_000_000.0),
        ],
    )
    quarters(con, 1, latest=200_000_000.0, growth=0.05)
    quarters(con, 2, latest=55_000_000.0, growth=0.30)
    quarters(con, 3, latest=200_000_000.0, growth=0.02)
    store_market_metrics(con, [quotes("AAA"), quotes("BBB"), quotes("CCC")])
    return con


# --- 判定 -------------------------------------------------------------------


def test_screen_picks_up_both_tracks(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    report = screen(universe, criteria)
    assert report.universe_size == 4
    assert report.missing_fundamentals == 1  # ZZZ
    assert report.evaluated == 3
    assert [item.candidate.ticker for item in report.passed] == ["AAA", "BBB"]
    assert report.track_counts == {"A": 1, "B": 1}
    assert report.price_coverage == 1.0


def test_blocked_counts_separate_failures_from_undecidable(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    """成長が足りない CCC は「閾値未満」、粗利を開示しない分は「判定不能」。"""
    blocked = screen(universe, criteria).blocked_counts()
    assert blocked["track_a.revenue_cagr_3y"]["fail"] == 1
    # 数えるのは落ちた銘柄だけ。AAA はトラックA を通っているので、粗利が無くても
    # ここには出てこない。CCC の1件だけが残る。
    assert blocked["track_b.gross_margin"]["unknown"] == 1
    assert "fail" not in blocked["track_b.gross_margin"]


def test_price_coverage_counts_missing_quotes(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    universe.execute("DELETE FROM market_metrics WHERE ticker = 'CCC'")
    report = screen(universe, criteria)
    assert report.price_coverage == pytest.approx(2 / 3)


def test_multi_class_shares_one_fundamentals_row(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    """複数クラス株。財務は CIK 単位なので同じ行を共有し、株価だけが別。"""
    universe.execute(
        "INSERT INTO universe (market, ticker, cik, name, excluded_reason, updated_at) "
        "VALUES ('us', 'AAA.B', 1, 'Alpha Inc', NULL, now()::TIMESTAMP)"
    )
    store_market_metrics(
        universe,
        [quotes("AAA"), quotes("BBB"), quotes("CCC"), quotes("AAA.B", market_cap=900_000_000.0)],
    )
    report = screen(universe, criteria)
    passed = {item.candidate.ticker for item in report.passed}
    assert {"AAA", "AAA.B"} <= passed


# --- 株価が要らない条件だけを当てるモード -----------------------------------


def test_prescreen_narrows_targets_before_price_fetch(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    """株価取得は財務の足切りの後（CLAUDE.md）。CCC はここで落ちる。"""
    assert prescreen_tickers(universe, criteria) == ["AAA", "BBB"]


def test_prescreen_works_without_any_prices(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    universe.execute("DELETE FROM market_metrics")
    assert prescreen_tickers(universe, criteria) == ["AAA", "BBB"]
    # 株価を当てる設定なら、株価が無い以上1件も通らない。
    assert screen(universe, criteria).passed == []


def test_without_price_filters_leaves_timing_score_empty(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    universe.execute("DELETE FROM market_metrics")
    report = screen(universe, criteria, with_price=False)
    assert all(item.timing_score is None for item in report.passed)


# --- 記録 -------------------------------------------------------------------


def test_store_run_records_the_criteria_snapshot(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    report = screen(universe, criteria)
    run_id = store_run(universe, report, criteria, csv_path="output/2026-09-21_us.csv")

    run = universe.execute(
        "SELECT market, criteria_snapshot, universe_size, passed_count, price_coverage, csv_path "
        "FROM screen_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert run[0] == Market.US.value
    snapshot = json.loads(run[1])
    # ファイルの生テキストではなく、既定値の補完まで含めた実際の値が入る。
    assert snapshot["track_a"]["op_margin"]["by_market"]["us"]["exclusive"] is True
    assert snapshot["common"]["by_market"]["us"]["market_cap"]["max"] == 8_000_000_000
    assert (run[2], run[3], run[4]) == (4, 2, 1.0)
    assert run[5] == "output/2026-09-21_us.csv"


def test_store_run_records_values_and_sources(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    run_id = store_run(universe, screen(universe, criteria), criteria)
    rows = universe.execute(
        "SELECT ticker, track, passed_filters, timing_score, market_cap, revenue_cagr_3y, "
        "  pbr, gross_margin, fundamentals_fiscal_year, fundamentals_period_end, "
        "  fundamentals_filed_at, price_as_of, data_source "
        "FROM screen_results WHERE run_id = ? ORDER BY ticker",
        [run_id],
    ).fetchall()
    assert [row[0] for row in rows] == ["AAA", "BBB"]

    alpha = rows[0]
    assert alpha[1] == Track.A.value
    assert "track_a" in alpha[2]
    assert alpha[3] == 2.0  # 下落率もレンジ内位置も条件を満たす
    assert alpha[4] == pytest.approx(1_000_000_000.0)
    assert alpha[5] == pytest.approx(0.15)
    assert alpha[6] == pytest.approx(2.0)
    assert alpha[7] is None  # 粗利を開示していない
    # 出典と基準日（CLAUDE.md「数値の出典を記録する」）
    assert (alpha[8], alpha[9]) == (2025, dt.date(2025, 12, 31))
    assert (alpha[10], alpha[11]) == (dt.date(2026, 2, 15), AS_OF)
    assert alpha[12] == "sec_companyfacts+yfinance"


def test_store_run_keeps_history_across_runs(
    universe: duckdb.DuckDBPyConnection, criteria: Criteria
) -> None:
    """閾値調整の試行錯誤が記録として残る（docs/architecture.md）。"""
    first = store_run(universe, screen(universe, criteria), criteria)
    second = store_run(universe, screen(universe, criteria), criteria)
    assert first != second
    assert universe.execute("SELECT count(*) FROM screen_runs").fetchone()[0] == 2
    assert universe.execute("SELECT count(*) FROM screen_results").fetchone()[0] == 4
