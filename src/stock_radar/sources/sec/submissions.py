"""``submissions.zip`` から SIC と提出フォーム種別を取り、ユニバースを確定する。

なぜ zip か（2026-09-20 に実測）：

| 方式 | 転送量 | リクエスト数 |
|---|---|---|
| ``submissions.zip`` | 1.56 GB | 1 |
| 企業ごとの ``data.sec.gov/submissions/CIK*.json`` | 約1GB（1社 約160KB × 6,067社） | 6,067 |

転送量はほぼ同じで、後者は「企業ごとの API を連打しない」（CLAUDE.md）に反する。

zip には2種類のエントリがある。

- ``CIK##########.json`` — 表紙情報（``sic`` / ``exchanges`` / ``entityType``）＋ ``filings.recent``
- ``CIK##########-submissions-NNN.json`` — 古い提出履歴のシャード。表紙情報を持たない

**必要なのは前者だけ**なので後者は読み飛ばす。
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stock_radar.config import Market, SecRuntime
from stock_radar.sources.sec.client import SecClient, SecError
from stock_radar.sources.sec.universe import ExclusionReason
from stock_radar.storage import utc_now

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "ANNUAL_REPORT_FORMS",
    "FINANCIAL_SIC_MAX",
    "FINANCIAL_SIC_MIN",
    "SUBMISSIONS_ZIP_URL",
    "SubmissionProfile",
    "annual_report_form",
    "apply_submission_profiles",
    "download_submissions",
    "is_profile_entry",
    "iter_submission_profiles",
    "parse_submission_profile",
    "submission_exclusion",
]

SUBMISSIONS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"

# SIC 6000番台は金融・保険・不動産（REIT を含む）。共通足切りで除外する。
FINANCIAL_SIC_MIN = 6000
FINANCIAL_SIC_MAX = 6999

# 年次報告のフォーム。先頭が 10-K のものを優先する（10-K/A や旧 10-K405 も含む）。
# 20-F / 40-F しか出していない企業は ADR・外国民間発行体で、開示頻度・会計基準・
# XBRL の揃い方が異なるため第一弾では扱わない。
ANNUAL_REPORT_FORMS = ("10-K", "20-F", "40-F")

# CIK0000320193.json は対象、CIK0000320193-submissions-001.json は対象外。
_PROFILE_ENTRY = re.compile(r"(?:^|/)CIK(\d{10})\.json$")


@dataclass(frozen=True, slots=True)
class SubmissionProfile:
    cik: int
    sic: str | None
    entity_type: str | None
    # 見つかった年次報告フォーム。10-K を優先し、無ければ 20-F / 40-F を記録する。
    # 何も出していなければ None。
    annual_form: str | None

    @property
    def files_10k(self) -> bool:
        return self.annual_form is not None and self.annual_form.startswith("10-K")


def is_profile_entry(name: str) -> bool:
    """表紙情報を持つエントリか。シャードは読み飛ばす。"""
    return _PROFILE_ENTRY.search(name) is not None


def annual_report_form(forms: Iterable[Any]) -> str | None:
    """提出フォームの一覧から年次報告を1つ選ぶ。

    10-K を最優先する。10-K と 20-F の両方がある企業は 10-K 提出企業として扱う。
    """
    found: dict[str, str] = {}
    for form in forms:
        if not isinstance(form, str):
            continue
        for prefix in ANNUAL_REPORT_FORMS:
            if form.startswith(prefix) and prefix not in found:
                found[prefix] = form
    for prefix in ANNUAL_REPORT_FORMS:
        if prefix in found:
            return found[prefix]
    return None


def parse_submission_profile(payload: Any) -> SubmissionProfile:
    """1社分の ``CIK##########.json`` を読む。"""
    if not isinstance(payload, dict):
        raise SecError(f"submissions のエントリがオブジェクトでない: {type(payload)}")
    raw_cik = payload.get("cik")
    if raw_cik is None:
        raise SecError("submissions のエントリに cik が無い")

    sic = payload.get("sic")
    forms = payload.get("filings", {}).get("recent", {}).get("form", [])
    if not isinstance(forms, list):
        forms = []

    return SubmissionProfile(
        cik=int(raw_cik),
        sic=str(sic) if sic else None,
        entity_type=str(payload["entityType"]) if payload.get("entityType") else None,
        annual_form=annual_report_form(forms),
    )


def iter_submission_profiles(
    zip_path: Path, *, wanted_ciks: set[int] | None = None
) -> Iterator[SubmissionProfile]:
    """zip をストリームで読んで1社ずつ返す。

    ``wanted_ciks`` を渡すと、その CIK だけを展開する。上場していない企業まで
    JSON にするのは無駄なので、呼び出し側は ``universe`` の残存分を渡す。
    """
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            match = _PROFILE_ENTRY.search(info.filename)
            if match is None:
                continue
            if wanted_ciks is not None and int(match.group(1)) not in wanted_ciks:
                continue
            with archive.open(info) as handle:
                try:
                    payload = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise SecError(
                        f"submissions のエントリを解釈できない: {info.filename}\n{exc}"
                    ) from exc
            yield parse_submission_profile(payload)


def submission_exclusion(profile: SubmissionProfile) -> ExclusionReason | None:
    """SIC と提出フォームによる除外理由。残すなら ``None``。"""
    if (
        profile.sic
        and profile.sic.isdigit()
        and FINANCIAL_SIC_MIN <= int(profile.sic) <= FINANCIAL_SIC_MAX
    ):
        return ExclusionReason.FINANCIAL_SIC
    if not profile.files_10k:
        return ExclusionReason.NO_10K
    return None


def download_submissions(client: SecClient, runtime: SecRuntime) -> Path:
    dest = runtime.raw_dir / "submissions.zip"
    return client.download(SUBMISSIONS_ZIP_URL, dest)


def apply_submission_profiles(
    con: duckdb.DuckDBPyConnection,
    profiles: Iterable[SubmissionProfile],
    *,
    market: Market = Market.US,
) -> int:
    """``universe`` に SIC / フォーム種別と、それによる除外理由を書き込む。

    **すでに除外理由が付いている行は上書きしない。** 取引所による除外が先に
    決まっているので、理由は先に付いたものが残る。こうしておくと
    ``excluded_reason`` 別の件数がユニバースをちょうど分割する。
    """
    timestamp = utc_now()
    rows = [
        (
            profile.sic,
            profile.annual_form,
            exclusion.value if (exclusion := submission_exclusion(profile)) else None,
            timestamp,
            market.value,
            profile.cik,
        )
        for profile in profiles
    ]
    con.executemany(
        "UPDATE universe SET "
        "  sic = ?, "
        "  form_type = ?, "
        "  excluded_reason = coalesce(excluded_reason, ?), "
        "  updated_at = ? "
        "WHERE market = ? AND cik = ?",
        rows,
    )
    return len(rows)


def mark_missing_submissions(con: duckdb.DuckDBPyConnection, *, market: Market = Market.US) -> int:
    """submissions に現れなかった残存銘柄を除外する。

    SIC も 10-K の有無も分からない銘柄を、判定できないまま残さない。
    """
    marked = con.execute(
        "UPDATE universe SET excluded_reason = ?, updated_at = ? "
        "WHERE market = ? AND excluded_reason IS NULL AND form_type IS NULL AND sic IS NULL "
        "RETURNING ticker",
        [ExclusionReason.NO_SUBMISSIONS.value, utc_now(), market.value],
    ).fetchall()
    return len(marked)
