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
        max_listed=20,
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
        "max_listed": 20,
    }
    defaults.update(kwargs)
    return summary_lines(**defaults)  # type: ignore[arg-type]


# --- 中身 -------------------------------------------------------------------


def test_summary_has_the_agreed_items(criteria: Criteria) -> None:
    """実行日・ユニバース件数・トラック別通過数・株価取得率・候補一覧・CSV パス。"""
    text = "\n".join(lines_for(criteria))
    assert "stock-radar 2026-09-21 (us)" in text
    assert "候補 2件（A:8 / B:14）/ 3,560銘柄中" in text
    assert "株価取得 100%" in text
    assert "AA0" in text
    assert "CSV: output/2026-09-21_us.csv" in text


def test_summary_keeps_the_universe_count_without_the_word(criteria: Criteria) -> None:
    """母数は残すが「ユニバース」という語は使わない。"""
    text = "\n".join(lines_for(criteria))
    assert "ユニバース" not in text
    assert "3,560銘柄中" in text


def test_the_list_is_not_labelled_as_a_ranking(criteria: Criteria) -> None:
    """並び順はタイミング加点であって評価の順位ではない。「上位」と書かない。"""
    text = "\n".join(lines_for(criteria))
    assert "上位" not in text
    assert "タイミング加点順:" in text


def test_warns_when_price_coverage_is_low(criteria: Criteria) -> None:
    """取りこぼしのせいで候補が少ない可能性を受け手に伝える。"""
    text = "\n".join(lines_for(criteria, price_coverage=0.5))
    assert "⚠️取りこぼしの可能性" in text


def test_no_warning_at_the_threshold(criteria: Criteria) -> None:
    text = "\n".join(lines_for(criteria, price_coverage=0.90))
    assert "⚠️" not in text


def test_lists_every_candidate_up_to_the_limit(criteria: Criteria) -> None:
    """上限ちょうどなら全件載り、省略の断りは出ない。"""
    text = "\n".join(lines_for(criteria, count=20, max_listed=20))
    assert text.count("  AA") == 20
    assert "省略" not in text


def test_lists_the_limit_and_says_how_many_were_dropped(criteria: Criteria) -> None:
    """あふれても上限分は載せる。**そして落とした件数を必ず書く。**"""
    text = "\n".join(lines_for(criteria, count=23, max_listed=20))
    assert text.count("  AA") == 20
    assert "ほか3件は省略。CSV を参照" in text
    # 総数と CSV のパスは残す。それが無いと受け手に打つ手が無い。
    assert "候補 23件" in text
    assert "CSV: output/2026-09-21_us.csv" in text


def test_omits_the_list_when_nothing_passed(criteria: Criteria) -> None:
    text = "\n".join(lines_for(criteria, count=0))
    assert "タイミング加点順:" not in text
    assert "省略" not in text
    assert "候補 0件" in text


def test_no_empty_heading_when_the_limit_is_zero(criteria: Criteria) -> None:
    """max_listed=0 は「一覧を出さない」設定。空の見出しを残さない。"""
    text = "\n".join(lines_for(criteria, count=3, max_listed=0))
    assert "タイミング加点順:" not in text
    assert "ほか3件は省略。CSV を参照" in text


# --- 文字数の上限 -----------------------------------------------------------


def test_message_fits_in_the_limit(criteria: Criteria) -> None:
    lines = lines_for(criteria, count=20, max_listed=20)
    message = build_message(lines, max_chars=120)
    assert len(message) <= 120
    # 行単位で落とす。途中で切ると候補行の途中で終わる。
    assert "…" in message
    assert message.splitlines()[0] == lines[0]


def test_truncation_keeps_the_csv_path(criteria: Criteria) -> None:
    """落とすのは真ん中から。CSV パスは一覧が入らなかったときの唯一の出口なので残す。"""
    lines = lines_for(criteria, count=20, max_listed=20)
    message = build_message(lines, max_chars=120)
    assert message.endswith("CSV: output/2026-09-21_us.csv")


def test_the_whole_list_fits_at_the_configured_limit(criteria: Criteria) -> None:
    """上限20件＋省略の断り・社名込みで max_message_chars(900) に収まる。

    ここが破れると build_message が行を落とし始める。運用値のまま切り詰めが
    起きないことを固定しておく。
    """
    lines = lines_for(criteria, count=25, max_listed=20, price_coverage=0.5)
    message = build_message(lines, max_chars=900)
    assert message == "\n".join(lines)


DRIVE_URL = "https://drive.google.com/file/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456/view?usp=drivesdk"


def test_uploaded_csv_is_linked_by_url(criteria: Criteria) -> None:
    """Drive に上がったら URL を載せる。PVC のパスは claude.ai から読めないので出さない。"""
    lines = lines_for(criteria, csv_url=DRIVE_URL)
    assert lines[-1] == f"CSV: {DRIVE_URL}"
    assert "output/2026-09-21_us.csv" not in "\n".join(lines)


def test_upload_failure_is_written_with_the_local_path(criteria: Criteria) -> None:
    """失敗しても run は落とさない。代わりに毎週の通知で目に入るようにする。"""
    lines = lines_for(criteria, upload_error="x" * 500)
    assert lines[-1].startswith("CSV: output/2026-09-21_us.csv（Drive へのアップロード失敗: ")
    # 理由は長くなりうる（HTTP の本文）ので切る。
    assert len(lines[-1]) < 150


@pytest.mark.parametrize(
    "csv", [{"csv_url": DRIVE_URL}, {"upload_error": "x" * 500}], ids=["url", "failure"]
)
def test_the_whole_list_still_fits_with_the_drive_line(
    criteria: Criteria, csv: dict[str, str]
) -> None:
    """URL や失敗の理由で最終行が伸びても、運用値では切り詰めが起きない。"""
    lines = lines_for(criteria, count=25, max_listed=20, price_coverage=0.5, **csv)
    assert build_message(lines, max_chars=900) == "\n".join(lines)


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
