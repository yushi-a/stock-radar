"""財務指標の計算。

**純粋関数として書く。** DB も設定も触らない。`docs/testing.md` のレイヤ表で
「ユニット・高」に置かれている層で、境界値が本番になる。

- 分母ゼロ（売上ゼロ、自己資本ゼロ、流動負債ゼロ）
- 自己資本マイナス（足切り対象だが、計算過程で落ちないこと）
- 履歴3年未満（3年CAGR が計算不能）
- 欠損の伝播（粗利を開示しない企業で例外にならないこと）

**取れなかった指標は None を返す。** 0 を返すと「ゼロだった」と区別できない。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from stock_radar.sources.sec.normalize import Fundamentals

__all__ = [
    "CAGR_MAX_GAP_DAYS",
    "CAGR_MIN_GAP_DAYS",
    "CAGR_YEARS",
    "QUARTER_MATCH_TOLERANCE_DAYS",
    "YOY_MAX_GAP_DAYS",
    "YOY_MIN_GAP_DAYS",
    "FundamentalMetrics",
    "Quarter",
    "cagr",
    "compute_metrics",
    "compute_universe_metrics",
    "find_prior",
    "growth",
    "latest_quarter_yoy",
    "quality_warnings",
    "ratio",
]

# 3年CAGR は「3行前」ではなく期末の間隔で探す。履歴に穴がある企業が
# 278社あり、行数で数えると年数が合わない（docs/xbrl-findings.md の A-7）。
CAGR_YEARS = 3
CAGR_MIN_GAP_DAYS = 1_000
CAGR_MAX_GAP_DAYS = 1_190
# 年次 YoY。52/53週決算のぶれを吸収する。
YOY_MIN_GAP_DAYS = 340
YOY_MAX_GAP_DAYS = 390
# 四半期の前年同期を探す窓。
QUARTER_MATCH_TOLERANCE_DAYS = 20


@dataclass(frozen=True, slots=True)
class Quarter:
    period_end: dt.date
    revenue: float


@dataclass(frozen=True, slots=True)
class FundamentalMetrics:
    """条件判定に渡す値。取れなかったものは None。"""

    cik: int
    fiscal_year: int
    period_end: dt.date
    revenue_cagr_3y: float | None = None
    revenue_growth_yoy: float | None = None
    revenue_growth_latest_quarter_yoy: float | None = None
    op_margin: float | None = None
    gross_margin: float | None = None
    fcf: float | None = None
    roa: float | None = None
    roe: float | None = None
    equity_ratio: float | None = None
    current_ratio: float | None = None
    asset_growth_minus_ebit_growth: float | None = None
    # 前期の営業利益が赤字だと「EBIT成長率」を定義できず、上の差は None になる。
    # 母集団の 44.9% がこれに当たるが、トラックA は当期の営業黒字を求めるので
    # 実際に効くのは**黒字転換した165社**だけ。
    # 「資産を膨らませているのに収益が伴わない企業を外す」という条件の趣旨からは
    # 黒字転換は通すべきに見えるが、判定に使うかは Phase 4 で決める。
    ebit_turned_positive: bool = False
    warnings: tuple[str, ...] = ()


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """割り算。分母がゼロか、どちらかが欠損なら None。

    ゼロ割りを 0 や inf で埋めない。「計算できなかった」を値で表さない。
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def growth(latest: float | None, base: float | None) -> float | None:
    """成長率。基準がゼロ以下なら None。

    基準がマイナスのときの「成長率」は意味を持たない（赤字が縮んだのか
    拡大したのかが符号で反転する）ので計算しない。
    """
    if latest is None or base is None or base <= 0:
        return None
    return latest / base - 1


def cagr(latest: float | None, base: float | None, years: int = CAGR_YEARS) -> float | None:
    """年平均成長率。

    定義は「累積 +33% ≒ 年率 10%」＝ ``(latest / base) ** (1 / years) - 1``。
    ツールによって「3年前比の累積」「2年間の年率」など定義が違うので、
    自前計算に統一する（docs/screening-criteria.md）。
    """
    if latest is None or base is None or base <= 0 or years <= 0:
        return None
    if latest <= 0:
        # 売上が消えた企業。成長率としては表せない。
        return None
    return (latest / base) ** (1 / years) - 1


def find_prior(
    rows: Sequence[Fundamentals], anchor: dt.date, min_gap: int, max_gap: int
) -> Fundamentals | None:
    """``anchor`` から ``min_gap``〜``max_gap`` 日前の年度を探す。

    行数で数えない。履歴に穴がある企業では「3行前」が3年前とは限らない。
    """
    for row in rows:
        gap = (anchor - row.period_end).days
        if min_gap <= gap <= max_gap:
            return row
    return None


def latest_quarter_yoy(
    quarters: Iterable[Quarter], *, tolerance_days: int = QUARTER_MATCH_TOLERANCE_DAYS
) -> float | None:
    """直近四半期の前年同期比。

    米国 XBRL は3ヶ月値を直接報告しているので YTD の引き算は要らない
    （母集団の 92.4%。docs/xbrl-findings.md の B）。
    """
    ordered = sorted(quarters, key=lambda q: q.period_end, reverse=True)
    if not ordered:
        return None
    latest = ordered[0]
    target = latest.period_end - dt.timedelta(days=365)
    matches = [q for q in ordered[1:] if abs((q.period_end - target).days) <= tolerance_days]
    if not matches:
        return None
    prior = min(matches, key=lambda q: abs((q.period_end - target).days))
    return growth(latest.revenue, prior.revenue)


