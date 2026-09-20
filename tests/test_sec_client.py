"""SEC アクセス層。

実 API は叩かない（ソケットは pytest-socket で塞いである）。偽の Transport を
相手にする。待ち時間の計算は純粋関数なので、一度も待たずに値を確かめられる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_radar.config import ConfigError, load_runtime
from stock_radar.sources.sec.client import (
    Response,
    SecClient,
    SecError,
    SecHttpError,
    parse_retry_after,
    retry_wait_sec,
    should_retry,
    throttle_wait_sec,
    validate_user_agent,
)
from tests.helpers import FakeTransport, make_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = REPO_ROOT / "config" / "runtime.yaml"


# --- 待ち時間の計算 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("elapsed", "interval", "expected"),
    [(0.0, 0.15, 0.15), (0.1, 0.15, pytest.approx(0.05)), (0.15, 0.15, 0.0), (9.0, 0.15, 0.0)],
)
def test_throttle_wait_sec(elapsed: float, interval: float, expected: float) -> None:
    assert throttle_wait_sec(elapsed, interval) == expected


def test_throttle_wait_sec_rejects_negative_elapsed() -> None:
    with pytest.raises(ValueError, match="負"):
        throttle_wait_sec(-1.0, 0.15)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, True), (500, True), (503, True), (200, False), (404, False), (403, False)],
)
def test_should_retry(status: int, expected: bool) -> None:
    """404 を延々と叩き直さないこと。"""
    assert should_retry(status) is expected


@pytest.mark.parametrize(("attempt", "expected"), [(1, 2.0), (2, 4.0), (3, 8.0)])
def test_retry_wait_sec_backs_off_exponentially(attempt: int, expected: float) -> None:
    assert retry_wait_sec(attempt, 2.0) == expected


def test_retry_wait_sec_prefers_retry_after() -> None:
    """サーバの指示より早く叩き直さない。"""
    assert retry_wait_sec(3, 2.0, retry_after_sec=1.0) == 1.0


def test_retry_wait_sec_rejects_attempt_below_one() -> None:
    with pytest.raises(ValueError, match="attempt"):
        retry_wait_sec(0, 2.0)


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), (" 2.5 ", 2.5), ("0", 0.0), (None, None), ("", None), ("-1", None)],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_ignores_http_date() -> None:
    """日時形式は扱わない。誤って0秒と解釈して即叩き直すよりは無視する。"""
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


# --- User-Agent -------------------------------------------------------------


def test_validate_user_agent_requires_a_contact() -> None:
    with pytest.raises(SecError, match="連絡先"):
        validate_user_agent("stock-radar")


def test_user_agent_value_is_not_echoed_in_the_error() -> None:
    """エラーメッセージにメールアドレスを載せない。ログに残ると連絡先が漏れる。"""
    with pytest.raises(SecError) as excinfo:
        validate_user_agent("secret-build-name-12345")
    assert "secret-build-name-12345" not in str(excinfo.value)


def test_from_runtime_fails_before_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-Agent が無ければ SEC に出る前に落ちること。"""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    transport = FakeTransport([])
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        SecClient.from_runtime(load_runtime(RUNTIME_PATH).sec, transport)
    assert transport.requests == []


def test_from_runtime_builds_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "stock-radar tester@example.com")
    client = SecClient.from_runtime(load_runtime(RUNTIME_PATH).sec, FakeTransport([]))
    assert client.headers["User-Agent"] == "stock-radar tester@example.com"


def test_every_request_carries_the_user_agent() -> None:
    transport = FakeTransport([Response(200, b"{}")])
    make_client(transport).get_json("https://example.com/x.json")
    _, headers = transport.requests[0]
    assert headers["User-Agent"] == "stock-radar tester@example.com"


# --- リトライ ---------------------------------------------------------------


def test_get_json_returns_the_payload() -> None:
    transport = FakeTransport([Response(200, json.dumps({"a": 1}).encode())])
    assert make_client(transport).get_json("https://example.com/x.json") == {"a": 1}


def test_get_json_reports_unparsable_body() -> None:
    transport = FakeTransport([Response(200, b"<html>maintenance</html>")])
    with pytest.raises(SecError, match="JSON"):
        make_client(transport).get_json("https://example.com/x.json")


def test_retries_on_429_then_succeeds() -> None:
    transport = FakeTransport(
        [
            Response(429, headers={"Retry-After": "0"}),
            Response(429, headers={"Retry-After": "0"}),
            Response(200, b"{}"),
        ]
    )
    assert make_client(transport).get_json("https://example.com/x.json") == {}
    assert len(transport.requests) == 3


def test_gives_up_after_max_attempts() -> None:
    transport = FakeTransport([Response(429, headers={"Retry-After": "0"})] * 3)
    with pytest.raises(SecHttpError) as excinfo:
        make_client(transport).get_json("https://example.com/x.json")
    assert excinfo.value.status_code == 429
    assert len(transport.requests) == 3


def test_does_not_retry_a_permanent_error() -> None:
    transport = FakeTransport([Response(404)])
    with pytest.raises(SecHttpError) as excinfo:
        make_client(transport).get_json("https://example.com/x.json")
    assert excinfo.value.status_code == 404
    assert len(transport.requests) == 1


# --- ダウンロード -----------------------------------------------------------


def test_download_writes_the_file(tmp_path: Path) -> None:
    transport = FakeTransport([Response(200, b"zip-bytes")])
    dest = tmp_path / "raw" / "submissions.zip"
    assert make_client(transport).download("https://example.com/s.zip", dest) == dest
    assert dest.read_bytes() == b"zip-bytes"


def test_download_retries_and_raises_like_get(tmp_path: Path) -> None:
    transport = FakeTransport([Response(503)] * 3)
    with pytest.raises(SecHttpError):
        make_client(transport).download("https://example.com/s.zip", tmp_path / "s.zip")
    assert len(transport.requests) == 3
