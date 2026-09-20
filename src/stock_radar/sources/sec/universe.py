"""ユニバースの確定（前半：ティッカーと取引所）。

``company_tickers_exchange.json`` から全銘柄を取り、取引所で足切りして
``universe`` テーブルに入れる。SIC と 10-K 提出実績による除外は後続。

**除外した銘柄も行として残す。** Phase 1 の検証項目が「除外理由別の件数」なので、
落としてしまうと何件をどの理由で外したのか分からなくなる。
``excluded_reason`` が NULL のものが残存ユニバース。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_radar.config import Market
from stock_radar.sources.sec.client import SecClient, SecError
from stock_radar.storage import as_utc_naive, utc_now

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "COMPANY_TICKERS_EXCHANGE_URL",
    "LISTED_EXCHANGES",
    "ExclusionReason",
    "TickerRow",
    "exchange_exclusion",
    "exclusion_counts",
    "fetch_ticker_rows",
    "parse_company_tickers_exchange",
    "replace_ticker_rows",
]

COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# 上場取引所とみなすもの。CBOE は CBOE BZX Exchange で、実在の上場取引所。
# OTC（店頭）と、取引所が空のものは除外する。
LISTED_EXCHANGES = frozenset({"nasdaq", "nyse", "cboe"})


class ExclusionReason(StrEnum):
    """``universe.excluded_reason`` に入る値。"""

    OTC = "otc"
    NO_EXCHANGE = "no_exchange"
    # 以下は SIC と提出フォームを取ってから付ける。
    FINANCIAL_SIC = "financial_sic"
    NO_10K = "no_10k"
    # submissions に現れなかった銘柄。SIC も 10-K の有無も判定できない。
    NO_SUBMISSIONS = "no_submissions"


@dataclass(frozen=True, slots=True)
class TickerRow:
    cik: int
    name: str
    ticker: str
    exchange: str | None


def exchange_exclusion(exchange: str | None) -> ExclusionReason | None:
    """取引所による除外理由。残すなら ``None``。"""
    if exchange is None or not exchange.strip():
        return ExclusionReason.NO_EXCHANGE
    if exchange.strip().lower() in LISTED_EXCHANGES:
        return None
    return ExclusionReason.OTC


def parse_company_tickers_exchange(payload: Any) -> list[TickerRow]:
    """``{"fields": [...], "data": [[...], ...]}`` を読む。

    列は名前で引く。SEC が列順を変えても静かに壊れないようにするため。
    形が変わったときは黙って空を返さず落とす。
    """
    if not isinstance(payload, dict):
        raise SecError(f"company_tickers_exchange の最上位がオブジェクトでない: {type(payload)}")
    fields = payload.get("fields")
    data = payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise SecError("company_tickers_exchange に fields / data が無い")

    try:
        index = {name: fields.index(name) for name in ("cik", "name", "ticker", "exchange")}
    except ValueError as exc:
        raise SecError(f"company_tickers_exchange に想定の列が無い: {fields}") from exc

    rows: list[TickerRow] = []
    for raw in data:
        if not isinstance(raw, list) or len(raw) != len(fields):
            raise SecError(f"company_tickers_exchange の行の形が想定と違う: {raw!r}")
        ticker = raw[index["ticker"]]
        if not ticker:
            # 実データには無いが、あれば主キーが作れないので落とす。
            raise SecError(f"ticker が空の行がある: {raw!r}")
        exchange = raw[index["exchange"]]
        rows.append(
            TickerRow(
                cik=int(raw[index["cik"]]),
                name=str(raw[index["name"]]),
                ticker=str(ticker),
                exchange=str(exchange) if exchange else None,
            )
        )
    return rows


def fetch_ticker_rows(client: SecClient) -> list[TickerRow]:
    return parse_company_tickers_exchange(client.get_json(COMPANY_TICKERS_EXCHANGE_URL))


def replace_ticker_rows(
    con: duckdb.DuckDBPyConnection,
    rows: list[TickerRow],
    *,
    market: Market = Market.US,
    now: dt.datetime | None = None,
) -> int:
    """``universe`` のその市場の行を丸ごと入れ替える。

    ``universe`` は再生成可なので差分更新にしない。上場廃止でティッカーが
    SEC の一覧から消えたとき、差分更新だと古い行が残り続ける。
    """
    timestamp = as_utc_naive(now) if now is not None else utc_now()
    payload = [
        (
            market.value,
            row.ticker,
            row.cik,
            row.name,
            row.exchange,
            exclusion.value if (exclusion := exchange_exclusion(row.exchange)) else None,
            timestamp,
        )
        for row in rows
    ]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM universe WHERE market = ?", [market.value])
        con.executemany(
            "INSERT INTO universe "
            "(market, ticker, cik, name, exchange, excluded_reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return len(payload)


def exclusion_counts(
    con: duckdb.DuckDBPyConnection, *, market: Market = Market.US
) -> dict[str | None, int]:
    """除外理由ごとの件数。``None`` が残存。

    Phase 1 の検証項目「除外理由別の件数も出す」がこれ。
    """
    rows = con.execute(
        "SELECT excluded_reason, count(*) FROM universe WHERE market = ? GROUP BY 1",
        [market.value],
    ).fetchall()
    return {reason: count for reason, count in rows}
