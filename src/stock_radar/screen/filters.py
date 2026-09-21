"""判定の三値と、共通足切り。

**DB も設定ファイルも触らない純粋関数**（docs/testing.md のレイヤ表で「ユニット・高」）。
入力は組み立て済みの `Candidate` で、DuckDB との受け渡しは `screen/runner.py` の責務。

## 判定を三値にしている理由

「閾値未満で落ちた」と「値が無くて判定できなかった」は別物で、混ぜると閾値調整の
判断ができなくなる。粗利率がその代表で、実測では **28.8% の企業が粗利を算出できない**
（docs/xbrl-findings.md の D）。粗利率 40% 未満で落ちた企業と、粗利の行を持たない
航空・電力を同じ「不通過」にまとめると、閾値を下げても候補が増えない理由が読めない。

**判定不能は落とす**（2026-09-21 ユーザー決定。docs/open-questions.md）。
ただし記録は分ける。これが `config/criteria.yaml` の「判定不能と閾値未満を区別して
記録すること」の実体。

## ここで判定しないもの

共通足切りのうち**業種除外（SIC 6000番台）と ADR 除外（10-K 非提出）は
`universe` の段階で済んでいる**（`excluded_reason`）。ここまで来る銘柄は
通過済みなので、条件を二重に当てない。`criteria.common` の該当フラグは
ユニバース構築側が読む。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from stock_radar.config import Criteria, Market, Threshold
from stock_radar.metrics.fundamentals import FundamentalMetrics, ratio
from stock_radar.metrics.market import MarketMetrics
from stock_radar.metrics.market import fcf_yield as _fcf_yield
from stock_radar.metrics.market import psr as _psr
from stock_radar.sources.sec.normalize import Fundamentals

__all__ = [
    "Candidate",
    "Check",
    "Verdict",
    "check",
    "check_any",
    "check_positive",
    "common_checks",
]


class Verdict(StrEnum):
    """1つの条件に対する判定。

    ``UNKNOWN`` は「値が取れず判定できなかった」。通過はしないが、
    ``FAIL``（閾値に届かなかった）とは別物として残す。
    """

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Check:
    """1条件の判定結果。``value`` は判定に使った値（取れなければ None）。

    ``needs_price`` は株価が要る条件（時価総額・売買代金・PBR・PSR・FCF利回り）。
    パイプラインでは財務の足切りが先なので、**財務で落ちた銘柄の株価条件は
    「取りこぼし」ではなく「そもそも取りに行っていない」**。両者を混ぜると
    `price_coverage` が実態とかけ離れる。
    """

    name: str
    verdict: Verdict
    value: float | None = None
    needs_price: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    @property
    def undecidable(self) -> bool:
        return self.verdict is Verdict.UNKNOWN


@dataclass(frozen=True, slots=True)
class Candidate:
    """1銘柄ぶんの判定材料。

    `universe`（銘柄）・`fundamentals`（財務）・`market_metrics`（株価）は粒度が違う。
    **財務は CIK 単位、株価はティッカー単位**で、複数クラス株では1つの CIK に
    複数のティッカーがぶら下がる（docs/xbrl-findings.md の C）。ここで1行に束ねる。

    ``quotes`` が None なのは、株価をまだ取っていない銘柄（Phase 3 の時間予算で
    打ち切られた分を含む）と、株価条件を飛ばして財務だけ測るとき。
    """

    market: Market
    ticker: str
    cik: int | None
    name: str | None
    fundamentals: Fundamentals
    metrics: FundamentalMetrics
    quotes: MarketMetrics | None = None

    # --- 株価が要る指標。株価が無ければ None のまま判定不能になる ---------

    @property
    def market_cap(self) -> float | None:
        return self.quotes.market_cap if self.quotes is not None else None

    @property
    def avg_daily_value(self) -> float | None:
        return self.quotes.avg_daily_value if self.quotes is not None else None

    @property
    def drawdown_from_52w_high(self) -> float | None:
        return self.quotes.drawdown_from_52w_high if self.quotes is not None else None

    @property
    def range_position_52w(self) -> float | None:
        return self.quotes.range_position_52w if self.quotes is not None else None

    @property
    def pbr(self) -> float | None:
        """時価総額 / 自己資本。自己資本マイナスの銘柄は共通足切りで先に落ちる。"""
        return ratio(self.market_cap, self.fundamentals.equity)

    @property
    def psr(self) -> float | None:
        return _psr(self.market_cap, self.fundamentals.revenue)

    @property
    def fcf_yield(self) -> float | None:
        return _fcf_yield(self.metrics.fcf, self.market_cap)


def check(
    name: str, value: float | None, threshold: Threshold, *, needs_price: bool = False
) -> Check:
    """閾値に当てる。値が無ければ判定不能。"""
    if value is None:
        return Check(name, Verdict.UNKNOWN, needs_price=needs_price)
    verdict = Verdict.PASS if threshold.contains(value) else Verdict.FAIL
    return Check(name, verdict, value, needs_price=needs_price)


def check_positive(name: str, value: float | None) -> Check:
    """プラスであること。自己資本と売上のゼロ除外に使う。"""
    if value is None:
        return Check(name, Verdict.UNKNOWN)
    return Check(name, Verdict.PASS if value > 0 else Verdict.FAIL, value)


def check_any(name: str, alternatives: Sequence[Check]) -> Check:
    """どれか1つ満たせばよい条件（ROA/ROE、自己資本比率/流動比率）。

    **片方が判定不能でも、もう片方が満たしていれば通す。** 両方とも満たさず、
    かつ判定不能が混じっているときだけ判定不能にする。
    ROE は取れないが ROA は 8% ある、という銘柄を落とす理由が無い。
    """
    for alternative in alternatives:
        if alternative.passed:
            return Check(name, Verdict.PASS, alternative.value)
    if any(alternative.undecidable for alternative in alternatives):
        return Check(name, Verdict.UNKNOWN)
    return Check(name, Verdict.FAIL)


def common_checks(
    candidate: Candidate, criteria: Criteria, *, with_price: bool = True
) -> list[Check]:
    """共通足切り。トラック A / B のどちらに進むにもここを通る。

    ``with_price=False`` は株価がまだ無い段階での足切り。CLAUDE.md の
    「**株価取得は必ず財務による足切りの後に実行する**」がここに効く。
    時価総額と売買代金を飛ばし、財務だけで落とせる分を落とす。
    """
    common = criteria.common
    checks: list[Check] = []
    if common.equity_positive:
        checks.append(check_positive("common.equity_positive", candidate.fundamentals.equity))
    if common.revenue_positive:
        checks.append(check_positive("common.revenue_positive", candidate.fundamentals.revenue))
    if with_price:
        limits = common.for_market(candidate.market)
        checks.append(
            check("common.market_cap", candidate.market_cap, limits.market_cap, needs_price=True)
        )
        checks.append(
            check(
                "common.avg_daily_value",
                candidate.avg_daily_value,
                limits.avg_daily_value,
                needs_price=True,
            )
        )
    return checks
