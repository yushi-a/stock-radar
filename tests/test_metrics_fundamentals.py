"""財務指標の計算。**境界値が本番**（docs/testing.md）。

「普通のケース」より、分母ゼロ・自己資本マイナス・履歴不足の方が実際に踏む。
"""

from __future__ import annotations

import datetime as dt

import pytest

from stock_radar.metrics.fundamentals import (
    CAGR_MAX_GAP_DAYS,
    CAGR_MIN_GAP_DAYS,
    Quarter,
    cagr,
    compute_metrics,
    find_prior,
    growth,
    latest_quarter_yoy,
    quality_warnings,
    ratio,
)
from stock_radar.sources.sec.normalize import Fundamentals

D = dt.date


def fy(year: int, **values: float | None) -> Fundamentals:
    """12月決算の1年度。指定しなかった項目は None。"""
    return Fundamentals(
        cik=1,
        fiscal_year=year,
        period_start=D(year, 1, 1),
        period_end=D(year, 12, 31),
        period_days=365 if year % 4 else 366,
        **values,  # type: ignore[arg-type]
    )


# --- ratio ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (10.0, 100.0, 0.1),
        (-10.0, 100.0, -0.1),
        # 分母ゼロを 0 や inf で埋めない。
        (10.0, 0.0, None),
        (0.0, 0.0, None),
        (None, 100.0, None),
        (10.0, None, None),
        # 自己資本マイナス。足切り対象だが計算過程では落ちない。
        (10.0, -50.0, -0.2),
    ],
)
def test_ratio(numerator: float | None, denominator: float | None, expected: float | None) -> None:
    assert ratio(numerator, denominator) == expected


# --- growth -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "base", "expected"),
    [
        (125.0, 100.0, 0.25),
        (75.0, 100.0, -0.25),
        # 基準がゼロ以下だと成長率が意味を持たない。
        (100.0, 0.0, None),
        (100.0, -50.0, None),
        (None, 100.0, None),
        (100.0, None, None),
    ],
)
def test_growth(latest: float | None, base: float | None, expected: float | None) -> None:
    got = growth(latest, base)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# --- cagr -------------------------------------------------------------------


def test_cagr_definition_is_cumulative_33_percent_equals_10_percent_a_year() -> None:
    """ツールによって定義が違うので、自前計算の定義をここで固定する。

    「累積 +33% ≒ 年率 10%」（docs/screening-criteria.md）。
    """
    assert cagr(133.0, 100.0) == pytest.approx(0.0997, abs=0.0005)


def test_cagr_of_a_doubling_over_three_years() -> None:
    assert cagr(200.0, 100.0) == pytest.approx(0.2599, abs=0.0005)


def test_cagr_of_no_growth_is_zero() -> None:
    assert cagr(100.0, 100.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("latest", "base"),
    [
        (100.0, 0.0),  # 分母ゼロ
        (100.0, -50.0),  # 基準がマイナス
        (0.0, 100.0),  # 売上が消えた
        (-10.0, 100.0),
        (None, 100.0),
        (100.0, None),
    ],
)
def test_cagr_is_none_when_undefined(latest: float | None, base: float | None) -> None:
    assert cagr(latest, base) is None


def test_cagr_rejects_zero_years() -> None:
    assert cagr(133.0, 100.0, years=0) is None


# --- 比較対象の年度を探す ---------------------------------------------------


def test_find_prior_uses_the_gap_not_the_row_count() -> None:
    """履歴に穴がある企業が278社ある。「3行前」では年数が合わない。"""
    rows = [fy(2024), fy(2023), fy(2018), fy(2017)]
    found = find_prior(rows[1:], rows[0].period_end, CAGR_MIN_GAP_DAYS, CAGR_MAX_GAP_DAYS)
    assert found is None


def test_find_prior_finds_the_year_three_years_back() -> None:
    rows = [fy(2024), fy(2023), fy(2022), fy(2021)]
    found = find_prior(rows[1:], rows[0].period_end, CAGR_MIN_GAP_DAYS, CAGR_MAX_GAP_DAYS)
    assert found is not None
    assert found.fiscal_year == 2021


# --- 直近四半期 YoY ---------------------------------------------------------


