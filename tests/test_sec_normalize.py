"""年次レコードの正規化。**ここが最優先のゴールデンテスト**（docs/testing.md）。

XBRL 正規化は静かに間違う。落ちるバグなら気づくが、決算期変更で6ヶ月しかない
「年度」を12ヶ月として扱って売上CAGRが1.5倍になっても例外は1つも出ない。

期待値は実物の 10-K と手で突き合わせて確定したもの。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest

from stock_radar.sources.sec.companyfacts import parse_company_facts
from stock_radar.sources.sec.normalize import (
    FOLD_MIN_GAP_DAYS,
    Fundamentals,
    Observation,
    fiscal_year_ends,
    normalize_company,
    normalize_universe,
    pick,
    store_fundamentals,
)
from stock_radar.sources.sec.universe import TickerRow, replace_ticker_rows

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec" / "companyfacts"
CIKS = {"aapl": 320193, "googl": 1652044, "deere": 315189, "dal": 27904, "shph": 1757499}

D = dt.date


def rows_for(slug: str) -> list[Fundamentals]:
    annual, _ = parse_company_facts(json.loads((FIXTURES / f"{slug}.json").read_text()))
    observations = [
        Observation(f.concept, f.unit, f.period_start, f.period_end, f.value, f.accn, f.filed_at)
        for f in annual
    ]
    return normalize_company(CIKS[slug], observations)


def year(slug: str, fiscal_year: int) -> Fundamentals:
    return next(r for r in rows_for(slug) if r.fiscal_year == fiscal_year)


def obs(
    concept: str,
    end: dt.date,
    value: float,
    *,
    start: dt.date | None = None,
    unit: str = "USD",
    accn: str = "a",
    filed: dt.date | None = None,
) -> Observation:
    return Observation(concept, unit, start, end, value, accn, filed)


# --- 会計年度の束ね（A-6） ---------------------------------------------------


def test_fiscal_year_ends_are_newest_first() -> None:
    ends = [D(2022, 12, 31), D(2024, 12, 31), D(2023, 12, 31)]
    assert fiscal_year_ends(ends) == [D(2024, 12, 31), D(2023, 12, 31), D(2022, 12, 31)]


def test_period_ends_a_day_apart_are_one_fiscal_year() -> None:
    """DEERE で実在する。束ねないと1年度が2行になり 3年CAGR が1年ずれる。"""
    assert fiscal_year_ends([D(2015, 10, 31), D(2015, 11, 1)]) == [D(2015, 11, 1)]


def test_a_real_year_apart_is_not_folded() -> None:
    ends = [D(2024, 9, 28), D(2023, 9, 30)]
    assert fiscal_year_ends(ends) == ends


def test_fold_threshold_is_below_a_short_year() -> None:
    """340日未満を同一年度とみなす。52週決算（364日）は別年度に残る。"""
    assert FOLD_MIN_GAP_DAYS < 364


# --- どの候補を採るか -------------------------------------------------------


def test_pick_prefers_the_higher_priority_tag() -> None:
    anchor = D(2024, 12, 31)
    chosen = pick(
        [
            obs("Revenues", anchor, 100.0, start=D(2024, 1, 1)),
            obs(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                anchor,
                90.0,
                start=D(2024, 1, 1),
            ),
        ],
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
        anchor,
    )
    assert chosen is not None
    assert chosen.value == 90.0


def test_pick_prefers_the_period_closest_to_a_year() -> None:
    """A-9：同じ期末に 356日と 365日が併記され、値が4倍違う例がある。"""
    anchor = D(2011, 12, 31)
    chosen = pick(
        [
            obs("Revenues", anchor, 246_911_000.0, start=D(2011, 1, 10)),
            obs("Revenues", anchor, 1_021_200_000.0, start=D(2011, 1, 1)),
        ],
        ["Revenues"],
        anchor,
    )
    assert chosen is not None
    assert chosen.value == 1_021_200_000.0


def test_pick_prefers_the_latest_filing() -> None:
    """A-3：修正再提出の最新値を採る。"""
    anchor = D(2009, 9, 26)
    chosen = pick(
        [
            obs("Revenues", anchor, 36_537.0, start=D(2008, 9, 28), filed=D(2009, 10, 27)),
            obs("Revenues", anchor, 42_905.0, start=D(2008, 9, 28), filed=D(2010, 10, 27)),
        ],
        ["Revenues"],
        anchor,
    )
    assert chosen is not None
    assert chosen.value == 42_905.0


def test_pick_prefers_usd() -> None:
    """年次売上を USD で一度も報告していない企業が4社ある。あるなら USD を採る。"""
    anchor = D(2024, 12, 31)
    chosen = pick(
        [
            obs("Revenues", anchor, 130.0, start=D(2024, 1, 1), unit="CAD"),
            obs("Revenues", anchor, 100.0, start=D(2024, 1, 1), unit="USD"),
        ],
        ["Revenues"],
        anchor,
    )
    assert chosen is not None
    assert chosen.unit == "USD"


def test_pick_falls_back_to_another_currency() -> None:
    anchor = D(2024, 12, 31)
    chosen = pick(
        [obs("Revenues", anchor, 130.0, start=D(2024, 1, 1), unit="CAD")], ["Revenues"], anchor
    )
    assert chosen is not None
    assert chosen.unit == "CAD"


def test_pick_ignores_other_fiscal_years() -> None:
    chosen = pick(
        [obs("Revenues", D(2023, 12, 31), 100.0, start=D(2023, 1, 1))],
        ["Revenues"],
        D(2024, 12, 31),
    )
    assert chosen is None


def test_pick_ignores_null_values() -> None:
    anchor = D(2024, 12, 31)
    observations = [Observation("Revenues", "USD", D(2024, 1, 1), anchor, None, "a", None)]
    assert pick(observations, ["Revenues"], anchor) is None


# --- ゴールデン：実物の 10-K と突き合わせた値 -------------------------------


def test_apple_fy2024_matches_the_filing() -> None:
    """Apple FY2024（2023-10-01〜2024-09-28、52週で364日）。"""
    row = year("aapl", 2024)
    assert (row.period_start, row.period_end, row.period_days) == (
        D(2023, 10, 1),
        D(2024, 9, 28),
        364,
    )
    assert row.revenue == 391_035_000_000
    assert row.gross_profit == 180_683_000_000
    assert row.operating_income == 123_216_000_000
    assert row.net_income == 93_736_000_000
    assert row.total_assets == 364_980_000_000
    assert row.equity == 56_950_000_000
    assert row.cfo == 118_254_000_000
    assert row.capex == 9_447_000_000
    assert row.currency == "USD"
    assert row.source_concepts["revenue"] == ("RevenueFromContractWithCustomerExcludingAssessedTax")


def test_apple_cover_page_share_date_does_not_become_the_fiscal_year_end() -> None:
    """`dei:EntityCommonStockSharesOutstanding` は表紙の日付で報告される。

    これを会計年度の基準日にすると決算期末が窓から外れ、**売上が丸ごと落ちる**。
    実装中に実際に踏んだ。
    """
    row = year("aapl", 2024)
    assert row.period_end == D(2024, 9, 28)
    assert row.revenue is not None


def test_alphabet_fy2024_matches_the_filing() -> None:
    row = year("googl", 2024)
    assert row.revenue == 350_018_000_000
    assert row.operating_income == 112_390_000_000
    assert row.net_income == 100_118_000_000
    assert row.total_assets == 450_256_000_000
    assert row.equity == 325_084_000_000


def test_multi_class_issuer_gets_total_shares() -> None:
    """C：`dei` のタグが無い企業でも全クラス合計の株数が取れること。"""
    row = year("googl", 2024)
    assert row.shares_outstanding == 12_447_000_000
    assert row.source_concepts["shares_outstanding"] == (
        "WeightedAverageNumberOfDilutedSharesOutstanding"
    )


def test_gross_profit_is_derived_when_the_tag_is_missing() -> None:
    """粗利は 売上 − 原価 で導出する。出典にもその旨を残す。"""
    row = year("googl", 2024)
    assert row.gross_profit == 203_712_000_000
    assert row.source_concepts["gross_profit"] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax-CostOfRevenue"
    )


def test_deere_folds_one_day_apart_year_ends() -> None:
    """A-6：20年分あって重複年が無いこと。"""
    rows = rows_for("deere")
    years = [r.fiscal_year for r in rows]
    assert len(years) == len(set(years))
    assert rows[0].fiscal_year > rows[-1].fiscal_year


def test_deere_53_week_year_is_kept() -> None:
    """53週決算（371日）は 350〜380日ガードの内側。"""
    row = year("deere", 2025)
    assert row.period_days == 371
    assert row.revenue == 45_684_000_000


def test_deere_derives_gross_profit_for_older_years_only() -> None:
    """Deere は2017年までしか原価タグを付けていない。"""
    old = year("deere", 2017)
    assert old.gross_profit == 9_804_500_000
    assert old.source_concepts["gross_profit"] == "Revenues-CostOfGoodsSold"
    assert year("deere", 2024).gross_profit is None


def test_airline_has_no_gross_profit_but_keeps_everything_else() -> None:
    """D：粗利が算出できない 28.8% の代表例。**0 ではなく None**。"""
    row = year("dal", 2024)
    assert row.gross_profit is None
    assert "gross_profit" not in row.source_concepts
    assert row.revenue == 61_643_000_000
    assert row.operating_income == 5_995_000_000


def test_pre_revenue_company_does_not_crash() -> None:
    rows = rows_for("shph")
    assert rows
    assert all(r.revenue is None for r in rows)


def test_normalize_company_handles_no_observations() -> None:
    assert normalize_company(1, []) == []


def test_share_only_observations_produce_no_rows() -> None:
    """株数しか無い企業に会計年度は作れない。"""
    rows = normalize_company(
        1, [obs("EntityCommonStockSharesOutstanding", D(2024, 10, 18), 100.0, unit="shares")]
    )
    assert rows == []


# --- DuckDB との受け渡し ----------------------------------------------------


def test_store_and_reload(con: duckdb.DuckDBPyConnection) -> None:
    rows = rows_for("aapl")
    assert store_fundamentals(con, rows) == len(rows)

    stored = con.execute(
        "SELECT revenue, gross_profit, currency, source_concepts FROM fundamentals "
        "WHERE fiscal_year = 2024"
    ).fetchone()
    assert stored[0] == 391_035_000_000
    assert stored[1] == 180_683_000_000
    assert stored[2] == "USD"
    assert json.loads(stored[3])["revenue"] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_store_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    rows = rows_for("aapl")
    store_fundamentals(con, rows)
    store_fundamentals(con, rows)
    assert con.execute("SELECT count(*) FROM fundamentals").fetchone()[0] == len(rows)


def test_missing_values_stay_null(con: duckdb.DuckDBPyConnection) -> None:
    """「開示されていない」を 0 にしない。"""
    store_fundamentals(con, rows_for("dal"))
    nulls = con.execute("SELECT count(*) FROM fundamentals WHERE gross_profit IS NULL").fetchone()[
        0
    ]
    assert nulls > 0
    assert (
        con.execute("SELECT count(*) FROM fundamentals WHERE gross_profit = 0").fetchone()[0] == 0
    )


def test_normalize_universe_reads_facts_annual(con: duckdb.DuckDBPyConnection) -> None:
    replace_ticker_rows(con, [TickerRow(CIKS["aapl"], "Apple", "AAPL", "Nasdaq")])
    annual, _ = parse_company_facts(json.loads((FIXTURES / "aapl.json").read_text()))
    con.executemany(
        "INSERT INTO facts_annual "
        "(cik, fiscal_year, period_start, period_end, concept, value, unit, accn, filed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f.cik,
                f.fiscal_year,
                f.period_start,
                f.period_end,
                f.concept,
                f.value,
                f.unit,
                f.accn,
                f.filed_at,
            )
            for f in annual
        ],
    )

    produced = normalize_universe(con)

    assert produced > 0
    assert (
        con.execute("SELECT revenue FROM fundamentals WHERE fiscal_year = 2024").fetchone()[0]
        == 391_035_000_000
    )


# --- 会計年度のラベル -------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "expected"),
    [
        # 米国の慣行どおり「終わる暦年」を使う。
        (D(2024, 9, 28), 2024),
        (D(2024, 12, 31), 2024),
        # Walmart（1月末決算）は会社自身も FY2024 と呼ぶ。
        (D(2024, 1, 31), 2024),
        # 12/31 から 1/1 に1日ずれただけで翌年度にしない。
        # BK Technologies は 2020-01-01 と 2020-12-31 の両方を持つ。
        (D(2020, 1, 1), 2019),
        (D(2023, 1, 7), 2022),
        (D(2023, 1, 8), 2023),
    ],
)
def test_fiscal_year_of(period_end: dt.date, expected: int) -> None:
    from stock_radar.sources.sec.normalize import fiscal_year_of

    assert fiscal_year_of(period_end) == expected


def test_two_year_ends_in_one_calendar_year_are_separate_rows(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """決算期がずれると同じ暦年に2つの年度が並ぶ。主キーは期末で取る。"""
    observations = [
        obs("Revenues", D(2020, 1, 1), 40_100_000.0, start=D(2019, 1, 1)),
        obs("Revenues", D(2020, 12, 31), 44_139_000.0, start=D(2020, 1, 1)),
    ]
    rows = normalize_company(2186, observations)
    assert [r.fiscal_year for r in rows] == [2020, 2019]
    assert store_fundamentals(con, rows) == 2
    assert con.execute("SELECT count(*) FROM fundamentals").fetchone()[0] == 2
