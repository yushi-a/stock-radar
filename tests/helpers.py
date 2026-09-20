"""テストから使う道具。テストケースそのものは置かない。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from stock_radar.sources.sec.client import Response, SecClient

__all__ = ["FakeTransport", "make_client"]


class FakeTransport:
    """決めた応答を順に返す Transport。

    実 API を叩かずに 429 やリトライの挙動を確かめるために要る（docs/testing.md）。
    """

    def __init__(self, responses: list[Response]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.downloads: list[Path] = []

    def _next(self, url: str, headers: Mapping[str, str]) -> Response:
        self.requests.append((url, dict(headers)))
        if not self._responses:
            raise AssertionError(f"想定より多く呼ばれた: {url}")
        return self._responses.pop(0)

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        return self._next(url, headers)

    def download(self, url: str, headers: Mapping[str, str], dest: Path) -> Response:
        response = self._next(url, headers)
        self.downloads.append(dest)
        if response.status_code == 200:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.content)
        return response


def make_client(transport: FakeTransport, **overrides: object) -> SecClient:
    """待たない設定の SecClient。

    間隔と待ち時間を0にしてあるので実時間に依存しない。値そのものの検証は
    純粋関数（``throttle_wait_sec`` 等）の側で行う。
    """
    kwargs: dict[str, object] = {
        "user_agent": "stock-radar tester@example.com",
        "min_interval_sec": 0.0,
        "max_attempts": 3,
        "retry_backoff_sec": 0.0,
        "transport": transport,
    }
    kwargs.update(overrides)
    return SecClient(**kwargs)  # type: ignore[arg-type]