def test_latest_quarter_yoy() -> None:
    quarters = [
        Quarter(D(2025, 6, 30), 120.0),
        Quarter(D(2025, 3, 31), 110.0),
        Quarter(D(2024, 6, 30), 100.0),
    ]
    assert latest_quarter_yoy(quarters) == pytest.approx(0.20)


def test_latest_quarter_yoy_tolerates_a_shifted_period_end() -> None:
    """52/53週決算だと前年同期がぴったり365日前に来ない。"""
    quarters = [Quarter(D(2025, 6, 28), 120.0), Quarter(D(2024, 6, 29), 100.0)]
    assert latest_quarter_yoy(quarters) == pytest.approx(0.20)


def test_latest_quarter_yoy_needs_a_prior_year() -> None:
    assert latest_quarter_yoy([Quarter(D(2025, 6, 30), 120.0)]) is None


def test_latest_quarter_yoy_without_data() -> None:
    assert latest_quarter_yoy([]) is None


def test_latest_quarter_yoy_ignores_an_unrelated_quarter() -> None:
    quarters = [Quarter(D(2025, 6, 30), 120.0), Quarter(D(2025, 3, 31), 110.0)]
    assert latest_quarter_yoy(quarters) is None


# --- まとめて計算 -----------------------------------------------------------


def full_history() -> list[Fundamentals]:
    return [
        fy(
            2024,
            revenue=1000.0,
            gross_profit=450.0,
            operating_income=120.0,
            net_income=90.0,
            total_assets=1500.0,
            equity=900.0,
            current_assets=600.0,
            current_liabilities=300.0,
            cfo=200.0,
            capex=50.0,
        ),
        fy(2023, revenue=900.0, operating_income=100.0, total_assets=1400.0),
        fy(2022, revenue=800.0),
        fy(2021, revenue=700.0),
    ]


def test_compute_metrics_on_a_full_history() -> None:
    metrics = compute_metrics(full_history())
    assert metrics is not None
    assert metrics.revenue_cagr_3y == pytest.approx(cagr(1000.0, 700.0))
    assert metrics.revenue_growth_yoy == pytest.approx(1000.0 / 900.0 - 1)
    assert metrics.op_margin == pytest.approx(0.12)
    assert metrics.gross_margin == pytest.approx(0.45)
    assert metrics.fcf == pytest.approx(150.0)
    assert metrics.roa == pytest.approx(0.06)
    assert metrics.roe == pytest.approx(0.1)
    assert metrics.equity_ratio == pytest.approx(0.6)
    assert metrics.current_ratio == pytest.approx(2.0)
    # 資産成長率 (1500/1400-1) − EBIT成長率 (120/100-1)
    assert metrics.asset_growth_minus_ebit_growth == pytest.approx((1500 / 1400 - 1) - 0.2)


def test_compute_metrics_without_rows() -> None:
    assert compute_metrics([]) is None


def test_recent_ipo_has_no_three_year_cagr() -> None:
    """履歴3年未満。落とさずに None を返す。"""
    metrics = compute_metrics([fy(2024, revenue=100.0), fy(2023, revenue=50.0)])
    assert metrics is not None
    assert metrics.revenue_cagr_3y is None
    assert metrics.revenue_growth_yoy == pytest.approx(1.0)


def test_missing_gross_profit_does_not_break_the_rest() -> None:
    """粗利を開示しない企業で例外にならないこと。28.8% がこれに当たる。"""
    metrics = compute_metrics([fy(2024, revenue=1000.0, operating_income=50.0)])
    assert metrics is not None
    assert metrics.gross_margin is None
    assert metrics.op_margin == pytest.approx(0.05)


def test_gross_margin_change_over_three_years() -> None:
    """粗利率の3年変化は率の差（pt）。低下はマイナスで出る。"""
    metrics = compute_metrics(
        [
            fy(2024, revenue=1000.0, gross_profit=600.0),
            fy(2023, revenue=900.0, gross_profit=600.0),
            fy(2022, revenue=800.0),
            fy(2021, revenue=700.0, gross_profit=490.0),
        ]
    )
    assert metrics is not None
    assert metrics.gross_margin_change_3y == pytest.approx(0.60 - 0.70)


