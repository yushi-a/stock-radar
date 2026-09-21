"""トラックB：高成長型（赤字許容）。

実証研究の裏付けは弱く、日本データの「高い売上成長」と既存の評価フレームからの
見解でできている（`docs/screening-criteria.md` の③）。

米国版では会社予想が取れない（10-K/10-Q にガイダンスが無い）ため、
「予想売上成長率 ≥ 20%」を**直近四半期の前年同期比**で代替している。

株価が要るのは PSR だけ。
"""

from __future__ import annotations

from stock_radar.config import Criteria
from stock_radar.screen.filters import Candidate, Check, check, check_any

__all__ = ["evaluate"]


def evaluate(candidate: Candidate, criteria: Criteria, *, with_price: bool = True) -> list[Check]:
    """トラックB の条件をすべて当てる。全部 PASS なら通過。

    粗利率は**判定不能なら落ちる**（ハードフィルタのまま。2026-09-21 ユーザー決定）。
    実測で 28.8% の企業が粗利を算出できないが、スキップしても増えるのは最大23社で、
    内訳は石油・医薬・サービスという粗利率40%が外そうとしている業態そのものだった
    （docs/xbrl-findings.md の F）。落ちた理由が「粗利不明」か「40%未満」かは
    `Verdict` で区別して残る。
    """
    track = criteria.track_b
    metrics = candidate.metrics
    checks = [
        check("track_b.revenue_growth_yoy", metrics.revenue_growth_yoy, track.revenue_growth_yoy),
        check(
            "track_b.revenue_growth_latest_quarter_yoy",
            metrics.revenue_growth_latest_quarter_yoy,
            track.revenue_growth_latest_quarter_yoy,
        ),
        check("track_b.op_margin", metrics.op_margin, track.op_margin),
        check("track_b.gross_margin", metrics.gross_margin, track.gross_margin),
        check_any(
            "track_b.financial_buffer_any",
            [
                check(
                    "track_b.equity_ratio",
                    metrics.equity_ratio,
                    track.financial_buffer_any.equity_ratio,
                ),
                check(
                    "track_b.current_ratio",
                    metrics.current_ratio,
                    track.financial_buffer_any.current_ratio,
                ),
            ],
        ),
    ]
    if with_price:
        checks.append(check("track_b.psr", candidate.psr, track.psr, needs_price=True))
    return checks
