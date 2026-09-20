"""submissions.zip の解釈と、それによるユニバースの確定。

フィクスチャは実物の ``submissions.zip`` から6社分を抜き、読む項目だけに絞ったもの。
zip はテスト中に組み立てる（バイナリをコミットするとレビューできないため）。

含めてある観点：10-K を出す事業会社 / 銀行 / REIT / SPAC / 20-F しか出さない ADR /
年次報告を出さないクローズドエンドファンド / 読み飛ばすべきシャード。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import duckdb
import pytest

from stock_radar.config import Market
from stock_radar.sources.sec.client import Response, SecError
from stock_radar.sources.sec.submissions import (
    SUBMISSIONS_ZIP_URL,
    SubmissionProfile,
    annual_report_form,
    apply_submission_profiles,
    download_submissions,
    is_profile_entry,
    iter_submission_profiles,
    mark_missing_submissions,
    parse_submission_profile,
    submission_exclusion,
)
from stock_radar.sources.sec.universe import ExclusionReason, TickerRow, replace_ticker_rows
from tests.helpers import FakeTransport, make_client

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sec" / "submissions_sample.json"


@pytest.fixture
def entries() -> dict[str, dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def zip_path(tmp_path: Path, entries: dict[str, dict]) -> Path:
    path = tmp_path / "submissions.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, json.dumps(payload))
    return path


def profile_by_label(zip_path: Path, entries: dict[str, dict], label: str) -> SubmissionProfile:
    cik = next(int(e["cik"]) for e in entries.values() if e.get("_label") == label)
    return next(p for p in iter_submission_profiles(zip_path) if p.cik == cik)


# --- エントリの選別 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CIK0000320193.json", True),
        ("submissions/CIK0000320193.json", True),
        # 古い提出履歴のシャード。表紙情報を持たないので読み飛ばす。
        ("CIK0000320193-submissions-001.json", False),
        ("metadata.json", False),
        ("CIK320193.json", False),
    ],
)
def test_is_profile_entry(name: str, expected: bool) -> None:
    assert is_profile_entry(name) is expected


def test_shards_are_skipped(zip_path: Path, entries: dict[str, dict]) -> None:
    """シャードを読むと表紙情報が無くて壊れる。件数で確かめる。"""
    profiles = list(iter_submission_profiles(zip_path))
    shards = [name for name in entries if "-submissions-" in name]
    assert shards, "フィクスチャにシャードが入っていない"
    assert len(profiles) == len(entries) - len(shards)


def test_wanted_ciks_limits_what_is_parsed(zip_path: Path, entries: dict[str, dict]) -> None:
    apple = next(int(e["cik"]) for e in entries.values() if e.get("_label") == "operating_10k")
    profiles = list(iter_submission_profiles(zip_path, wanted_ciks={apple}))
    assert [p.cik for p in profiles] == [apple]


# --- 年次報告フォームの判定 -------------------------------------------------


@pytest.mark.parametrize(
    ("forms", "expected"),
    [
        (["4", "8-K", "10-K"], "10-K"),
        (["10-K/A"], "10-K/A"),
        # 旧様式も 10-K 系として扱う。
        (["10-K405"], "10-K405"),
        (["10-KT"], "10-KT"),
        # 10-K と 20-F の両方があれば 10-K 提出企業。
        (["20-F", "10-K"], "10-K"),
        (["6-K", "20-F"], "20-F"),
        (["40-F"], "40-F"),
        (["N-CSR", "N-PX"], None),
        ([], None),
        # 提出遅延の届出であって年次報告ではない。
        (["NT 10-K"], None),
        ([None, 4, "10-K"], "10-K"),
    ],
)
def test_annual_report_form(forms: list, expected: str | None) -> None:
    assert annual_report_form(forms) == expected


# --- 1社分のパース ----------------------------------------------------------


def test_parses_an_operating_company(zip_path: Path, entries: dict[str, dict]) -> None:
    profile = profile_by_label(zip_path, entries, "operating_10k")
    assert profile.sic == "3571"
    assert profile.entity_type == "operating"
    assert profile.files_10k


def test_missing_sic_becomes_none(zip_path: Path, entries: dict[str, dict]) -> None:
    profile = profile_by_label(zip_path, entries, "closed_end_fund")
    assert profile.sic is None
    assert profile.annual_form is None
    assert not profile.files_10k


@pytest.mark.parametrize("broken", [[], "x", {"name": "no cik"}])
def test_unexpected_shapes_fail_loudly(broken: object) -> None:
    with pytest.raises(SecError):
        parse_submission_profile(broken)


def test_missing_filings_section_is_tolerated() -> None:
    """提出履歴が無い会社でも落ちないこと。年次報告なしとして扱う。"""
    profile = parse_submission_profile({"cik": "0000000001", "sic": "1234"})
    assert profile.annual_form is None


# --- 除外判定 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("operating_10k", None),
        # SIC 6000番台は金融・保険・不動産。10-K を出していても除外する。
        ("bank", ExclusionReason.FINANCIAL_SIC),
        ("reit", ExclusionReason.FINANCIAL_SIC),
        ("spac", ExclusionReason.FINANCIAL_SIC),
        # SIC は非金融だが 20-F しか出していない。これが ADR。
        ("adr_20f", ExclusionReason.NO_10K),
        ("closed_end_fund", ExclusionReason.NO_10K),
    ],
)
def test_submission_exclusion(
    zip_path: Path, entries: dict[str, dict], label: str, expected: ExclusionReason | None
) -> None:
    assert submission_exclusion(profile_by_label(zip_path, entries, label)) is expected


@pytest.mark.parametrize(
    ("sic", "excluded"),
    [("5999", False), ("6000", True), ("6770", True), ("6999", True), ("7000", False)],
)
def test_financial_sic_boundaries(sic: str, excluded: bool) -> None:
    profile = SubmissionProfile(cik=1, sic=sic, entity_type="operating", annual_form="10-K")
    assert (submission_exclusion(profile) is ExclusionReason.FINANCIAL_SIC) is excluded


def test_non_numeric_sic_does_not_crash() -> None:
    profile = SubmissionProfile(cik=1, sic="N/A", entity_type=None, annual_form="10-K")
    assert submission_exclusion(profile) is None


# --- universe への反映 ------------------------------------------------------


def _seed(con: duckdb.DuckDBPyConnection, ciks: list[int]) -> None:
    replace_ticker_rows(con, [TickerRow(cik, f"name {cik}", f"T{cik}", "Nasdaq") for cik in ciks])


def test_applies_sic_form_and_reason(
    con: duckdb.DuckDBPyConnection, zip_path: Path, entries: dict[str, dict]
) -> None:
    profiles = list(iter_submission_profiles(zip_path))
    _seed(con, [p.cik for p in profiles])
    assert apply_submission_profiles(con, profiles) == len(profiles)

    rows = dict(
        con.execute("SELECT cik, excluded_reason FROM universe").fetchall()  # type: ignore[arg-type]
    )
    apple = profile_by_label(zip_path, entries, "operating_10k")
    bank = profile_by_label(zip_path, entries, "bank")
    assert rows[apple.cik] is None
    assert rows[bank.cik] == "financial_sic"

    sic, form = con.execute(
        "SELECT sic, form_type FROM universe WHERE cik = ?", [apple.cik]
    ).fetchone()
    assert (sic, form) == ("3571", "10-K")


def test_existing_reason_is_not_overwritten(con: duckdb.DuckDBPyConnection, zip_path: Path) -> None:
    """取引所による除外が先に決まっている。理由は先に付いたものが残る。

    こうしておくと excluded_reason 別の件数がユニバースをちょうど分割する。
    """
    profiles = list(iter_submission_profiles(zip_path))
    bank = next(p for p in profiles if p.sic == "6022")
    replace_ticker_rows(con, [TickerRow(bank.cik, "OTC bank", "OTCB", "OTC")])

    apply_submission_profiles(con, profiles)

    reason, sic = con.execute("SELECT excluded_reason, sic FROM universe").fetchone()
    assert reason == "otc"
    # 理由は上書きしないが、SIC 自体は記録する。
    assert sic == "6022"


def test_applies_only_to_its_own_market(
    con: duckdb.DuckDBPyConnection, zip_path: Path, entries: dict[str, dict]
) -> None:
    profiles = list(iter_submission_profiles(zip_path))
    bank = next(p for p in profiles if p.sic == "6022")
    con.execute(
        "INSERT INTO universe (market, ticker, cik, updated_at) VALUES ('jp', '7203', ?, now())",
        [bank.cik],
    )
    apply_submission_profiles(con, profiles, market=Market.US)
    assert con.execute("SELECT excluded_reason FROM universe WHERE market = 'jp'").fetchone() == (
        None,
    )


def test_marks_companies_absent_from_submissions(con: duckdb.DuckDBPyConnection) -> None:
    """SIC も 10-K の有無も分からない銘柄を、判定できないまま残さない。"""
    _seed(con, [999_999])
    assert mark_missing_submissions(con) == 1
    assert con.execute("SELECT excluded_reason FROM universe").fetchone() == ("no_submissions",)


def test_marks_nothing_when_every_company_was_found(
    con: duckdb.DuckDBPyConnection, zip_path: Path
) -> None:
    profiles = list(iter_submission_profiles(zip_path))
    _seed(con, [p.cik for p in profiles])
    apply_submission_profiles(con, profiles)
    assert mark_missing_submissions(con) == 0


# --- ダウンロード -----------------------------------------------------------


def test_download_uses_the_documented_url_and_raw_dir(tmp_path: Path) -> None:
    from stock_radar.config import load_runtime

    runtime = load_runtime(Path(__file__).resolve().parents[1] / "config" / "runtime.yaml")
    sec = runtime.sec.model_copy(update={"raw_dir": tmp_path / "raw"})
    transport = FakeTransport([Response(200, b"zip")])

    dest = download_submissions(make_client(transport), sec)

    assert transport.requests[0][0] == SUBMISSIONS_ZIP_URL
    assert dest == tmp_path / "raw" / "submissions.zip"
    assert dest.read_bytes() == b"zip"
