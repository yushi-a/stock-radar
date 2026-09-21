"""yfinance 版の :class:`PriceSource`。

**バージョンは `pyproject.toml` でピン留めする**（docs/architecture.md）。
429 の挙動は yfinance のバージョンに強く依存する（cookie/crumb の取得方式、
ブラウザ偽装の有無など、この領域は上流で頻繁に修正されている）。
429 が多発したら、自前の制御を疑う前にまず上流の新しいバージョンを試す。

2026-09-21 実測：素の curl は 429（ブラウザ風 User-Agent でも）だが、
yfinance 1.7.0 経由では通る。
"""

from __future__ import annotations

import datetime as dt
import logging

from stock_radar.sources.prices.base import Bar, ErrorClass, PriceFetchError

__all__ = ["YFinanceSource", "classify_error"]

log = logging.getLogger(__name__)

# 429 を示す文字列。yfinance は例外型を分けてくれないのでメッセージを見る。
_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit")
# 銘柄が存在しない・上場廃止。恒久的失敗として扱う候補。
_INVALID_SYMBOL_MARKERS = (
    "no data found",
    "delisted",
    "no price data found",
    "symbol may be delisted",
    "not found",
)


def classify_error(message: str) -> ErrorClass:
    """yfinance の例外メッセージを `fetch_failures.error_class` に振り分ける。

    一時的と恒久的を区別しないと、上場廃止銘柄を毎週リトライし続けることになる。
    """
    lowered = message.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return ErrorClass.RATE_LIMITED
    if any(marker in lowered for marker in _INVALID_SYMBOL_MARKERS):
        return ErrorClass.INVALID_SYMBOL
    return ErrorClass.OTHER


class YFinanceSource:
    """1銘柄ずつ直列に取る。

    `yf.download()` に複数ティッカーを渡しても内部ではティッカーごとに個別の
    HTTP リクエストを投げている。**リクエスト数は対象銘柄数そのもの**で、
    差分取得しても減らない（減るのはペイロードだけ）。まとめても速くならないので、
    素直に1銘柄ずつ扱ってペースを制御する。
    """

    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        import yfinance

        try:
            frame = yfinance.Ticker(ticker).history(
                start=start.isoformat(),
                # yfinance の end は排他的。最終日を含めたいので1日足す。
                end=(end + dt.timedelta(days=1)).isoformat(),
                interval="1d",
                # 分割調整済み・配当未調整。「実際に株価がどこを通ってきたか」を
                # 見る 52週高安の用途に合う（docs/architecture.md）。
                auto_adjust=False,
                actions=False,
                raise_errors=True,
            )
        except Exception as exc:  # yfinance は独自の例外型を持たない
            message = str(exc) or type(exc).__name__
            raise PriceFetchError(ticker, classify_error(message), message) from exc

        return [
            Bar(
                date=index.date(),
                open=_number(row.get("Open")),
                high=_number(row.get("High")),
                low=_number(row.get("Low")),
                close=_number(row.get("Close")),
                volume=_integer(row.get("Volume")),
            )
            for index, row in frame.iterrows()
        ]


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN を除く


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)