def test_gross_margin_change_needs_both_ends() -> None:
    """どちらかの年度で粗利が取れなければ None。0 で埋めない。"""
    no_base = compute_metrics(
        [fy(2024, revenue=1000.0, gross_profit=600.0), fy(2021, revenue=700.0)]
    )
    no_latest = compute_metrics(
        [fy(2024, revenue=1000.0), fy(2021, revenue=700.0, gross_profit=490.0)]
    )
    assert no_base is not None and no_base.gross_margin_change_3y is None
    assert no_latest is not None and no_latest.gross_margin_change_3y is None


def test_zero_revenue_does_not_divide_by_zero() -> None:
    metrics = compute_metrics([fy(2024, revenue=0.0, operating_income=-10.0)])
    assert metrics is not None
    assert metrics.op_margin is None
    assert metrics.gross_margin is None


def test_negative_equity_is_computed_not_crashed() -> None:
    """足切り対象だが、計算過程で落ちないこと。

    **赤字 ÷ 債務超過はプラスの ROE になる。** 数式としてはそうなるが、これを
    「収益性が高い」と読むと誤る。共通足切りの equity_positive が先に効く前提で、
    ここでは値をそのまま出す。
    """
    metrics = compute_metrics(
        [fy(2024, revenue=100.0, net_income=-50.0, equity=-200.0, total_assets=300.0)]
    )
    assert metrics is not None
    assert metrics.roe == pytest.approx(0.25)
    assert metrics.equity_ratio == pytest.approx(-200 / 300)


def test_zero_current_liabilities() -> None:
    metrics = compute_metrics([fy(2024, current_assets=100.0, current_liabilities=0.0)])
    assert metrics is not None
    assert metrics.current_ratio is None


def test_history_with_a_hole_has_no_cagr() -> None:
    """期末が1,000〜1,190日前の年度が無ければ計算しない。278社で実在する。"""
    metrics = compute_metrics(
        [fy(2024, revenue=1000.0), fy(2023, revenue=900.0), fy(2015, revenue=100.0)]
    )
    assert metrics is not None
    assert metrics.revenue_cagr_3y is None


# --- 実行時のデータ品質チェック ---------------------------------------------


def test_warns_on_negative_revenue() -> None:
    assert "売上がマイナス" in quality_warnings(fy(2024, revenue=-1.0), None)


def test_warns_on_equity_ratio_above_one() -> None:
    row = fy(2024, equity=200.0, total_assets=100.0)
    assert "自己資本比率が1を超えている" in quality_warnings(row, None)


def test_warns_on_a_non_twelve_month_year() -> None:
    row = Fundamentals(
        cik=1,
        fiscal_year=2024,
        period_start=D(2024, 1, 1),
        period_end=D(2024, 6, 30),
        period_days=182,
    )
    assert any("12ヶ月でない" in w for w in quality_warnings(row, None))


def test_warns_on_a_hundredfold_revenue_jump() -> None:
    warnings = quality_warnings(fy(2024, revenue=10_000.0), fy(2023, revenue=1.0))
    assert "売上が前年比100倍を超えている" in warnings


def test_clean_data_has_no_warnings() -> None:
    assert quality_warnings(full_history()[0], full_history()[1]) == ()


def test_warnings_are_carried_on_the_metrics() -> None:
    metrics = compute_metrics([fy(2024, revenue=-5.0)])
    assert metrics is not None
    assert metrics.warnings


# --- 合成ユニバースによる回帰テスト（docs/testing.md） ----------------------
#
# 架空の財務データを手で作り、指標の値を固定する。閾値は何度も調整する前提なので、
# 計算側を触ったときに意図しない副作用が出ていないかをここで検知する。
#
# トラック A / B の通過判定そのものは Phase 4（条件判定）で固定する。
# ここは判定の入力になる値を押さえる層。

