"""SEC へのアクセス層。

SEC の作法（docs/architecture.md「SEC アクセスの作法」）をここに閉じ込める。

- User-Agent に連絡先を入れる（SEC Developer FAQ）。値は環境変数から取り、ファイルには書かない
- fair access（10 リクエスト/秒）を超えない間隔で回す
- 一括取得は zip を使い、企業ごとの API を連打しない

HTTP 層は :class:`Transport` で差し替えられる。429 を返す偽レスポンスに対して
テストするために要る（docs/testing.md）。

待ち時間の「計算」と「実際に待つ」は分けてある。計算側が純粋関数なので、
テストは一度も待たずに値を確かめられる。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from stock_radar.config import SecRuntime, require_env

__all__ = [
    "Response",
    "SecClient",
    "SecError",
    "SecHttpError",
    "Transport",
    "parse_retry_after",
    "retry_wait_sec",
    "should_retry",
    "throttle_wait_sec",
    "validate_user_agent",
]


class SecError(Exception):
    """SEC アクセスに関する失敗。"""


class SecHttpError(SecError):
    """リトライしても成功しなかった、または恒久的なエラー応答。"""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"SEC への要求が失敗した（HTTP {status_code}）: {url}")
        self.url = url
        self.status_code = status_code


# --- 待ち時間の計算（純粋関数） ---------------------------------------------


def throttle_wait_sec(elapsed_sec: float, min_interval_sec: float) -> float:
    """前のリクエストから ``elapsed_sec`` 経ったとき、次に待つべき秒数。"""
    if elapsed_sec < 0:
        raise ValueError(f"経過秒が負になっている: {elapsed_sec}")
    return max(0.0, min_interval_sec - elapsed_sec)


def should_retry(status_code: int) -> bool:
    """この応答をリトライしてよいか。

    429（レート超過）と 5xx（一時障害）だけ。404 を延々と叩き直さない。
    """
    return status_code == 429 or 500 <= status_code < 600


def retry_wait_sec(attempt: int, base_sec: float, retry_after_sec: float | None = None) -> float:
    """``attempt`` 回目の失敗の後に待つ秒数。

    サーバが ``Retry-After`` を指定していればそれに従う。こちらの都合で
    早く叩き直すのは相手の指示を無視することになる。
    """
    if attempt < 1:
        raise ValueError(f"attempt は1以上: {attempt}")
    if retry_after_sec is not None and retry_after_sec >= 0:
        return retry_after_sec
    return base_sec * (2 ** (attempt - 1))


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` ヘッダを秒に直す。秒数形式のみ扱い、日時形式は無視する。"""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def validate_user_agent(value: str) -> str:
    """SEC が求める「連絡先入りの User-Agent」になっているか。

    値そのものはメールアドレスなので、エラーメッセージに載せない。
    """
    if "@" not in value:
        raise SecError(
            "SEC_USER_AGENT に連絡先のメールアドレスが含まれていない。"
            "「アプリ名 you@example.com」の形で環境変数に設定する（SEC Developer FAQ）。"
        )
    return value


# --- HTTP 層 ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Response:
    status_code: int
    content: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """ヘッダ名は大文字小文字を区別しない。"""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class Transport(Protocol):
    """差し替え可能な HTTP 層。"""

    def get(self, url: str, headers: Mapping[str, str]) -> Response: ...

    def download(self, url: str, headers: Mapping[str, str], dest: Path) -> Response:
        """本文をメモリに載せずに ``dest`` へ書く。

        ``submissions.zip`` は 1.5GB 超あるため、これが無いと落ちる。
        成功時の :attr:`Response.content` は空でよい。
        """
        ...


class HttpxTransport:
    """本番用の HTTP 層。"""

    def __init__(self, timeout_sec: float = 60.0) -> None:
        self._timeout_sec = timeout_sec

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        import httpx

        response = httpx.get(url, headers=dict(headers), timeout=self._timeout_sec)
        return Response(response.status_code, response.content, dict(response.headers))

    def download(self, url: str, headers: Mapping[str, str], dest: Path) -> Response:
        import httpx

        dest.parent.mkdir(parents=True, exist_ok=True)
        # 途中で落ちた不完全なファイルを本物として残さない。
        partial = dest.with_suffix(dest.suffix + ".part")
        with httpx.stream(
            "GET", url, headers=dict(headers), timeout=self._timeout_sec, follow_redirects=True
        ) as response:
            if response.status_code != 200:
                response.read()
                return Response(response.status_code, b"", dict(response.headers))
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
            partial.replace(dest)
            return Response(response.status_code, b"", dict(response.headers))


# --- クライアント -----------------------------------------------------------


class SecClient:
    """SEC を叩く唯一の入口。"""

    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_sec: float,
        max_attempts: int,
        retry_backoff_sec: float,
        transport: Transport,
    ) -> None:
        self._user_agent = validate_user_agent(user_agent)
        self._min_interval_sec = min_interval_sec
        self._max_attempts = max_attempts
        self._retry_backoff_sec = retry_backoff_sec
        self._transport = transport
        self._last_request_at: float | None = None

    @classmethod
    def from_runtime(cls, runtime: SecRuntime, transport: Transport | None = None) -> SecClient:
        """設定と環境変数から組み立てる。

        User-Agent が無ければ**ネットワークに出る前に**落ちる。
        """
        return cls(
            user_agent=require_env(runtime.user_agent_env),
            min_interval_sec=runtime.min_request_interval_sec,
            max_attempts=runtime.max_attempts,
            retry_backoff_sec=runtime.retry_backoff_sec,
            transport=transport if transport is not None else HttpxTransport(),
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
        }

    def get_json(self, url: str) -> Any:
        response = self._request(url)
        try:
            return json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise SecError(f"JSON として解釈できない応答: {url}\n{exc}") from exc

    def download(self, url: str, dest: Path) -> Path:
        self._request(url, dest=dest)
        return dest

    def _request(self, url: str, dest: Path | None = None) -> Response:
        response: Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_slot()
            if dest is None:
                response = self._transport.get(url, self.headers)
            else:
                response = self._transport.download(url, self.headers, dest)
            self._last_request_at = time.monotonic()

            if response.status_code == 200:
                return response
            if not should_retry(response.status_code) or attempt == self._max_attempts:
                break
            time.sleep(
                retry_wait_sec(
                    attempt,
                    self._retry_backoff_sec,
                    parse_retry_after(response.header("Retry-After")),
                )
            )

        assert response is not None  # max_attempts >= 1 なので必ず1回は入る
        raise SecHttpError(url, response.status_code)

    def _wait_for_slot(self) -> None:
        if self._last_request_at is None:
            return
        wait = throttle_wait_sec(time.monotonic() - self._last_request_at, self._min_interval_sec)
        if wait > 0:
            time.sleep(wait)
