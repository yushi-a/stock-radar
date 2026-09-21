"""株価取得の待ち方。**一度も待たずに検証する**（docs/testing.md）。

待ち時間の計算を純粋関数に切り出してあるので、実時間に依存しない。
時間予算を実測で試すと、設定を0.05秒にしても CI の負荷次第で結果が揺れる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stock_radar.config import CircuitBreakerRuntime, ThrottleRuntime, load_runtime
from stock_radar.sources.prices.base import ErrorClass
from stock_radar.sources.prices.throttle import (
    PaceState,
    circuit_pause_sec,
    next_interval_sec,
    should_stop,
    sleep_seconds,
)

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.yaml"


@pytest.fixture
def throttle() -> ThrottleRuntime:
    return load_runtime(RUNTIME_PATH).prices.throttle


@pytest.fixture
def breaker() -> CircuitBreakerRuntime:
    return load_runtime(RUNTIME_PATH).prices.circuit_breaker


# --- 429 を受けたときのペース -----------------------------------------------


def test_rate_limit_doubles_the_whole_interval(throttle: ThrottleRuntime) -> None:
    """そのリクエストだけリトライするのではなく、全体の間隔を倍にする。

    Yahoo のレート制限は IP 単位で粘着的なので、1件だけ待って再開するとすぐまた踏む。
    """
    state = PaceState.initial(throttle)
    widened = next_interval_sec(state, rate_limited=True, throttle=throttle)
    assert widened.interval_sec == state.interval_sec * throttle.backoff_multiplier
    assert widened.consecutive_rate_limits == 1


def test_repeated_rate_limits_stop_at_the_ceiling(throttle: ThrottleRuntime) -> None:
    state = PaceState.initial(throttle)
    for _ in range(20):
        state = next_interval_sec(state, rate_limited=True, throttle=throttle)
    assert state.interval_sec == throttle.max_interval_sec


def test_a_success_resets_the_rate_limit_streak(throttle: ThrottleRuntime) -> None:
    state = next_interval_sec(PaceState.initial(throttle), rate_limited=True, throttle=throttle)
    recovered = next_interval_sec(state, rate_limited=False, throttle=throttle)
    assert recovered.consecutive_rate_limits == 0


# --- 連続成功で戻す ---------------------------------------------------------


def test_interval_does_not_narrow_before_enough_successes(throttle: ThrottleRuntime) -> None:
    state = PaceState(interval_sec=60.0)
    for _ in range(throttle.recovery_after_successes - 1):
        state = next_interval_sec(state, rate_limited=False, throttle=throttle)
    assert state.interval_sec == 60.0


def test_interval_narrows_after_enough_successes(throttle: ThrottleRuntime) -> None:
    state = PaceState(interval_sec=60.0)
    for _ in range(throttle.recovery_after_successes):
        state = next_interval_sec(state, rate_limited=False, throttle=throttle)
    assert state.interval_sec == pytest.approx(60.0 * throttle.recovery_factor)
    assert state.consecutive_successes == 0


def test_interval_never_goes_below_the_floor(throttle: ThrottleRuntime) -> None:
    state = PaceState(interval_sec=throttle.min_interval_sec)
    for _ in range(throttle.recovery_after_successes * 5):
        state = next_interval_sec(state, rate_limited=False, throttle=throttle)
    assert state.interval_sec == throttle.min_interval_sec


# --- サーキットブレーカー ---------------------------------------------------


def test_breaker_stays_closed_below_the_threshold(
    throttle: ThrottleRuntime, breaker: CircuitBreakerRuntime
) -> None:
    state = PaceState(
        interval_sec=throttle.base_interval_sec,
        consecutive_rate_limits=breaker.consecutive_429_threshold - 1,
    )
    assert circuit_pause_sec(state, breaker) is None


def test_breaker_steps_through_the_configured_pauses(
    throttle: ThrottleRuntime, breaker: CircuitBreakerRuntime
) -> None:
    """30分 → 2h → 6h → 12h と伸びること。"""
    state = PaceState(interval_sec=throttle.base_interval_sec)
    seen = []
    for _ in range(len(breaker.pause_sec)):
        state = PaceState(
            interval_sec=state.interval_sec,
            consecutive_rate_limits=breaker.consecutive_429_threshold,
            circuit_trips=state.circuit_trips,
        )
        result = circuit_pause_sec(state, breaker)
        assert result is not None
        pause, state = result
        seen.append(pause)
    assert seen == [float(x) for x in breaker.pause_sec]


def test_breaker_holds_at_the_longest_pause(
    throttle: ThrottleRuntime, breaker: CircuitBreakerRuntime
) -> None:
    state = PaceState(interval_sec=throttle.base_interval_sec, circuit_trips=99)
    state = PaceState(
        interval_sec=state.interval_sec,
        consecutive_rate_limits=breaker.consecutive_429_threshold,
        circuit_trips=99,
    )
    result = circuit_pause_sec(state, breaker)
    assert result is not None
    assert result[0] == float(breaker.pause_sec[-1])


def test_breaker_clears_the_streak_so_it_does_not_fire_twice(
    throttle: ThrottleRuntime, breaker: CircuitBreakerRuntime
) -> None:
    state = PaceState(
        interval_sec=throttle.base_interval_sec,
        consecutive_rate_limits=breaker.consecutive_429_threshold,
    )
    result = circuit_pause_sec(state, breaker)
    assert result is not None
    assert circuit_pause_sec(result[1], breaker) is None


# --- 実際に待つ秒数 ---------------------------------------------------------


def test_sleep_seconds_adds_jitter(throttle: ThrottleRuntime) -> None:
    state = PaceState(interval_sec=10.0)
    assert sleep_seconds(state, throttle, 0.0) == 10.0
    assert sleep_seconds(state, throttle, 1.0) == 10.0 + throttle.jitter_sec
    assert sleep_seconds(state, throttle, 0.5) == 10.0 + throttle.jitter_sec / 2


def test_sleep_seconds_rejects_a_jitter_outside_the_unit_range(
    throttle: ThrottleRuntime,
) -> None:
    with pytest.raises(ValueError, match="jitter"):
        sleep_seconds(PaceState(interval_sec=10.0), throttle, 1.5)


# --- 時間予算 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("elapsed", "budget", "expected"),
    [(0.0, 72_000.0, False), (71_999.0, 72_000.0, False), (72_000.0, 72_000.0, True)],
)
def test_should_stop(elapsed: float, budget: float, expected: bool) -> None:
    assert should_stop(elapsed, budget) is expected


def test_should_stop_rejects_negative_elapsed() -> None:
    with pytest.raises(ValueError, match="負"):
        should_stop(-1.0, 100.0)


# --- error_class ------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_class", "temporary"),
    [
        (ErrorClass.RATE_LIMITED, True),
        (ErrorClass.OTHER, True),
        # 上場廃止・ティッカー変更の疑い。毎週リトライし続けない。
        (ErrorClass.INVALID_SYMBOL, False),
    ],
)
def test_error_class_temporality(error_class: ErrorClass, temporary: bool) -> None:
    assert error_class.is_temporary is temporary


def test_error_classes_match_the_schema() -> None:
    """`fetch_failures.error_class` の CHECK 制約と同じ集合であること。"""
    from stock_radar.storage import _ERROR_CLASSES

    allowed = {value.strip().strip("'") for value in _ERROR_CLASSES.split(",")}
    assert {e.value for e in ErrorClass} == allowed


# --- 設計の前提 -------------------------------------------------------------


def test_the_pace_can_finish_the_universe_within_the_budget(
    throttle: ThrottleRuntime,
) -> None:
    """既定の間隔で、想定銘柄数を時間予算内に回せること。

    「2,000リクエストに24時間を割り当てれば1件あたり43秒まで許容できる」という
    設計の前提（docs/architecture.md）を、実際の設定値で確かめる。
    """
    runtime = load_runtime(RUNTIME_PATH).prices
    worst_case_per_request = throttle.base_interval_sec + throttle.jitter_sec
    assert 2_000 * worst_case_per_request < runtime.budget.max_wall_clock_sec
