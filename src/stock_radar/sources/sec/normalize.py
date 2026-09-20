"""縦持ちの facts を、会計年度ごとの横持ち `fundamentals` に正規化する。

**ここが Phase 2 の山**（docs/implementation-plan.md）。XBRL 正規化は静かに間違う。
落ちるバグなら気づくが、決算期変更で6ヶ月しかない「年度」を12ヶ月として扱って
売上CAGRが1.5倍になっても例外は1つも出ない。

判断ルールはすべて Phase 2a の実測に基づく。根拠は `docs/xbrl-findings.md`。

| | ルール |
|---|---|
| A-1 | 会計年度は SEC の `fy` ではなく期末で決める（`fy` は 66% ずれる） |
| A-2/3 | 同じ期間に複数エントリがあれば `filed` が最新を採る |
| A-6 | 期末が数日ずれた同一年度を束ねる（19社で実在） |
| A-8 | `start` の有無で BS（時点値）と PL/CF（期間値）を区別する |
| A-9 | 同じ期末に期間長の違う候補があれば、1年に最も近いものを採る（値が4倍違う例がある） |
| C | 株数は `concepts.SHARES_OUTSTANDING` の順。`dei` のタグは複数クラス株で欠落する |
| D | タグ優先順位は**会計年度ごとに**適用する。1社が複数タグを併用しているのが普通 |
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stock_radar.sources.sec import concepts
from stock_radar.storage import bulk_insert

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "ANCHOR_TOLERANCE_DAYS",
    "FOLD_MIN_GAP_DAYS",
    "FULL_YEAR_DAYS",
    "SHARE_TOLERANCE_DAYS",
    "Fundamentals",
    "Observation",
    "fiscal_year_ends",
    "fiscal_year_of",
    "iter_by_company",
    "normalize_company",
    "normalize_universe",
    "pick",
    "store_fundamentals",
]

# 同じ会計年度とみなす期末の間隔。これ未満なら束ねる（A-6）。
# 実測の最短は1日（DEERE）、最長のケースでも15日以内だった。
FOLD_MIN_GAP_DAYS = 340
# 会計年度の代表日から、この日数以内の期末は同じ年度の値として拾う。
ANCHOR_TOLERANCE_DAYS = 15
# 株数だけは窓を広く取る。`dei:EntityCommonStockSharesOutstanding` は決算期末ではなく
# **10-K の表紙の日付**で報告される（Apple は期末 9/27 に対して 10/17）。
# 10-K の提出期限は期末から60日以内なので、100日あれば隣の年度と混ざらない。
SHARE_TOLERANCE_DAYS = 100
FULL_YEAR_DAYS = 365
# 期末がこの日以前の1月なら、会計年度は前年とみなす。
# 期末が 12/31 から 1/1 にずれただけで翌年度扱いになると、同じ暦年に
# 2つの年度が並ぶ（BK Technologies で実在）。
EARLY_JANUARY_DAY = 7


@dataclass(frozen=True, slots=True)
class Observation:
    """`facts_annual` / `facts_quarterly` の1行。"""

    concept: str
    unit: str
    period_start: dt.date | None
    period_end: dt.date
    value: float | None
    accn: str
    filed_at: dt.date | None

    @property
    def days(self) -> int | None:
        if self.period_start is None:
            return None
        return (self.period_end - self.period_start).days + 1


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """`fundamentals` の1行。値が取れなかった項目は None のままにする。

    **0 を入れない。** 「開示されていない」と「ゼロだった」は別物で、
    混ぜると粗利ゼロの会社と粗利を開示しない会社が区別できなくなる。
    """

    cik: int
    fiscal_year: int
    period_start: dt.date | None
    period_end: dt.date
    period_days: int | None
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    equity: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    cfo: float | None = None
    capex: float | None = None
    shares_outstanding: float | None = None
    currency: str | None = None
    accn: str | None = None
    filed_at: dt.date | None = None
    source_concepts: dict[str, str] = field(default_factory=dict)


def _sort_key(
    observation: Observation, priority: Sequence[str], preferred_unit: str, anchor: dt.date
) -> tuple[int, int, int, int, int]:
    """どの候補を採るかの順序。小さいほうが優先。

    1. 単位。USD を他通貨より優先する（USD 以外のみで報告する企業が4社ある）
    2. タグの優先順位（`concepts.py`。実測の出現率で並べてある）
    3. 期間長が1年からどれだけ離れているか。**同じ期末に 356日と 365日が
       併記され、値が4倍違う例がある**（A-9）
    4. 会計年度の代表日に近いもの
    5. `filed` が新しいもの。修正再提出の最新値を採る（A-3）
    """
    unit_penalty = 0 if observation.unit == preferred_unit else 1
    tag_rank = priority.index(observation.concept)
    length_gap = 0 if observation.days is None else abs(observation.days - FULL_YEAR_DAYS)
    distance = abs((observation.period_end - anchor).days)
    filed_rank = -(observation.filed_at.toordinal() if observation.filed_at else 0)
    return (unit_penalty, tag_rank, length_gap, distance, filed_rank)


def pick(
    observations: Iterable[Observation],
    priority: Sequence[str],
    anchor: dt.date,
    *,
    preferred_unit: str = "USD",
    tolerance_days: int = ANCHOR_TOLERANCE_DAYS,
) -> Observation | None:
    """会計年度 ``anchor`` の値を1つ選ぶ。"""
    candidates = [
        o
        for o in observations
        if o.concept in priority
        and o.value is not None
        and abs((o.period_end - anchor).days) <= tolerance_days
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda o: _sort_key(o, priority, preferred_unit, anchor))


def fiscal_year_of(period_end: dt.date) -> int:
    """会計年度のラベル。

    米国の慣行どおり「その年度が終わる暦年」を使う。ただし期末が1月のごく初めなら
    前年の年度とみなす。12/31 から 1/1 に1日ずれただけで翌年度になってしまうため。

    Walmart（1月末決算）は 2024-01-31 → FY2024 で、会社自身の呼び方と一致する。
    """
    if period_end.month == 1 and period_end.day <= EARLY_JANUARY_DAY:
        return period_end.year - 1
    return period_end.year


def fiscal_year_ends(ends: Iterable[dt.date]) -> list[dt.date]:
    """会計年度を代表する期末を、新しい順に返す。

    期末が数日ずれた同一年度を束ねる（A-6）。束ねないと1年度が2行になり、
    3年CAGR が1年ずれる。
    """
    folded: list[dt.date] = []
    for end in sorted(set(ends), reverse=True):
        if folded and (folded[-1] - end).days < FOLD_MIN_GAP_DAYS:
            continue
        folded.append(end)
    return folded


# 指標名 → (タグ優先順位, 期間値か)
_PERIOD_METRICS: dict[str, Sequence[str]] = {
    "revenue": concepts.REVENUE,
    "gross_profit": concepts.GROSS_PROFIT,
    "operating_income": concepts.OPERATING_INCOME,
    "net_income": concepts.NET_INCOME,
    "cfo": concepts.OPERATING_CASH_FLOW,
    "capex": concepts.CAPEX,
}
_POINT_METRICS: dict[str, Sequence[str]] = {
    "total_assets": concepts.TOTAL_ASSETS,
    "equity": concepts.EQUITY,
    "current_assets": concepts.CURRENT_ASSETS,
    "current_liabilities": concepts.CURRENT_LIABILITIES,
}
_SHARE_PRIORITY = [tag for _, tag in concepts.SHARES_OUTSTANDING]

# 会計年度の代表日を決めるのに使う概念。
#
# **株数のタグを混ぜてはいけない。** `dei:EntityCommonStockSharesOutstanding` は
# 決算期末ではなく 10-K の表紙の日付で報告されるため、これを基準日にすると
# 決算期末が窓から外れて売上が丸ごと落ちる（Apple で実際に踏んだ）。
_ANCHOR_CONCEPTS: frozenset[str] = frozenset(
    tag for priority in (*_PERIOD_METRICS.values(), *_POINT_METRICS.values()) for tag in priority
)


def normalize_company(cik: int, observations: Iterable[Observation]) -> list[Fundamentals]:
    """1社分を会計年度ごとの行にする。新しい年度から順に返す。"""
    rows = list(observations)
    if not rows:
        return []

    anchors = fiscal_year_ends(
        o.period_end for o in rows if o.concept in _ANCHOR_CONCEPTS and o.value is not None
    )
    out: list[Fundamentals] = []
    for anchor in anchors:
        picked: dict[str, Observation] = {}
        for name, priority in {**_PERIOD_METRICS, **_POINT_METRICS}.items():
            found = pick(rows, priority, anchor)
            if found is not None:
                picked[name] = found

        shares = pick(
            rows,
            _SHARE_PRIORITY,
            anchor,
            preferred_unit="shares",
            tolerance_days=SHARE_TOLERANCE_DAYS,
        )

        # 粗利は GrossProfit が無ければ 売上 − 原価 で導出する。
        # どちらも無ければ None のままにする（実測で 28.8% がこれに当たる）。
        gross = picked.get("gross_profit")
        gross_value = gross.value if gross else None
        source_gross = gross.concept if gross else None
        if gross_value is None:
            revenue_obs = picked.get("revenue")
            cost = pick(rows, concepts.COST_OF_REVENUE, anchor)
            if revenue_obs is not None and cost is not None:
                gross_value = revenue_obs.value - cost.value  # type: ignore[operator]
                source_gross = f"{revenue_obs.concept}-{cost.concept}"

        # 期間・出典は売上を基準にする。売上が無ければ他の期間値で代用する。
        basis = picked.get("revenue") or next(
            (picked[name] for name in _PERIOD_METRICS if name in picked), None
        )
        currency = next(
            (picked[name].unit for name in (*_PERIOD_METRICS, *_POINT_METRICS) if name in picked),
            None,
        )

        source = {name: o.concept for name, o in picked.items()}
        if source_gross:
            source["gross_profit"] = source_gross
        if shares is not None:
            source["shares_outstanding"] = shares.concept

        out.append(
            Fundamentals(
                cik=cik,
                fiscal_year=fiscal_year_of(anchor),
                period_start=basis.period_start if basis else None,
                period_end=anchor,
                period_days=basis.days if basis else None,
                revenue=_value(picked, "revenue"),
                gross_profit=gross_value,
                operating_income=_value(picked, "operating_income"),
                net_income=_value(picked, "net_income"),
                total_assets=_value(picked, "total_assets"),
                equity=_value(picked, "equity"),
                current_assets=_value(picked, "current_assets"),
                current_liabilities=_value(picked, "current_liabilities"),
                cfo=_value(picked, "cfo"),
                capex=_value(picked, "capex"),
                shares_outstanding=shares.value if shares else None,
                currency=currency,
                accn=basis.accn if basis else None,
                filed_at=basis.filed_at if basis else None,
                source_concepts=source,
            )
        )
    return out


def _value(picked: dict[str, Observation], name: str) -> float | None:
    found = picked.get(name)
    return found.value if found else None


# --- DuckDB との受け渡し ----------------------------------------------------

_SELECT = (
    "SELECT cik, concept, unit, period_start, period_end, value, accn, filed_at "
    "FROM facts_annual ORDER BY cik"
)


_FETCH = 100_000


def iter_by_company(
    con: duckdb.DuckDBPyConnection,
) -> Iterator[tuple[int, list[Observation]]]:
    """`facts_annual` を cik 順に読み、1社ぶんずつ返す。

    全行を一度に Python に載せるとピークメモリが 1.4GB になる。cik で並べて
    おけば境目で切り出せるので、1社ぶんだけ持てばよい。
    """
    con.execute(_SELECT)
    current: int | None = None
    buffer: list[Observation] = []
    while chunk := con.fetchmany(_FETCH):
        for cik, concept, unit, start, end, value, accn, filed in chunk:
            cik = int(cik)
            if current is not None and cik != current:
                yield current, buffer
                buffer = []
            current = cik
            buffer.append(
                Observation(
                    concept=concept,
                    unit=unit,
                    period_start=start,
                    period_end=end,
                    value=value,
                    accn=accn,
                    filed_at=filed,
                )
            )
    if current is not None:
        yield current, buffer


def normalize_universe(con: duckdb.DuckDBPyConnection) -> int:
    """`facts_annual` を読んで `fundamentals` を作り直す。"""
    produced = [
        row
        for cik, observations in iter_by_company(con)
        for row in normalize_company(cik, observations)
    ]
    return store_fundamentals(con, produced)


_COLUMNS = (
    "cik",
    "fiscal_year",
    "period_start",
    "period_end",
    "period_days",
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "total_assets",
    "equity",
    "current_assets",
    "current_liabilities",
    "cfo",
    "capex",
    "shares_outstanding",
    "currency",
    "accn",
    "filed_at",
    "source_concepts",
)


def store_fundamentals(con: duckdb.DuckDBPyConnection, rows: Iterable[Fundamentals]) -> int:
    """`fundamentals` を入れ替える。

    `fundamentals` に market 列は無い。CIK は SEC 固有なので、日本株を足すときは
    別のキー体系になる。そのときに分け方を決める。
    """
    payload = [
        (
            row.cik,
            row.fiscal_year,
            row.period_start,
            row.period_end,
            row.period_days,
            row.revenue,
            row.gross_profit,
            row.operating_income,
            row.net_income,
            row.total_assets,
            row.equity,
            row.current_assets,
            row.current_liabilities,
            row.cfo,
            row.capex,
            row.shares_outstanding,
            row.currency,
            row.accn,
            row.filed_at,
            json.dumps(row.source_concepts, sort_keys=True),
        )
        for row in rows
    ]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM fundamentals")
        written = bulk_insert(con, "fundamentals", _COLUMNS, payload)
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return written
