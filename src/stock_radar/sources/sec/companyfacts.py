"""`companyfacts.zip` の取得と、縦持ちテーブルへの取り込み。

正規化（横持ちの `fundamentals` 化）はここではやらない。ここは
「SEC が言っていることをそのまま DuckDB に置く」層。

**格納するのは `concepts.py` に挙げたタグだけ。** us-gaap には1社500超の概念があり、
全部入れると行数が桁違いになる。実測（Phase 2a）でも読んだのはこの範囲。

期間の粒度でテーブルを分ける。`docs/xbrl-findings.md` の A-4 / B が根拠。

- `facts_annual`    … 10-K 系で、期間長 350〜380日 または 時点値（BS・株数）
- `facts_quarterly` … 売上のみ、期間長 80〜100日
- それ以外（半期・9ヶ月の比較データ）は**捨てる**。使う場面が無く、実測で約110万行ある
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stock_radar.config import Market, SecRuntime
from stock_radar.sources.sec import concepts
from stock_radar.sources.sec.client import SecClient, SecError

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "ANNUAL_MAX_DAYS",
    "ANNUAL_MIN_DAYS",
    "COMPANYFACTS_ZIP_URL",
    "QUARTER_MAX_DAYS",
    "QUARTER_MIN_DAYS",
    "Fact",
    "download_companyfacts",
    "iter_company_facts",
    "latest_companyfacts",
    "parse_company_facts",
    "period_kind",
    "prune_companyfacts",
    "store_facts",
    "wanted_ciks",
]

COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# 52週決算は364日、53週決算は371日。実測の分布は 364 / 365 / 366 / 371 のみだった。
ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
# 四半期。実測では 89〜92日に集中し、62日や98日も出る。
QUARTER_MIN_DAYS = 80
QUARTER_MAX_DAYS = 100

_ENTRY = re.compile(r"(?:^|/)CIK(\d{10})\.json$")
_FILENAME = re.compile(r"^companyfacts_(\d{4}-\d{2}-\d{2})\.zip$")

# 金額系のタグ。単位は原則 USD だが、実測で34社が CAD などを混ぜており、
# 4社は年次売上を USD で一度も報告していない。生の段階では単位ごと残し、
# どれを採るかは正規化（Phase 2b-2）で決める。
_MONETARY: dict[str, tuple[str, ...]] = {
    "us-gaap": tuple(
        {
            *concepts.REVENUE,
            *concepts.GROSS_PROFIT,
            *concepts.COST_OF_REVENUE,
            *concepts.OPERATING_INCOME,
            *concepts.NET_INCOME,
            *concepts.OPERATING_CASH_FLOW,
            *concepts.CAPEX,
            *concepts.TOTAL_ASSETS,
            *concepts.CURRENT_ASSETS,
            *concepts.CURRENT_LIABILITIES,
            *concepts.EQUITY,
        }
    )
}
_WANTED: dict[str, frozenset[str]] = {
    "us-gaap": frozenset(_MONETARY["us-gaap"])
    | frozenset(tag for tax, tag in concepts.SHARES_OUTSTANDING if tax == "us-gaap"),
    "dei": frozenset(tag for tax, tag in concepts.SHARES_OUTSTANDING if tax == "dei"),
}

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Fact:
    """縦持ち1行。SEC の1エントリに対応する。"""

    cik: int
    concept: str
    unit: str
    # 期間末の暦年。**SEC の `fy` は使わない。** `fy` は「報告した提出書類」の
    # 会計年度で、実測では 66% が期間末の年と一致しない（docs/xbrl-findings.md の A-1）。
    fiscal_year: int
    fiscal_period: str | None
    period_start: dt.date | None
    period_end: dt.date
    value: float | None
    accn: str
    filed_at: dt.date | None


def period_kind(start: dt.date | None, end: dt.date) -> str | None:
    """このエントリを入れるテーブル。使わない粒度なら ``None``。

    ``start`` が無ければ時点値（BS・株数）。実測で BS 項目に ``start`` は
    1件も付いていなかった（docs/xbrl-findings.md の A-8）。
    """
    if start is None:
        return "annual"
    days = (end - start).days + 1
    if ANNUAL_MIN_DAYS <= days <= ANNUAL_MAX_DAYS:
        return "annual"
    if QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS:
        return "quarterly"
    return None


def _date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def parse_company_facts(payload: Any) -> tuple[list[Fact], list[Fact]]:
    """1社分の ``CIK##########.json`` を ``(年次, 四半期)`` に分けて返す。"""
    if not isinstance(payload, dict):
        raise SecError(f"companyfacts のエントリがオブジェクトでない: {type(payload)}")
    raw_cik = payload.get("cik")
    if raw_cik is None:
        raise SecError("companyfacts のエントリに cik が無い")
    cik = int(raw_cik)

    annual: list[Fact] = []
    quarterly: list[Fact] = []
    for taxonomy, wanted in _WANTED.items():
        node = payload.get("facts", {}).get(taxonomy) or {}
        for concept, body in node.items():
            if concept not in wanted:
                continue
            for unit, entries in (body.get("units") or {}).items():
                for entry in entries:
                    fact = _to_fact(cik, concept, unit, entry)
                    if fact is None:
                        continue
                    form = str(entry.get("form", ""))
                    kind = period_kind(fact.period_start, fact.period_end)
                    if kind == "annual" and form.startswith("10-K"):
                        annual.append(fact)
                    elif (
                        kind == "quarterly"
                        and concept in concepts.REVENUE
                        and form.startswith(("10-K", "10-Q"))
                    ):
                        quarterly.append(fact)
    return annual, quarterly


