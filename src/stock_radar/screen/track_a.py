"""トラックA：黒字・割安型（Yartseva 準拠）。

条件の出どころは `docs/screening-criteria.md` の②。米国版では EBITDA を EBIT で
代用している（減価償却費の XBRL タグ揺れが激しいため。同ドキュメントの読み替え表）。

株価が要るのは FCF 利回りと PBR の2つ。残りは財務だけで判定できる。
"""

from __future__ import annotations

from stock_radar.config import Criteria
from stock_radar.metrics.fundamentals import FundamentalMetrics
from stock_radar.screen.filters import Candidate, Check, Verdict, check, check_any

__all__ = ["ebit_discipline", "evaluate"]


def ebit_discipline(metrics: FundamentalMetrics, criteria: Criteria) -> Check:
    """資産成長率 − EBIT成長率 ≤ 0。**黒字転換は通す。**

    Yartseva の「資産成長率が EBITDA 成長率を上回ると翌年リターンが平均約23pt低下」
    に対応する条件で、趣旨は**資産を膨らませているのに収益が伴わない企業を外す**こと。

    ⚠️ 前期が営業赤字だと EBIT成長率が定義できない（`-80 / -70 - 1 = +14%` と出すと
    赤字拡大が成長になる）。指標側は正直に None を返しており、母集団の 44.9% が
    これに当たる。ただしトラックA は当期の営業黒字を要求するので、実際に効くのは
    **前期赤字から黒字転換した165社**だけ（docs/xbrl-findings.md の E）。

    🔵 2026-09-21 ユーザー決定：**黒字転換は通す。** 赤字から黒字に転じた企業は
    条件の趣旨に反しないため。`passed_filters` には別名で残し、差を後から追えるようにする。
    """
    threshold = criteria.track_a.asset_growth_minus_ebit_growth
    spread = metrics.asset_growth_minus_ebit_growth
    if spread is not None:
        return check("track_a.asset_growth_minus_ebit_growth", spread, threshold)
    if metrics.ebit_turned_positive:
        return Check("track_a.ebit_turned_positive", Verdict.PASS)
    return Check("track_a.asset_growth_minus_ebit_growth", Verdict.UNKNOWN)


def evaluate(candidate: Candidate, criteria: Criteria, *, with_price: bool = True) -> list[Check]:
    """トラックA の条件をすべて当てる。全部 PASS なら通過。"""
    track = criteria.track_a
    metrics = candidate.metrics
    checks = [
        check("track_a.revenue_cagr_3y", metrics.revenue_cagr_3y, track.revenue_cagr_3y),
        check("track_a.op_margin", metrics.op_margin, track.op_margin.for_market(candidate.market)),
        check_any(
            "track_a.profitability_any",
            [
                check("track_a.roa", metrics.roa, track.profitability_any.roa),
                check("track_a.roe", metrics.roe, track.profitability_any.roe),
            ],
        ),
        ebit_discipline(metrics, criteria),
    ]
    if with_price:
        checks.append(
            check("track_a.fcf_yield", candidate.fcf_yield, track.fcf_yield, needs_price=True)
        )
        checks.append(check("track_a.pbr", candidate.pbr, track.pbr, needs_price=True))
    return checks
