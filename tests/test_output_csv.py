"""候補 CSV。評価スキルへの受け渡し口なので、列と値の対応を固定する。"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from stock_radar.config import Criteria, Market, load_criteria
from stock_radar.metrics.fundamentals import FundamentalMetrics
from stock_radar.output.csv_writer import COLUMNS, csv_path_for, write_candidates
from stock_radar.screen.evaluate import evaluate
from stock_radar.screen.filters import Candidate
from tests.test_screen_logic import CRITERIA_PATH, TRACK_B, make

AS_OF = dt.datetime(2026, 9, 21, 12, 30, 0)


@pytest.fixture(scope="module")
def criteria() -> Criteria:
    return load_criteria(CRITERIA_PATH)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path: Path, criteria: Criteria, **kwargs: object) -> list[dict[str, str]]:
    result = evaluate(make(), criteria)
    write_candidates(
        path,
        [result],
        market=Market.US,
        data_source="sec_companyfacts+yfinance",
        as_of=AS_OF,
        **kwargs,  # type: ignore[arg-type]
    )
    return read(path)


def test_path_is_dated_and_market_scoped() -> None:
    path = csv_path_for("output", market=Market.US, as_of=dt.date(2026, 9, 21))
    assert path == Path("output/2026-09-21_us.csv")


def test_columns_start_with_the_skill_interface(tmp_path: Path, criteria: Criteria) -> None:
    """先頭は docs/skill-integration.md のたたき台の順。そのまま読み込める。"""
    rows = write(tmp_path / "out.csv", criteria)
    assert list(rows[0])[:6] == [
        "ticker",
        "market",
        "name",
        "track",
        "passed_filters",
        "timing_score",
    ]
    assert set(rows[0]) == set(COLUMNS)


def test_values_match_the_judgement(tmp_path: Path, criteria: Criteria) -> None:
    row = write(tmp_path / "out.csv", criteria)[0]
    assert row["ticker"] == "AAA"
    assert row["track"] == "A"
    assert row["market_cap"] == "1000000000.0"
    assert row["pbr"] == "2.0"
    assert row["timing_score"] == "2.0"
    # 判定に使わなかった指標も手がかりとして載せる。
    assert row["revenue_growth_yoy"] == "0.05"


def test_carries_reference_values_not_used_in_screening(tmp_path: Path, criteria: Criteria) -> None:
    """判定に使わない参考値も載せる。取れなければ空欄（0 で埋めない）。"""
    result = evaluate(make(gross_margin_change_3y=-0.099), criteria)
    write_candidates(
        tmp_path / "out.csv",
        [result],
        market=Market.US,
        data_source="sec_companyfacts+yfinance",
        as_of=AS_OF,
    )
    assert read(tmp_path / "out.csv")[0]["gross_margin_change_3y"] == "-0.099"
    assert write(tmp_path / "blank.csv", criteria)[0]["gross_margin_change_3y"] == ""


def test_passed_filters_fit_in_one_cell(tmp_path: Path, criteria: Criteria) -> None:
    """カンマだと CSV の区切りと紛らわしいのでセミコロンで繋ぐ。"""
    row = write(tmp_path / "out.csv", criteria)[0]
    filters = row["passed_filters"].split(";")
    assert filters[0] == "track_a"
    assert "track_a.pbr" in filters
    assert "," not in row["passed_filters"]


def test_records_sources_and_as_of_dates(tmp_path: Path, criteria: Criteria) -> None:
    """CLAUDE.md「数値の出典を記録する」。決算期・提出日・株価日付・取得日。"""
    row = write(tmp_path / "out.csv", criteria)[0]
    assert row["fundamentals_fiscal_year"] == "2025"
    assert row["fundamentals_period_end"] == "2025-12-31"
    assert row["price_as_of"] == "2026-09-21"
    assert row["data_source"] == "sec_companyfacts+yfinance"
    assert row["as_of"] == "2026-09-21 12:30:00"


def test_flags_approximate_market_cap(tmp_path: Path, criteria: Criteria) -> None:
    """複数クラス株の時価総額は全クラス合計の株数 × 片方のクラスの株価。"""
    plain = write(tmp_path / "plain.csv", criteria)[0]
    assert plain["market_cap_is_approximate"] == "False"
    flagged = write(tmp_path / "flagged.csv", criteria, approximate_tickers=["AAA"])[0]
    assert flagged["market_cap_is_approximate"] == "True"


def test_carries_data_quality_warnings(tmp_path: Path, criteria: Criteria) -> None:
    """手がかりが怪しいことを受け手に伝える。"""
    base = evaluate(make(**TRACK_B, revenue=200_000_000.0), criteria).candidate
    broken = FundamentalMetrics(
        cik=1,
        fiscal_year=2025,
        period_end=dt.date(2025, 12, 31),
        warnings=("自己資本比率が1を超えている",),
    )
    candidate = Candidate(
        market=Market.US,
        ticker="AAA",
        cik=1,
        name="Alpha",
        fundamentals=base.fundamentals,
        metrics=broken,
        quotes=base.quotes,
    )
    path = tmp_path / "warn.csv"
    write_candidates(
        path,
        [evaluate(candidate, criteria)],
        market=Market.US,
        data_source="sec_companyfacts+yfinance",
        as_of=AS_OF,
    )
    assert read(path)[0]["data_quality_warnings"] == "自己資本比率が1を超えている"


def test_writes_a_header_only_file_when_nothing_passes(tmp_path: Path) -> None:
    """「候補が無かった run」と「出力に失敗した run」をファイルの有無で区別する。"""
    path = tmp_path / "empty.csv"
    written = write_candidates(
        path, [], market=Market.US, data_source="sec_companyfacts", as_of=AS_OF
    )
    assert written == 0
    assert path.exists()
    assert read(path) == []
    assert path.read_text(encoding="utf-8").splitlines()[0].startswith("ticker,market,name")


def test_creates_the_output_directory(tmp_path: Path, criteria: Criteria) -> None:
    rows = write(tmp_path / "nested" / "deep" / "out.csv", criteria)
    assert len(rows) == 1
