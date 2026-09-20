"""docs/xbrl-findings.md の結論を、フィクスチャに対して固定する。

Phase 2a の成果物は調査ノートだが、文章だけにしておくと実装が進むうちに
前提が崩れても気づけない。ここで実データの性質そのものを assert しておくと、
Phase 2b の正規化がどの観測に依存しているかがコードから辿れる。

フィクスチャは実物の companyfacts から concepts.py が参照する概念だけを
抜いたもの（SEC データに再配布制限は無い。docs/architecture.md）。
"""

from __future__ import annotations

import datetime as dt
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from stock_radar.sources.sec import concepts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec" / "companyfacts"


def load(slug: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


def entries(facts: dict, taxonomy: str, concept: str) -> list[dict]:
    node = facts["facts"].get(taxonomy, {}).get(concept)
    if not node:
        return []
    return [{**e, "unit": unit} for unit, es in node["units"].items() for e in es]


def span_days(entry: dict) -> int | None:
    if not entry.get("start"):
        return None
    start = dt.date.fromisoformat(entry["start"])
    end = dt.date.fromisoformat(entry["end"])
    return (end - start).days + 1


def annual(facts: dict, concept: str) -> list[dict]:
    """A-4 の年次判定：10-K 系かつ期間長 350〜380日。"""
    return [
        e
        for e in entries(facts, "us-gaap", concept)
        if e["unit"] == "USD"
        and e.get("form", "").startswith("10-K")
        and (span_days(e) or 0) in range(350, 381)
    ]


# --- concepts.py の体裁 -----------------------------------------------------


@pytest.mark.parametrize(
    "priority",
    [
        concepts.REVENUE,
        concepts.COST_OF_REVENUE,
        concepts.CAPEX,
        concepts.OPERATING_CASH_FLOW,
        concepts.EQUITY,
    ],
)
def test_priority_lists_have_no_duplicates(priority: list[str]) -> None:
    assert len(priority) == len(set(priority))


def test_shares_priority_does_not_lead_with_the_dei_tag() -> None:
    """C：dei のタグは複数クラス株で丸ごと欠落するので先頭に置かない。"""
    first_taxonomy, first_tag = concepts.SHARES_OUTSTANDING[0]
    assert (first_taxonomy, first_tag) != ("dei", "EntityCommonStockSharesOutstanding")
    assert first_taxonomy == "us-gaap"


# --- A: 年次レコードの選定 --------------------------------------------------


def test_fy_is_the_filings_year_not_the_periods_year() -> None:
    """A-1：`fy` で束ねてはいけないこと。"""
    facts = load("aapl")
    rows = annual(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    mismatched = [e for e in rows if e["fy"] != int(e["end"][:4])]
    assert mismatched, "fy と期間末の年がずれるエントリが1つも無い"


def test_the_same_period_appears_more_than_once() -> None:
    """A-2：元の 10-K と翌年以降の比較欄で再掲される。"""
    facts = load("aapl")
    rows = annual(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    seen: dict[tuple[str, str], int] = {}
    for e in rows:
        seen[(e["start"], e["end"])] = seen.get((e["start"], e["end"]), 0) + 1
    assert max(seen.values()) >= 2


def test_restatements_change_the_value_for_the_same_period() -> None:
    """A-3：同じ期間で値が食い違う。最新の filed を採る根拠。"""
    facts = load("aapl")
    by_period: dict[tuple[str, str], set[float]] = {}
    for e in annual(facts, "SalesRevenueNet"):
        by_period.setdefault((e["start"], e["end"]), set()).add(e["val"])
    assert any(len(vals) > 1 for vals in by_period.values())


def test_ten_k_filings_also_contain_quarterly_periods() -> None:
    """A-4：フォームが 10-K かどうかだけでは年次を選べない。"""
    facts = load("aapl")
    lengths = {
        span_days(e)
        for e in entries(facts, "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
        if e.get("form", "").startswith("10-K") and span_days(e)
    }
    assert any(n < 200 for n in lengths), "10-K に四半期の期間が入っていない"
    assert any(350 <= n <= 380 for n in lengths)


def test_annual_periods_are_never_exactly_365_days_for_a_52_week_filer() -> None:
    """A-4：52/53週決算は 364 / 371 日になる。"""
    lengths = {
        span_days(e)
        for e in annual(load("aapl"), "RevenueFromContractWithCustomerExcludingAssessedTax")
    }
    assert lengths <= set(range(350, 381))
    assert 364 in lengths


def test_one_fiscal_year_can_appear_with_period_ends_a_day_apart() -> None:
    """A-6：期末そのものをキーにすると1年度が2行になる。"""
    ends = sorted({dt.date.fromisoformat(e["end"]) for e in annual(load("deere"), "Revenues")})
    gaps = [(b - a).days for a, b in pairwise(ends)]
    assert any(gap <= 14 for gap in gaps), "1日違いの期末が再現できていない"


def test_balance_sheet_entries_never_carry_a_start() -> None:
    """A-8：start の有無で時点値と期間値を区別できる。"""
    for slug in ("aapl", "deere", "dal"):
        assert all(not e.get("start") for e in entries(load(slug), "us-gaap", "Assets"))


# --- B: 四半期 --------------------------------------------------------------


def test_three_month_values_are_reported_directly() -> None:
    """B：YTD の引き算は要らない。設計の前提が違っていた箇所。"""
    facts = load("aapl")
    rows = [
        e
        for e in entries(facts, "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
        if e["unit"] == "USD" and e.get("form", "").startswith("10-Q")
    ]
    three_month = [e for e in rows if (span_days(e) or 0) in range(80, 101)]
    ytd = [e for e in rows if (span_days(e) or 0) in range(170, 286)]
    assert three_month, "3ヶ月値が直接取れていない"
    assert ytd, "YTD も併記されているはず"


# --- C: 複数クラス株 --------------------------------------------------------


def test_companyfacts_has_no_dimension_keys() -> None:
    """C：軸付きファクトが含まれないという仮説の検証。"""
    known = {"start", "end", "val", "accn", "fy", "fp", "form", "filed", "frame"}
    facts = load("googl")
    keys: set[str] = set()
    for taxonomy in facts["facts"].values():
        for concept in taxonomy.values():
            for unit_entries in concept["units"].values():
                for entry in unit_entries:
                    keys |= set(entry)
    assert keys <= known, f"軸を表すキーが現れた: {keys - known}"


def test_multi_class_issuer_is_missing_the_dei_share_count() -> None:
    """C：だから dei のタグを株数の第1候補にできない。"""
    assert not entries(load("googl"), "dei", "EntityCommonStockSharesOutstanding")
    # 単一クラスの企業では取れる。
    assert entries(load("aapl"), "dei", "EntityCommonStockSharesOutstanding")


def test_multi_class_issuer_still_has_a_total_share_count() -> None:
    """C：代替タグは軸なしの全クラス合計で報告される。"""
    rows = entries(load("googl"), "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")
    assert rows
    latest = max(rows, key=lambda e: (e["end"], e["filed"]))
    # Alphabet の発行済株式は全クラス合計で約120億株。片方のクラスだけなら半分以下になる。
    assert latest["val"] > 10_000_000_000


# --- D: 欠損 ----------------------------------------------------------------


def test_some_industries_report_no_gross_profit() -> None:
    """D：粗利が 28.8% で算出できない件の代表例（航空）。"""
    dal = load("dal")
    assert not annual(dal, "GrossProfit")
    assert not any(annual(dal, tag) for tag in concepts.COST_OF_REVENUE)
    # 売上と営業利益は取れる。粗利だけが無い。
    assert any(annual(dal, tag) for tag in concepts.REVENUE)
    assert annual(dal, "OperatingIncomeLoss")


def test_a_pre_revenue_company_has_no_revenue_tag() -> None:
    """D：売上欠損 270社の代表例（臨床段階）。revenue_positive で落ちる。"""
    shph = load("shph")
    assert not any(annual(shph, tag) for tag in concepts.REVENUE)