SYNTHETIC: dict[str, list[Fundamentals]] = {
    # 黒字・安定成長。トラックA が想定する形。
    "steady_profitable": [
        fy(
            2024,
            revenue=1330.0,
            gross_profit=600.0,
            operating_income=200.0,
            net_income=150.0,
            total_assets=2000.0,
            equity=1200.0,
            current_assets=800.0,
            current_liabilities=400.0,
            cfo=300.0,
            capex=60.0,
        ),
        fy(2023, revenue=1200.0, operating_income=180.0, total_assets=1900.0),
        fy(2022, revenue=1100.0),
        fy(2021, revenue=1000.0, gross_profit=400.0),
    ],
    # 赤字・高成長・高粗利。トラックB が想定する形。
    "loss_making_grower": [
        fy(
            2024,
            revenue=500.0,
            gross_profit=350.0,
            operating_income=-80.0,
            net_income=-90.0,
            total_assets=900.0,
            equity=600.0,
            current_assets=500.0,
            current_liabilities=200.0,
            cfo=-40.0,
            capex=20.0,
        ),
        fy(2023, revenue=350.0, operating_income=-70.0, total_assets=700.0),
        fy(2022, revenue=220.0),
        fy(2021, revenue=150.0),
    ],
    # 粗利を開示しない業態。28.8% がこれ。
    "no_gross_profit": [
        fy(
            2024,
            revenue=2000.0,
            operating_income=150.0,
            net_income=100.0,
            total_assets=5000.0,
            equity=1500.0,
            current_assets=600.0,
            current_liabilities=1200.0,
            cfo=400.0,
            capex=350.0,
        ),
        fy(2023, revenue=1900.0, operating_income=140.0, total_assets=4800.0),
        fy(2022, revenue=1800.0),
        fy(2021, revenue=1700.0),
    ],
    # 資産だけ膨らんで収益が伴わない。トラックA が外したい形。
    "asset_bloat": [
        fy(
            2024,
            revenue=1000.0,
            gross_profit=300.0,
            operating_income=50.0,
            net_income=20.0,
            total_assets=4000.0,
            equity=1000.0,
            current_assets=500.0,
            current_liabilities=900.0,
            cfo=80.0,
            capex=500.0,
        ),
        fy(2023, revenue=950.0, operating_income=60.0, total_assets=2000.0),
        fy(2022, revenue=900.0),
        fy(2021, revenue=850.0),
    ],
    # 上場直後。3年CAGR が計算できない。
    "recent_ipo": [
        fy(
            2024,
            revenue=300.0,
            gross_profit=180.0,
            operating_income=-20.0,
            net_income=-25.0,
            total_assets=400.0,
            equity=300.0,
            current_assets=350.0,
            current_liabilities=100.0,
            cfo=-10.0,
            capex=5.0,
        ),
        fy(2023, revenue=180.0, operating_income=-30.0, total_assets=250.0),
    ],
    # 債務超過。共通足切りで落ちるが、計算過程で落ちないこと。
    "negative_equity": [
        fy(
            2024,
            revenue=800.0,
            gross_profit=200.0,
            operating_income=-50.0,
            net_income=-120.0,
            total_assets=600.0,
            equity=-100.0,
            current_assets=200.0,
            current_liabilities=500.0,
            cfo=-30.0,
            capex=10.0,
        ),
        fy(2023, revenue=820.0, operating_income=-40.0, total_assets=700.0),
        fy(2022, revenue=850.0),
        fy(2021, revenue=900.0),
    ],
}