def _to_fact(cik: int, concept: str, unit: str, entry: dict) -> Fact | None:
    end = _date(entry.get("end"))
    accn = entry.get("accn")
    if end is None or not accn:
        return None
    return Fact(
        cik=cik,
        concept=concept,
        unit=unit,
        fiscal_year=end.year,
        fiscal_period=str(entry["fp"]) if entry.get("fp") else None,
        period_start=_date(entry.get("start")),
        period_end=end,
        value=entry.get("val"),
        accn=str(accn),
        filed_at=_date(entry.get("filed")),
    )


def iter_company_facts(
    zip_path: Path, *, wanted: set[int] | None = None
) -> Iterator[tuple[list[Fact], list[Fact]]]:
    """zip をストリームで読み、1社ずつ返す。全社分を溜め込まない。"""
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            match = _ENTRY.search(info.filename)
            if match is None:
                continue
            if wanted is not None and int(match.group(1)) not in wanted:
                continue
            with archive.open(info) as handle:
                try:
                    payload = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise SecError(
                        f"companyfacts のエントリを解釈できない: {info.filename}\n{exc}"
                    ) from exc
            yield parse_company_facts(payload)


# --- ファイルの世代管理 -----------------------------------------------------


def latest_companyfacts(raw_dir: Path) -> tuple[Path, dt.date] | None:
    """手元にある最新世代の zip と、その日付。"""
    found: list[tuple[dt.date, Path]] = []
    if not raw_dir.exists():
        return None
    for path in raw_dir.glob("companyfacts_*.zip"):
        match = _FILENAME.match(path.name)
        if match:
            found.append((dt.date.fromisoformat(match.group(1)), path))
    if not found:
        return None
    stamp, path = max(found)
    return path, stamp


def prune_companyfacts(raw_dir: Path, keep: int) -> list[Path]:
    """古い世代を消す。1ファイル1.4GB あるので溜めない。"""
    found = []
    for path in raw_dir.glob("companyfacts_*.zip"):
        match = _FILENAME.match(path.name)
        if match:
            found.append((dt.date.fromisoformat(match.group(1)), path))
    removed = []
    for _, path in sorted(found, reverse=True)[keep:]:
        path.unlink()
        removed.append(path)
    return removed


def download_companyfacts(
    client: SecClient, runtime: SecRuntime, *, today: dt.date | None = None, force: bool = False
) -> Path:
    """必要なら取得して、使う zip のパスを返す。"""
    now = today if today is not None else dt.date.today()
    existing = latest_companyfacts(runtime.raw_dir)
    if not force and existing is not None:
        path, stamp = existing
        if (now - stamp).days <= runtime.companyfacts_max_age_days:
            log.info("手元の companyfacts を使う（%s）", path)
            return path

    dest = runtime.raw_dir / f"companyfacts_{now.isoformat()}.zip"
    log.info("companyfacts.zip を取得中（1.4GB 超）…")
    client.download(COMPANYFACTS_ZIP_URL, dest)
    for removed in prune_companyfacts(runtime.raw_dir, runtime.keep_generations):
        log.info("古い世代を削除した（%s）", removed)
    return dest


