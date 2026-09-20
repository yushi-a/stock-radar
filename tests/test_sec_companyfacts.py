"""companyfacts.zip の取り込み。

フィクスチャは実物から `concepts.py` が参照する概念だけを抜いた5社分。
zip はテスト中に組み立てる。
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from pathlib import Path

import duckdb
import pytest

from stock_radar.config import Market, load_runtime
from stock_radar.sources.sec import concepts
from stock_radar.sources.sec.client import Response, SecError
from stock_radar.sources.sec.companyfacts import (
    ANNUAL_MAX_DAYS,
    ANNUAL_MIN_DAYS,
    COMPANYFACTS_ZIP_URL,
    Fact,
    download_companyfacts,
    iter_company_facts,
    latest_companyfacts,
    parse_company_facts,
    period_kind,
    prune_companyfacts,
    store_facts,
    wanted_ciks,
)
from stock_radar.sources.sec.universe import TickerRow, replace_ticker_rows
from tests.helpers import FakeTransport, make_client

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sec" / "companyfacts"
CIKS = {"aapl": 320193, "googl": 1652044, "deere": 315189, "dal": 27904, "shph": 1757499}


@pytest.fixture
def zip_path(tmp_path: Path) -> Path:
    path = tmp_path / "companyfacts_2026-09-19.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for slug, cik in CIKS.items():
            archive.writestr(
                f"CIK{cik:010d}.json", (FIXTURES / f"{slug}.json").read_text(encoding="utf-8")
            )
        # 表紙以外のエントリが紛れていても壊れないこと。
        archive.writestr("metadata.json", "{}")
    return path


def seed_universe(con: duckdb.DuckDBPyConnection, ciks: list[int]) -> None:
    replace_ticker_rows(con, [TickerRow(cik, f"name {cik}", f"T{cik}", "Nasdaq") for cik in ciks])


def facts_of(zip_path: Path, slug: str) -> tuple[list[Fact], list[Fact]]:
    payload = json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))
    return parse_company_facts(payload)


# --- 期間の粒度 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # 時点値（BS・株数）。実測で BS 項目に start は付かない。
        (None, dt.date(2024, 9, 28), "annual"),
        # 52週決算 364日 / 53週決算 371日
        (dt.date(2023, 10, 1), dt.date(2024, 9, 28), "annual"),
        (dt.date(2022, 9, 25), dt.date(2023, 9, 30), "annual"),
        (dt.date(2024, 1, 1), dt.date(2024, 12, 31), "annual"),
        # 四半期
        (dt.date(2024, 7, 1), dt.date(2024, 9, 28), "quarterly"),
        # 半期・9ヶ月は捨てる（10-K に比較用として載っている）
        (dt.date(2024, 1, 1), dt.date(2024, 6, 30), None),
        (dt.date(2024, 1, 1), dt.date(2024, 9, 30), None),
    ],
)
def test_period_kind(start: dt.date | None, end: dt.date, expected: str | None) -> None:
    assert period_kind(start, end) is expected


def test_annual_window_matches_the_measured_distribution() -> None:
    """実測の期間長は 364 / 365 / 366 / 371 日だけだった。"""
    assert ANNUAL_MIN_DAYS <= 364 <= ANNUAL_MAX_DAYS
    assert ANNUAL_MIN_DAYS <= 371 <= ANNUAL_MAX_DAYS
    assert not ANNUAL_MIN_DAYS <= 273 <= ANNUAL_MAX_DAYS


# --- パース -----------------------------------------------------------------


def test_only_wanted_concepts_are_kept(zip_path: Path) -> None:
    """us-gaap には1社500超の概念がある。concepts.py の分だけ入れる。"""
    annual, quarterly = facts_of(zip_path, "aapl")
    known = (
        set(concepts.REVENUE)
        | set(concepts.GROSS_PROFIT)
        | set(concepts.COST_OF_REVENUE)
        | set(concepts.OPERATING_INCOME)
        | set(concepts.NET_INCOME)
        | set(concepts.OPERATING_CASH_FLOW)
        | set(concepts.CAPEX)
        | set(concepts.TOTAL_ASSETS)
        | set(concepts.CURRENT_ASSETS)
        | set(concepts.CURRENT_LIABILITIES)
        | set(concepts.EQUITY)
        | {tag for _, tag in concepts.SHARES_OUTSTANDING}
    )
    assert {f.concept for f in annual + quarterly} <= known


def test_quarterly_holds_revenue_only(zip_path: Path) -> None:
    _, quarterly = facts_of(zip_path, "aapl")
    assert quarterly
    assert {f.concept for f in quarterly} <= set(concepts.REVENUE)


def test_fiscal_year_comes_from_the_period_not_the_filing(zip_path: Path) -> None:
    """A-1：SEC の `fy` は提出書類の年度で 66% ずれる。使わない。"""
    annual, _ = facts_of(zip_path, "aapl")
    assert all(f.fiscal_year == f.period_end.year for f in annual)


def test_balance_sheet_facts_have_no_start(zip_path: Path) -> None:
    annual, _ = facts_of(zip_path, "aapl")
    assets = [f for f in annual if f.concept == "Assets"]
    assert assets
    assert all(f.period_start is None for f in assets)


def test_annual_periods_are_within_the_window(zip_path: Path) -> None:
    annual, _ = facts_of(zip_path, "aapl")
    for fact in annual:
        if fact.period_start is None:
            continue
        days = (fact.period_end - fact.period_start).days + 1
        assert ANNUAL_MIN_DAYS <= days <= ANNUAL_MAX_DAYS


def test_multi_class_issuer_yields_share_facts(zip_path: Path) -> None:
    """C：dei のタグが無くても株数が取れること。"""
    annual, _ = facts_of(zip_path, "googl")
    tags = {f.concept for f in annual}
    assert "EntityCommonStockSharesOutstanding" not in tags
    assert "WeightedAverageNumberOfDilutedSharesOutstanding" in tags


def test_company_without_revenue_does_not_fail(zip_path: Path) -> None:
    annual, quarterly = facts_of(zip_path, "shph")
    assert not [f for f in annual if f.concept in concepts.REVENUE]
    assert quarterly == []


@pytest.mark.parametrize("broken", [[], "x", {"facts": {}}])
def test_unexpected_shapes_fail_loudly(broken: object) -> None:
    with pytest.raises(SecError):
        parse_company_facts(broken)


def test_iter_filters_by_wanted_cik(zip_path: Path) -> None:
    got = list(iter_company_facts(zip_path, wanted={CIKS["dal"]}))
    assert len(got) == 1
    annual, _ = got[0]
    assert {f.cik for f in annual} == {CIKS["dal"]}


# --- DuckDB への書き込み ----------------------------------------------------


def test_store_writes_both_tables(con: duckdb.DuckDBPyConnection, zip_path: Path) -> None:
    seed_universe(con, list(CIKS.values()))
    keep = wanted_ciks(con)
    annual, quarterly = store_facts(con, iter_company_facts(zip_path, wanted=keep))

    assert annual > 0
    assert quarterly > 0
    assert con.execute("SELECT count(*) FROM facts_annual").fetchone()[0] == annual
    assert con.execute("SELECT count(*) FROM facts_quarterly").fetchone()[0] == quarterly


def test_store_is_idempotent(con: duckdb.DuckDBPyConnection, zip_path: Path) -> None:
    seed_universe(con, list(CIKS.values()))
    keep = wanted_ciks(con)
    first = store_facts(con, iter_company_facts(zip_path, wanted=keep))
    second = store_facts(con, iter_company_facts(zip_path, wanted=keep))
    assert first == second
    assert con.execute("SELECT count(*) FROM facts_annual").fetchone()[0] == first[0]


def test_store_drops_companies_no_longer_in_the_universe(
    con: duckdb.DuckDBPyConnection, zip_path: Path
) -> None:
    seed_universe(con, list(CIKS.values()))
    store_facts(con, iter_company_facts(zip_path, wanted=wanted_ciks(con)))

    seed_universe(con, [CIKS["dal"]])
    store_facts(con, iter_company_facts(zip_path, wanted=wanted_ciks(con)))
    remaining = {row[0] for row in con.execute("SELECT DISTINCT cik FROM facts_annual").fetchall()}
    assert remaining == {CIKS["dal"]}


def test_store_keeps_two_periods_sharing_an_end_and_filing(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """同じ提出書類が period_start 違いで報告してきても両方残ること。

    Coeur Mining の2011年度は同じ10-K に 356日と 365日が併記されていて
    値が4倍違う。片方を落とすと売上を取り違える。
    """
    seed_universe(con, [215466])
    facts = [
        Fact(
            215466,
            "Revenues",
            "USD",
            2011,
            "FY",
            dt.date(2011, 1, 1),
            dt.date(2011, 12, 31),
            1_021_200_000.0,
            "0001193125-12-075636",
            dt.date(2012, 2, 23),
        ),
        Fact(
            215466,
            "Revenues",
            "USD",
            2011,
            "FY",
            dt.date(2011, 1, 10),
            dt.date(2011, 12, 31),
            246_911_000.0,
            "0001193125-12-075636",
            dt.date(2012, 2, 23),
        ),
    ]
    store_facts(con, [(facts, [])])
    values = {row[0] for row in con.execute("SELECT value FROM facts_annual").fetchall()}
    assert values == {1_021_200_000.0, 246_911_000.0}


def test_store_writes_nulls_not_empty_strings(con: duckdb.DuckDBPyConnection) -> None:
    """CSV 経由で入れているので、欠損が空文字になっていないこと。"""
    seed_universe(con, [1])
    facts = [Fact(1, "Assets", "USD", 2024, None, None, dt.date(2024, 12, 31), None, "a", None)]
    store_facts(con, [(facts, [])])
    row = con.execute(
        "SELECT period_start IS NULL, value IS NULL, filed_at IS NULL FROM facts_annual"
    ).fetchone()
    assert row == (True, True, True)


def test_store_only_keeps_universe_members(con: duckdb.DuckDBPyConnection, zip_path: Path) -> None:
    seed_universe(con, [CIKS["aapl"]])
    annual, _ = store_facts(con, iter_company_facts(zip_path))
    assert annual > 0
    assert {row[0] for row in con.execute("SELECT DISTINCT cik FROM facts_annual").fetchall()} == {
        CIKS["aapl"]
    }


def test_wanted_ciks_skips_excluded_companies(con: duckdb.DuckDBPyConnection) -> None:
    replace_ticker_rows(
        con,
        [
            TickerRow(1, "keep", "KEEP", "Nasdaq"),
            TickerRow(2, "drop", "DROP", "OTC"),
        ],
    )
    assert wanted_ciks(con) == {1}


def test_wanted_ciks_is_market_scoped(con: duckdb.DuckDBPyConnection) -> None:
    seed_universe(con, [1])
    assert wanted_ciks(con, market=Market.JP) == set()


# --- 世代管理 ---------------------------------------------------------------


def test_latest_companyfacts_picks_the_newest(tmp_path: Path) -> None:
    for name in ("companyfacts_2026-09-12.zip", "companyfacts_2026-09-19.zip", "other.zip"):
        (tmp_path / name).write_bytes(b"x")
    path, stamp = latest_companyfacts(tmp_path)
    assert path.name == "companyfacts_2026-09-19.zip"
    assert stamp == dt.date(2026, 9, 19)


def test_latest_companyfacts_handles_an_empty_directory(tmp_path: Path) -> None:
    assert latest_companyfacts(tmp_path) is None
    assert latest_companyfacts(tmp_path / "missing") is None


def test_prune_keeps_only_the_newest_generations(tmp_path: Path) -> None:
    """1ファイル1.4GB あるので溜めない。"""
    for name in (
        "companyfacts_2026-09-05.zip",
        "companyfacts_2026-09-12.zip",
        "companyfacts_2026-09-19.zip",
    ):
        (tmp_path / name).write_bytes(b"x")
    removed = prune_companyfacts(tmp_path, keep=1)
    assert {p.name for p in removed} == {
        "companyfacts_2026-09-05.zip",
        "companyfacts_2026-09-12.zip",
    }
    assert [p.name for p in tmp_path.glob("companyfacts_*.zip")] == ["companyfacts_2026-09-19.zip"]


def test_download_reuses_a_recent_file(tmp_path: Path) -> None:
    runtime = load_runtime(REPO_ROOT / "config" / "runtime.yaml").sec
    sec = runtime.model_copy(update={"raw_dir": tmp_path, "companyfacts_max_age_days": 6})
    (tmp_path / "companyfacts_2026-09-19.zip").write_bytes(b"x")
    transport = FakeTransport([])

    path = download_companyfacts(make_client(transport), sec, today=dt.date(2026, 9, 21))

    assert path.name == "companyfacts_2026-09-19.zip"
    assert transport.requests == []


def test_download_refetches_a_stale_file(tmp_path: Path) -> None:
    runtime = load_runtime(REPO_ROOT / "config" / "runtime.yaml").sec
    sec = runtime.model_copy(update={"raw_dir": tmp_path, "companyfacts_max_age_days": 6})
    (tmp_path / "companyfacts_2026-09-01.zip").write_bytes(b"x")
    transport = FakeTransport([Response(200, b"zip")])

    path = download_companyfacts(make_client(transport), sec, today=dt.date(2026, 9, 21))

    assert transport.requests[0][0] == COMPANYFACTS_ZIP_URL
    assert path.name == "companyfacts_2026-09-21.zip"
    # 古い世代は消える。
    assert not (tmp_path / "companyfacts_2026-09-01.zip").exists()


def test_download_force_ignores_freshness(tmp_path: Path) -> None:
    runtime = load_runtime(REPO_ROOT / "config" / "runtime.yaml").sec
    sec = runtime.model_copy(update={"raw_dir": tmp_path})
    (tmp_path / "companyfacts_2026-09-19.zip").write_bytes(b"x")
    transport = FakeTransport([Response(200, b"zip")])

    download_companyfacts(make_client(transport), sec, today=dt.date(2026, 9, 21), force=True)
    assert transport.requests