# 期待値。小数第4位まで固定する。
EXPECTED: dict[str, dict[str, float | None]] = {
    "steady_profitable": {
        "revenue_cagr_3y": 0.0997,
        "revenue_growth_yoy": 0.1083,
        "op_margin": 0.1504,
        "gross_margin": 0.4511,
        "fcf": 240.0,
        "roa": 0.075,
        "roe": 0.125,
        "equity_ratio": 0.6,
        "current_ratio": 2.0,
        "asset_growth_minus_ebit_growth": -0.0585,
        "gross_margin_change_3y": 0.0511,
    },
    "loss_making_grower": {
        "revenue_cagr_3y": 0.4938,
        "revenue_growth_yoy": 0.4286,
        "op_margin": -0.16,
        "gross_margin": 0.7,
        "fcf": -60.0,
        "roa": -0.1,
        "roe": -0.15,
        "equity_ratio": 0.6667,
        "current_ratio": 2.5,
        # 前期の EBIT が赤字なので「EBIT成長率」は定義できない。
        # -80 / -70 - 1 = +14% と出すと、赤字が拡大したのに成長したことになる。
        "asset_growth_minus_ebit_growth": None,
        # 3年前の粗利が無い。
        "gross_margin_change_3y": None,
    },
    "no_gross_profit": {
        "revenue_cagr_3y": 0.0557,
        "revenue_growth_yoy": 0.0526,
        "op_margin": 0.075,
        "gross_margin": None,
        "fcf": 50.0,
        "roa": 0.02,
        "roe": 0.0667,
        "equity_ratio": 0.3,
        "current_ratio": 0.5,
        "asset_growth_minus_ebit_growth": -0.0298,
        "gross_margin_change_3y": None,
    },
    "asset_bloat": {
        "revenue_cagr_3y": 0.0557,
        "revenue_growth_yoy": 0.0526,
        "op_margin": 0.05,
        "gross_margin": 0.3,
        "fcf": -420.0,
        "roa": 0.005,
        "roe": 0.02,
        "equity_ratio": 0.25,
        "current_ratio": 0.5556,
        "asset_growth_minus_ebit_growth": 1.1667,
        "gross_margin_change_3y": None,
    },
    "recent_ipo": {
        "revenue_cagr_3y": None,
        "revenue_growth_yoy": 0.6667,
        "op_margin": -0.0667,
        "gross_margin": 0.6,
        "fcf": -15.0,
        "roa": -0.0625,
        "roe": -0.0833,
        "equity_ratio": 0.75,
        "current_ratio": 3.5,
        "asset_growth_minus_ebit_growth": None,
        # 3年前の年度が無い。
        "gross_margin_change_3y": None,
    },
    "negative_equity": {
        "revenue_cagr_3y": -0.0385,
        "revenue_growth_yoy": -0.0244,
        "op_margin": -0.0625,
        "gross_margin": 0.25,
        "fcf": -40.0,
        "roa": -0.2,
        "roe": 1.2,
        "equity_ratio": -0.1667,
        "current_ratio": 0.4,
        "asset_growth_minus_ebit_growth": None,
        "gross_margin_change_3y": None,
    },
}


@pytest.mark.parametrize("name", sorted(SYNTHETIC))
def test_synthetic_universe_metrics_are_fixed(name: str) -> None:
    metrics = compute_metrics(SYNTHETIC[name])
    assert metrics is not None
    for field, expected in EXPECTED[name].items():
        got = getattr(metrics, field)
        if expected is None:
            assert got is None, f"{name}.{field} は None のはず（実際 {got}）"
        else:
            assert got == pytest.approx(expected, abs=5e-5), f"{name}.{field}"


def test_synthetic_universe_covers_every_metric() -> None:
    """期待値の取りこぼしが無いこと。指標を足したらここで気づく。"""
    metrics = compute_metrics(SYNTHETIC["steady_profitable"])
    assert metrics is not None
    computed = {
        f
        for f in metrics.__slots__
        if f
        not in {
            "cik",
            "fiscal_year",
            "period_end",
            "warnings",
            "ebit_turned_positive",
            "revenue_growth_latest_quarter_yoy",
        }
    }
    assert computed == set(EXPECTED["steady_profitable"])


def test_ebit_growth_from_a_loss_is_not_computed() -> None:
    """前期が赤字のときの「EBIT成長率」は定義しない。

    -80 / -70 - 1 = +14% と出すと、**赤字が拡大したのに成長した**ことになる。
    トラックA は営業利益率プラスを求めるので実害は無いが、符号が反転する計算を
    黙って通さない。
    """
    metrics = compute_metrics(
        [
            fy(2024, operating_income=-80.0, total_assets=900.0),
            fy(2023, operating_income=-70.0, total_assets=700.0),
        ]
    )
    assert metrics is not None
    assert metrics.asset_growth_minus_ebit_growth is None


def test_turning_profitable_is_flagged() -> None:
    """前期赤字 → 当期黒字。差は定義できないが、改善したことは記録する。

    トラックA は当期の営業黒字を求めるので、この165社だけが実際に影響を受ける。
    判定に使うかは Phase 4 で決める。
    """
    metrics = compute_metrics(
        [
            fy(2024, revenue=1000.0, operating_income=50.0, total_assets=900.0),
            fy(2023, revenue=900.0, operating_income=-30.0, total_assets=700.0),
        ]
    )
    assert metrics is not None
    assert metrics.asset_growth_minus_ebit_growth is None
    assert metrics.ebit_turned_positive is True


def test_still_loss_making_is_not_flagged() -> None:
    metrics = compute_metrics(
        [
            fy(2024, operating_income=-10.0, total_assets=900.0),
            fy(2023, operating_income=-30.0, total_assets=700.0),
        ]
    )
    assert metrics is not None
    assert metrics.ebit_turned_positive is False