# --- DuckDB への書き込み ----------------------------------------------------

# 行単位の INSERT は DuckDB では極端に遅い（実測で20万行に240秒）。
# CSV に書き出して COPY すると 125万行が2.4秒で入る。列指向のストレージに
# 1行ずつ追記させないための回り道で、依存も増えない。
_ANNUAL_COLUMNS = "cik, fiscal_year, period_start, period_end, concept, value, unit, accn, filed_at"
_QUARTERLY_COLUMNS = (
    "cik, fiscal_year, fiscal_period, period_start, period_end, concept, value, unit, "
    "accn, filed_at"
)


def wanted_ciks(con: duckdb.DuckDBPyConnection, *, market: Market = Market.US) -> set[int]:
    """`universe` の残存銘柄の CIK。除外済みの企業は展開しない。"""
    rows = con.execute(
        "SELECT DISTINCT cik FROM universe "
        "WHERE market = ? AND excluded_reason IS NULL AND cik IS NOT NULL",
        [market.value],
    ).fetchall()
    return {int(row[0]) for row in rows}


def _annual_row(fact: Fact) -> tuple:
    return (
        fact.cik,
        fact.fiscal_year,
        fact.period_start or "",
        fact.period_end,
        fact.concept,
        "" if fact.value is None else fact.value,
        fact.unit,
        fact.accn,
        fact.filed_at or "",
    )


def _quarterly_row(fact: Fact) -> tuple:
    return (
        fact.cik,
        fact.fiscal_year,
        fact.fiscal_period or "",
        fact.period_start or "",
        fact.period_end,
        fact.concept,
        "" if fact.value is None else fact.value,
        fact.unit,
        fact.accn,
        fact.filed_at or "",
    )


def store_facts(
    con: duckdb.DuckDBPyConnection,
    batches: Iterable[tuple[list[Fact], list[Fact]]],
    *,
    market: Market = Market.US,
) -> tuple[int, int]:
    """縦持ちテーブルを入れ替える。

    `facts_annual` / `facts_quarterly` は再生成可なので差分更新にしない。
    ユニバースから外れた企業の行も一緒に落ちる。
    """
    keep = wanted_ciks(con, market=market)
    annual_total = quarterly_total = 0

    with tempfile.TemporaryDirectory(prefix="stock-radar-facts-") as tmp:
        annual_path = Path(tmp) / "facts_annual.csv"
        quarterly_path = Path(tmp) / "facts_quarterly.csv"
        with (
            annual_path.open("w", newline="", encoding="utf-8") as annual_file,
            quarterly_path.open("w", newline="", encoding="utf-8") as quarterly_file,
        ):
            annual_writer = csv.writer(annual_file)
            quarterly_writer = csv.writer(quarterly_file)
            for annual, quarterly in batches:
                for fact in annual:
                    if fact.cik in keep:
                        annual_writer.writerow(_annual_row(fact))
                        annual_total += 1
                for fact in quarterly:
                    if fact.cik in keep:
                        quarterly_writer.writerow(_quarterly_row(fact))
                        quarterly_total += 1

        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DELETE FROM facts_annual")
            con.execute("DELETE FROM facts_quarterly")
            _copy(con, "facts_annual", _ANNUAL_COLUMNS, annual_path)
            _copy(con, "facts_quarterly", _QUARTERLY_COLUMNS, quarterly_path)
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")

    return annual_total, quarterly_total


def _copy(con: duckdb.DuckDBPyConnection, table: str, columns: str, path: Path) -> None:
    if path.stat().st_size == 0:
        return
    con.execute(
        f"COPY {table} ({columns}) FROM ? "
        "(FORMAT CSV, HEADER false, NULLSTR '', DATEFORMAT '%Y-%m-%d')",
        [str(path)],
    )
