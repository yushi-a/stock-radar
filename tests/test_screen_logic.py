"""スクリーニングの判定ロジック。

**実際の `config/criteria.yaml` を読んで当てる。** テスト用の閾値を別に組むと、
「設定を変えたのに候補が変わらない」たぐいの事故を検知できない。閾値は何度も
調整する前提なので、触ったときに意図しない副作用が出ていないかをここで見る
（docs/testing.md「合成ユニバースによる条件の回帰テスト」）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stock_radar.config import Criteria, Market, load_criteria
from stock_radar.metrics.fundamentals import FundamentalMetrics
from stock_radar.metrics.market import MarketMetrics
from stock_radar.screen import timing
from stock_radar.screen.evaluate import Track, evaluate, undecidable_count
from stock_radar.screen.filters import Candidate, Verdict
from stock_radar.sources.sec.normalize import Fundamentals

CRITERIA_PATH = Path(__file__).resolve().parents[1] / "config" / "criteria.yaml"
PERIOD_END = dt.date(2025, 12, 31)
AS_OF = dt.date(2026, 9, 21)

# トラックA だけを通る銘柄。ここを起点に1項目ずつ壊して境界を確かめる。
# トラックB は売上成長（5%）と粗利率（30%）で落ちるようにしてある。
BASELINE: dict[str, Any] = {
    "revenue_cagr_3y": 0.15,
    "revenue_growth_yoy": 0.05,
    "revenue_growth_latest_quarter_yoy": 0.05,
    "op_margin": 0.12,
    "gross_margin": 0.30,
    "fcf": 60_000_000.0,
    "roa": 0.08,
    "roe": 0.12,
    "equity_ratio": 0.60,
    "current_ratio": 2.0,
    "asset_growth_minus_ebit_growth": -0.05,
    "ebit_turned_positive": False,
}

# トラックB だけを通る銘柄への差分。営業赤字なのでトラックA は通らない。
TRACK_B: dict[str, Any] = {
    "revenue_cagr_3y": None,
    "revenue_growth_yoy": 0.35,
    "revenue_growth_latest_quarter_yoy": 0.30,
    "op_margin": -0.10,
    "gross_margin": 0.55,
}


@pytest.fixture(scope="module")
def criteria() -> Criteria:
    return load_criteria(CRITERIA_PATH)


def make(
    ticker: str = "AAA",
    *,
    market: Market = Market.US,
    equity: float | None = 500_000_000.0,
    revenue: float | None = 800_000_000.0,
    market_cap: float | None = 1_000_000_000.0,
    avg_daily_value: float | None = 10_000_000.0,
    drawdown: float | None = -0.30,
    range_position: float | None = 0.40,
    no_quotes: bool = False,
    **overrides: Any,
) -> Candidate:
    """判定材料を1件組み立てる。既定はトラックA を通る値。"""
    values = BASELINE | overrides
    quotes = (
        None
        if no_quotes
        else MarketMetrics(
            ticker=ticker,
            as_of=AS_OF,
            market_cap=market_cap,
            avg_daily_value=avg_daily_value,
            high_52w=100.0,
            low_52w=50.0,
            range_position_52w=range_position,
            drawdown_from_52w_high=drawdown,
            latest_price_date=AS_OF,
        )
    )
    return Candidate(
        market=market,
        ticker=ticker,
        cik=1,
        name=ticker,
        fundamentals=Fundamentals(
            cik=1,
            fiscal_year=2025,
            period_start=dt.date(2025, 1, 1),
            period_end=PERIOD_END,
            period_days=365,
            revenue=revenue,
            equity=equity,
        ),
        metrics=FundamentalMetrics(cik=1, fiscal_year=2025, period_end=PERIOD_END, **values),
        quotes=quotes,
    )


def verdict_of(evaluation: Any, name: str) -> Verdict:
    for item in evaluation.checks:
        if item.name == name:
            return item.verdict
    raise AssertionError(f"条件 {name} が判定されていない")


# --- トラックの振り分け -----------------------------------------------------


def test_track_a_passes(criteria: Criteria) -> None:
    result = evaluate(make(), criteria)
    assert result.tracks == (Track.A,)
    assert result.passed
    assert "track_a" in result.passed_filters
    assert "track_a.pbr" in result.passed_filters
    assert result.blocking == ()


def test_track_b_passes(criteria: Criteria) -> None:
    result = evaluate(make(revenue=200_000_000.0, **TRACK_B), criteria)
    assert result.tracks == (Track.B,)
    assert result.track is Track.B


def test_both_tracks_keep_track_a_as_representative(criteria: Criteria) -> None:
    """両方通ったら A を代表にし、B を通ったことは `passed_filters` に残す。"""
    result = evaluate(
        make(
            revenue=200_000_000.0,
            revenue_growth_yoy=0.35,
            revenue_growth_latest_quarter_yoy=0.30,
            gross_margin=0.55,
        ),
        criteria,
    )
    assert result.tracks == (Track.A, Track.B)
    assert result.track is Track.A
    assert result.passed_filters[:2] == ("track_a", "track_b")


# --- 共通足切り -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"equity": -1.0}, "common.equity_positive"),
        ({"revenue": 0.0}, "common.revenue_positive"),
        ({"market_cap": 9_000_000_000.0}, "common.market_cap"),
        ({"market_cap": 100_000_000.0}, "common.market_cap"),
        ({"avg_daily_value": 1_000_000.0}, "common.avg_daily_value"),
    ],
)
def test_common_filter_blocks(criteria: Criteria, kwargs: dict[str, Any], name: str) -> None:
    result = evaluate(make(**kwargs), criteria)
    assert not result.passed
    assert [item.name for item in result.blocking] == [name]


def test_negative_equity_does_not_break_pbr(criteria: Criteria) -> None:
    """自己資本マイナスでも計算過程で落ちない（docs/testing.md の境界値）。"""
    candidate = make(equity=-100_000_000.0)
    assert candidate.pbr == pytest.approx(-10.0)
    assert not evaluate(candidate, criteria).passed


# --- 閾値の境界 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cagr_value", "expected"),
    [(0.10, Verdict.PASS), (0.0999, Verdict.FAIL), (None, Verdict.UNKNOWN)],
)
def test_revenue_cagr_boundary(
    criteria: Criteria, cagr_value: float | None, expected: Verdict
) -> None:
    """`min` は境界を含む（10.00% ちょうどは通る）。"""
    result = evaluate(make(revenue_cagr_3y=cagr_value), criteria)
    assert verdict_of(result, "track_a.revenue_cagr_3y") is expected


@pytest.mark.parametrize(
    ("margin", "expected"),
    [(0.0001, Verdict.PASS), (0.0, Verdict.FAIL), (-0.01, Verdict.FAIL)],
)
def test_us_op_margin_is_exclusive(criteria: Criteria, margin: float, expected: Verdict) -> None:
    """米国トラックA の営業利益率は「黒字であること」。0% ちょうどは通さない。"""
    result = evaluate(make(op_margin=margin), criteria)
    assert verdict_of(result, "track_a.op_margin") is expected


@pytest.mark.parametrize(
    ("psr_revenue", "expected"),
    [(125_000_000.0, Verdict.PASS), (120_000_000.0, Verdict.FAIL)],
)
def test_psr_boundary(criteria: Criteria, psr_revenue: float, expected: Verdict) -> None:
    """PSR ≤ 8。時価総額 10億ドルなので売上1.25億ドルがちょうど境界。"""
    result = evaluate(make(revenue=psr_revenue, **TRACK_B), criteria)
    assert verdict_of(result, "track_b.psr") is expected


# --- どちらか一方でよい条件 -------------------------------------------------


@pytest.mark.parametrize(
    ("roa", "roe", "expected"),
    [
        (0.08, 0.12, Verdict.PASS),
        (None, 0.12, Verdict.PASS),  # 片方が判定不能でも、もう片方が満たせば通す
        (0.08, None, Verdict.PASS),
        (0.01, 0.05, Verdict.FAIL),
        (0.01, None, Verdict.UNKNOWN),  # 満たさず、かつ判定不能が混じる
        (None, None, Verdict.UNKNOWN),
    ],
)
def test_profitability_any(
    criteria: Criteria, roa: float | None, roe: float | None, expected: Verdict
) -> None:
    result = evaluate(make(roa=roa, roe=roe), criteria)
    assert verdict_of(result, "track_a.profitability_any") is expected


@pytest.mark.parametrize(
    ("equity_ratio", "current_ratio", "expected"),
    [
        (0.60, 1.0, Verdict.PASS),
        (0.20, 2.0, Verdict.PASS),
        (0.20, 1.0, Verdict.FAIL),
        (0.20, None, Verdict.UNKNOWN),
    ],
)
def test_financial_buffer_any(
    criteria: Criteria,
    equity_ratio: float | None,
    current_ratio: float | None,
    expected: Verdict,
) -> None:
    result = evaluate(
        make(equity_ratio=equity_ratio, current_ratio=current_ratio, **TRACK_B), criteria
    )
    assert verdict_of(result, "track_b.financial_buffer_any") is expected


# --- EBIT の黒字転換（2026-09-21 ユーザー決定） -----------------------------


def test_ebit_spread_within_threshold(criteria: Criteria) -> None:
    result = evaluate(make(asset_growth_minus_ebit_growth=0.0), criteria)
    assert verdict_of(result, "track_a.asset_growth_minus_ebit_growth") is Verdict.PASS


def test_ebit_spread_over_threshold(criteria: Criteria) -> None:
    result = evaluate(make(asset_growth_minus_ebit_growth=0.10), criteria)
    assert verdict_of(result, "track_a.asset_growth_minus_ebit_growth") is Verdict.FAIL


def test_ebit_turned_positive_passes(criteria: Criteria) -> None:
    """前期赤字で成長率が定義できなくても、黒字転換していれば通す。"""
    result = evaluate(
        make(asset_growth_minus_ebit_growth=None, ebit_turned_positive=True), criteria
    )
    assert result.tracks == (Track.A,)
    assert "track_a.ebit_turned_positive" in result.passed_filters


def test_ebit_undecidable_without_turnaround(criteria: Criteria) -> None:
    """黒字転換でもなければ判定不能。落ちるが、閾値未満とは区別して残る。"""
    result = evaluate(
        make(asset_growth_minus_ebit_growth=None, ebit_turned_positive=False), criteria
    )
    assert verdict_of(result, "track_a.asset_growth_minus_ebit_growth") is Verdict.UNKNOWN
    assert not result.passed


# --- 判定不能と閾値未満の区別 -----------------------------------------------


def test_missing_gross_profit_is_undecidable(criteria: Criteria) -> None:
    """粗利を開示しない企業（実測 28.8%）。閾値未満と混ぜない。"""
    result = evaluate(make(**{**TRACK_B, "gross_margin": None}), criteria)
    assert verdict_of(result, "track_b.gross_margin") is Verdict.UNKNOWN
    assert not result.passed


def test_low_gross_margin_is_a_plain_failure(criteria: Criteria) -> None:
    result = evaluate(make(**{**TRACK_B, "gross_margin": 0.20}), criteria)
    assert verdict_of(result, "track_b.gross_margin") is Verdict.FAIL


def test_all_metrics_missing_does_not_raise(criteria: Criteria) -> None:
    """欠損の伝播。粗利も CAGR も無い銘柄で例外にならないこと。"""
    empty = {key: (False if key == "ebit_turned_positive" else None) for key in BASELINE}
    result = evaluate(make(**empty), criteria)
    assert not result.passed
    # 財務由来の条件はすべて判定不能になる。PBR だけは株価と自己資本から出せるので残る。
    undecidable = {item.name for item in result.checks if item.undecidable}
    assert undecidable == {
        "track_a.revenue_cagr_3y",
        "track_a.op_margin",
        "track_a.profitability_any",
        "track_a.asset_growth_minus_ebit_growth",
        "track_a.fcf_yield",
        "track_b.revenue_growth_yoy",
        "track_b.revenue_growth_latest_quarter_yoy",
        "track_b.op_margin",
        "track_b.gross_margin",
        "track_b.financial_buffer_any",
    }


def test_undecidable_count_groups_by_condition(criteria: Criteria) -> None:
    results = [
        evaluate(make(gross_margin=None), criteria),
        evaluate(make(gross_margin=None), criteria),
        evaluate(make(revenue_cagr_3y=None), criteria),
    ]
    counts = undecidable_count(results)
    assert counts["track_b.gross_margin"] == 2
    assert counts["track_a.revenue_cagr_3y"] == 1


# --- 株価を当てないモード（財務だけの足切り） -------------------------------


def test_without_price_filters_judges_on_fundamentals_only(criteria: Criteria) -> None:
    """株価取得の前段。時価総額も PBR も当てずにトラックA を判定する。"""
    result = evaluate(make(no_quotes=True), criteria, with_price=False)
    assert result.tracks == (Track.A,)
    assert result.timing_score is None
    assert [item.name for item in result.common] == [
        "common.equity_positive",
        "common.revenue_positive",
    ]
    assert not any(item.name == "track_a.pbr" for item in result.checks)


def test_with_price_filters_but_no_prices_is_undecidable(criteria: Criteria) -> None:
    """株価を当てる設定なのに株価が無ければ落ちる（取りこぼしを通過させない）。"""
    result = evaluate(make(no_quotes=True), criteria)
    assert not result.passed
    assert verdict_of(result, "common.market_cap") is Verdict.UNKNOWN


# --- タイミング加点 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("drawdown", "range_position", "expected"),
    [
        (-0.30, 0.40, 2.0),
        (-0.30, 0.80, 1.0),
        (-0.05, 0.40, 1.0),  # まだ高値圏
        (-0.70, 0.80, 0.0),  # 下げすぎ。業績悪化の疑いで加点しない
        (-0.20, 0.50, 2.0),  # 境界は含む
    ],
)
def test_timing_score(
    criteria: Criteria, drawdown: float, range_position: float, expected: float
) -> None:
    result = evaluate(make(drawdown=drawdown, range_position=range_position), criteria)
    assert result.timing_score == expected


def test_timing_score_is_none_without_prices(criteria: Criteria) -> None:
    """株価が無いのを 0点にしない。「条件を満たさなかった」と区別する。"""
    assert evaluate(make(no_quotes=True), criteria).timing_score is None


def test_ranking_orders_by_score_then_closeness_to_low() -> None:
    rows = [
        ("LOW", 1.0, 0.10),
        ("TOP", 2.0, 0.45),
        ("MID", 2.0, 0.20),
        ("NONE", None, None),
    ]
    ordered = [
        ticker
        for ticker, score, position in sorted(
            rows, key=lambda row: timing.ranking_key(row[1], row[2], row[0])
        )
    ]
    assert ordered == ["MID", "TOP", "LOW", "NONE"]


# --- 合成ユニバースでの回帰 -------------------------------------------------


def test_synthetic_universe(criteria: Criteria) -> None:
    """10社の架空データで通過・不通過を固定する（docs/testing.md）。

    閾値を触ったときに、意図した銘柄だけが動くかを見るための土台。
    """
    universe = [
        make("PASSA"),
        make("PASSB", revenue=200_000_000.0, **TRACK_B),
        make("NOCAGR", revenue_cagr_3y=0.05),
        make("LOSS", op_margin=-0.05),
        make("EXPENSIVE", equity=100_000_000.0),  # PBR 10倍
        make("TINY", market_cap=100_000_000.0),
        make("ILLIQUID", avg_daily_value=1_000_000.0),
        make("NOGROSS", **{**TRACK_B, "gross_margin": None}),
        make("TURNAROUND", asset_growth_minus_ebit_growth=None, ebit_turned_positive=True),
        make(
            "EMPTY", **{key: (False if key == "ebit_turned_positive" else None) for key in BASELINE}
        ),
    ]
    results = {candidate.ticker: evaluate(candidate, criteria) for candidate in universe}
    passed = {ticker for ticker, result in results.items() if result.passed}
    assert passed == {"PASSA", "PASSB", "TURNAROUND"}
    assert results["PASSB"].track is Track.B
    assert verdict_of(results["NOGROSS"], "track_b.gross_margin") is Verdict.UNKNOWN
    assert verdict_of(results["NOCAGR"], "track_a.revenue_cagr_3y") is Verdict.FAIL