def quality_warnings(row: Fundamentals, prior: Fundamentals | None) -> tuple[str, ...]:
    """実行時のデータ品質チェック。

    テストで捕まえられないデータ起因の問題はここでしか捕まらない
    （docs/testing.md）。落とさずに記録する。
    """
    found: list[str] = []
    if row.revenue is not None and row.revenue < 0:
        found.append("売上がマイナス")
    if row.total_assets is not None and row.total_assets < 0:
        found.append("総資産がマイナス")
    equity_ratio = ratio(row.equity, row.total_assets)
    if equity_ratio is not None and equity_ratio > 1:
        found.append("自己資本比率が1を超えている")
    if row.period_days is not None and not 350 <= row.period_days <= 380:
        found.append(f"年度の長さが12ヶ月でない（{row.period_days}日）")
    if prior is not None:
        change = growth(row.revenue, prior.revenue)
        if change is not None and change > 99:
            found.append("売上が前年比100倍を超えている")
    return tuple(found)


def compute_metrics(
    rows: Sequence[Fundamentals], quarters: Iterable[Quarter] = ()
) -> FundamentalMetrics | None:
    """直近年度の指標を計算する。``rows`` は新しい順。"""
    if not rows:
        return None
    latest = rows[0]
    older = rows[1:]

    prior_year = find_prior(older, latest.period_end, YOY_MIN_GAP_DAYS, YOY_MAX_GAP_DAYS)
    three_years_ago = find_prior(older, latest.period_end, CAGR_MIN_GAP_DAYS, CAGR_MAX_GAP_DAYS)

    asset_growth = growth(latest.total_assets, prior_year.total_assets) if prior_year else None
    ebit_growth = (
        growth(latest.operating_income, prior_year.operating_income) if prior_year else None
    )
    spread = (
        asset_growth - ebit_growth if asset_growth is not None and ebit_growth is not None else None
    )

    turned_positive = bool(
        prior_year is not None
        and prior_year.operating_income is not None
        and prior_year.operating_income <= 0
        and latest.operating_income is not None
        and latest.operating_income > 0
    )

    fcf = latest.cfo - latest.capex if latest.cfo is not None and latest.capex is not None else None

    return FundamentalMetrics(
        cik=latest.cik,
        fiscal_year=latest.fiscal_year,
        period_end=latest.period_end,
        revenue_cagr_3y=(
            cagr(latest.revenue, three_years_ago.revenue) if three_years_ago else None
        ),
        revenue_growth_yoy=(growth(latest.revenue, prior_year.revenue) if prior_year else None),
        revenue_growth_latest_quarter_yoy=latest_quarter_yoy(quarters),
        op_margin=ratio(latest.operating_income, latest.revenue),
        gross_margin=ratio(latest.gross_profit, latest.revenue),
        fcf=fcf,
        roa=ratio(latest.net_income, latest.total_assets),
        roe=ratio(latest.net_income, latest.equity),
        equity_ratio=ratio(latest.equity, latest.total_assets),
        current_ratio=ratio(latest.current_assets, latest.current_liabilities),
        asset_growth_minus_ebit_growth=spread,
        ebit_turned_positive=turned_positive,
        warnings=quality_warnings(latest, prior_year),
    )


# --- DuckDB から流し込む ----------------------------------------------------

_FUNDAMENTALS_SQL = (
    "SELECT cik, fiscal_year, period_start, period_end, period_days, revenue, "
    "  gross_profit, operating_income, net_income, total_assets, equity, "
    "  current_assets, current_liabilities, cfo, capex, shares_outstanding, currency "
    "FROM fundamentals ORDER BY cik, period_end DESC"
)
_QUARTERS_SQL = (
    "SELECT cik, period_end, value FROM facts_quarterly "
    "WHERE value IS NOT NULL AND unit = 'USD' ORDER BY cik, period_end DESC"
)


def compute_universe_metrics(con: object) -> dict[int, FundamentalMetrics]:
    """`fundamentals` と `facts_quarterly` から全社ぶんの指標を作る。

    Phase 4 の条件判定はここが返す値だけを見る。DB を触るのはこの関数で、
    計算そのものは純粋関数に閉じてある。
    """
    by_company: dict[int, list[Fundamentals]] = {}
    for row in con.execute(_FUNDAMENTALS_SQL).fetchall():  # type: ignore[attr-defined]
        cik = int(row[0])
        by_company.setdefault(cik, []).append(
            Fundamentals(
                cik=cik,
                fiscal_year=row[1],
                period_start=row[2],
                period_end=row[3],
                period_days=row[4],
                revenue=row[5],
                gross_profit=row[6],
                operating_income=row[7],
                net_income=row[8],
                total_assets=row[9],
                equity=row[10],
                current_assets=row[11],
                current_liabilities=row[12],
                cfo=row[13],
                capex=row[14],
                shares_outstanding=row[15],
                currency=row[16],
            )
        )

    quarters: dict[int, list[Quarter]] = {}
    for cik, period_end, value in con.execute(_QUARTERS_SQL).fetchall():  # type: ignore[attr-defined]
        quarters.setdefault(int(cik), []).append(Quarter(period_end, float(value)))

    out: dict[int, FundamentalMetrics] = {}
    for cik, rows in by_company.items():
        metrics = compute_metrics(rows, quarters.get(cik, ()))
        if metrics is not None:
            out[cik] = metrics
    return out
