"""価格ソースの差し替え口。

差し替え可能にしておく理由は2つ（docs/architecture.md / docs/testing.md）。

- **429 を返す偽ソースでテストする。** 実 API を叩かずに退避の挙動を確かめたい
- **要件 R5：yfinance → FMP に差し替えられる。** yfinance は非公式で、
  仕様変更や恒常的な 429 に当たったら乗り換える前提がある

どのみち要る分離なので最初から作る。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "Bar",
    "ErrorClass",
    "PriceFetchError",
    "PriceSource",
]


class ErrorClass(StrEnum):
    """`fetch_failures.error_class`。

    一時的と恒久的を区別しないと、上場廃止銘柄を毎週リトライし続けることになる。
    """

    RATE_LIMITED = "rate_limited"
    # シンボル不正・データなし。上場廃止やティッカー変更を疑う。
    INVALID_SYMBOL = "invalid_symbol"
    OTHER = "other"

    @property
    def is_temporary(self) -> bool:
        return self is not ErrorClass.INVALID_SYMBOL


class PriceFetchError(Exception):
    """1銘柄ぶんの取得が失敗した。全体は止めず、次の銘柄へ進む。"""

    def __init__(self, ticker: str, error_class: ErrorClass, message: str) -> None:
        super().__init__(f"{ticker}: {message}")
        self.ticker = ticker
        self.error_class = error_class
        self.message = message


@dataclass(frozen=True, slots=True)
class Bar:
    """日次の1本。

    `adj_close` は持たない。配当のたびに過去が遡及的に書き換わるうえ、使っている
    指標が1つも無い（52週高安は high/low、平均売買代金は close × volume、
    時価総額は close）。保存すると「古い値が残り続けるが誰も気づかない」罠だけが残る。
    """

    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


class PriceSource(Protocol):
    """日次株価の取得口。"""

    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        """``start``〜``end`` の日次バーを返す。

        取得できなければ :class:`PriceFetchError` を投げる。空リストは
        「その期間に取引が無かった」を意味し、失敗ではない。
        """
        ...
