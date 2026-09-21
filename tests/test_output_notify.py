"""notificator への通知。

実際の疎通はクラスタ内でしか確かめられない（ゲート G4 / Phase 6-4）。
ここで固定するのは**ペイロードの形**と**文字数上限の収め方**、そして
**失敗しても run を落とさないこと**。
"""

from __future__ import annotations

import datetime as dt

import pytest

from stock_radar.config import Criteria, NotifyRuntime, load_criteria
from stock_radar.output.notify import build_message, notify, summary_lines
from stock_radar.screen.evaluate import evaluate
from tests.test_screen_logic import CRITERIA_PATH, TRACK_B, make

RUN_AT = dt.datetime(2026, 9, 21, 13, 22, 35)


@pytest.fixture(scope="module")
def criteria() -> Criteria:
    return load_criteria(CRITERIA_PATH)


@pytest.fixture
def runtime() -> NotifyRuntime:
    return NotifyRuntime(
        address_env="TEST_NOTIFICATOR_ADDRESS",
        path="/yuxsr.notification.v1.NotificatorService/Notify",
        timeout_sec=10.0,
        max_message_chars=900,
        top_n=5,
    )


class FakePoster:
    """送らずに記録する `Poster`。"""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, *, json_body: dict[str, str], timeout_sec: float) -> int:
        self.calls.append((url, json_body, timeout_sec))
        if self.error is not None:
            raise self.error
        return self.status


def lines_for(criteria: Criteria, count: int = 2, **kwargs: object) -> list[str]:
    passed = [evaluate(make(f"AA{i}"), criteria) for i in range(count)]
    defaults: dict[str, object] = {
        "run_at": RUN_AT,
        "market": "us",
        "universe_size": 3560,
        "track_counts": {"A": 8, "B": 14},
        "passed": passed,
        "price_coverage": 1.0,
        "coverage_warn_threshold": 0.90,
        "csv_path": "output/2026-09-21_us.csv",
        "top_n": 5,
    }
    defaults.update(kwargs)
    return summary_lines(**defaults)  # type: ignore[arg-type]


# --- 中身 -------------------------------------------------------------------


def test_summary_has_the_agreed_items(criteria: Criteria) -> None:
    """実行日・ユニバース件数・トラック別通過数・株価取得率・上位銘柄・CSV パス。"""
    text = "\n".join(lines_for(criteria))
    assert "stock-radar 2026-09-21 (us)" in text
    assert "候補 2件（A:8 / B:14）/ ユニバース 3,560" in text
    assert "株価取得 100%" in text
    assert "AA0" in text
    assert "CSV: output/2026-09-21_us.csv" in text


def test_warns_when_price_coverage_is_low(criteria: Criteria) -> None:
    """取りこぼしのせいで候補が少ない可能性を受け手に伝える。"""
    text = "\n".join(lines_for(criteria, price_coverage=0.5))
    assert "⚠️取りこぼしの可能性" in text


def test_no_warning_at_the_threshold(criteria: Criteria) -> None:
    text = "\n".join(lines_for(criteria, price_coverage=0.90))
    assert "⚠️" not in text


def test_limits_the_listed_candidates(criteria: Criteria) -> None:
    """候補リスト全体は送らない。LINE に流せる長さに収める。"""
    text = "\n".join(lines_for(criteria, count=20, top_n=5))
    assert text.count("  AA") == 5


def test_omits_the_list_when_nothing_passed(criteria: Criteria) -> None:
    text = "\n".join(lines_for(criteria, count=0))
    assert "上位:" not in text
    assert "候補 0件" in text


# --- 文字数の上限 -----------------------------------------------------------


def test_message_fits_in_the_limit(criteria: Criteria) -> None:
    lines = lines_for(criteria, count=20, top_n=20)
    message = build_message(lines, max_chars=120)
    assert len(message) <= 120
    # 行単位で落とす。途中で切ると「上位:」の途中で終わる。
    assert message.endswith("…")
    assert message.splitlines()[0] == lines[0]


def test_short_message_is_untouched(criteria: Criteria) -> None:
    lines = lines_for(criteria)
    assert build_message(lines, max_chars=900) == "\n".join(lines)


def test_falls_back_to_hard_truncation(criteria: Criteria) -> None:
    """1行目すら入らない設定でも例外にしない。"""
    assert len(build_message(lines_for(criteria), max_chars=10)) == 10


# --- 送信 -------------------------------------------------------------------


def test_posts_connect_json(runtime: NotifyRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect の unary + JSON。grpcio も proto のコード生成も要らない。"""
    monkeypatch.setenv("TEST_NOTIFICATOR_ADDRESS", "http://notificator.svc:50051")
    poster = FakePoster()
    result = notify("こんにちは", runtime, poster=poster)
    assert result.sent
    url, body, timeout = poster.calls[0]
    assert url == "http://notificator.svc:50051/yuxsr.notification.v1.NotificatorService/Notify"
    assert body == {"message": "こんにちは"}
    assert timeout == 10.0


def test_trailing_slash_in_the_address(
    runtime: NotifyRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_NOTIFICATOR_ADDRESS", "http://notificator.svc:50051/")
    poster = FakePoster()
    notify("x", runtime, poster=poster)
    assert "50051/yuxsr" in poster.calls[0][0]


def test_failure_does_not_raise(runtime: NotifyRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    """通知が届かなくても CSV が出ていれば運用は続けられる。"""
    monkeypatch.setenv("TEST_NOTIFICATOR_ADDRESS", "http://notificator.svc:50051")
    result = notify("x", runtime, poster=FakePoster(error=OSError("接続できない")))
    assert not result.sent
    assert "接続できない" in (result.error or "")


def test_non_200_is_a_failure(runtime: NotifyRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_NOTIFICATOR_ADDRESS", "http://notificator.svc:50051")
    result = notify("x", runtime, poster=FakePoster(status=503))
    assert not result.sent
    assert result.error == "HTTP 503"


def test_missing_address_is_a_failure_not_a_crash(
    runtime: NotifyRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_NOTIFICATOR_ADDRESS", raising=False)
    poster = FakePoster()
    result = notify("x", runtime, poster=poster)
    assert not result.sent
    assert poster.calls == []


def test_track_b_candidate_is_labelled(criteria: Criteria) -> None:
    passed = [evaluate(make("BBB", revenue=200_000_000.0, **TRACK_B), criteria)]
    text = "\n".join(lines_for(criteria, passed=passed))
    assert "BBB (B)" in text
