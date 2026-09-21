"""株価取得の「待ち方」。

方針は **429 から回復するのではなく、429 を踏まない**（docs/architecture.md）。
週1回完走すればよく、1回に丸1日かけても構わないので、2,000リクエストに24時間を
割り当てれば1件あたり43秒まで許容できる。速度の余裕は桁違いにある。

**待ち時間の「計算」と「実際に待つ」を分けてある**（docs/testing.md）。
この層は純粋関数だけで、`sleep` を呼ばない。おかげでテストは一度も待たずに済み、
時間予算のような実時間依存のロジックもフレーキーにならない。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from stock_radar.config import CircuitBreakerRuntime, ThrottleRuntime

__all__ = [
    "PaceState",
    "circuit_pause_sec",
    "next_interval_sec",
    "should_stop",
    "sleep_seconds",
]


@dataclass(frozen=True, slots=True)
class PaceState:
    """いまのペース。取得ループが1銘柄ごとに持ち回る。"""

    interval_sec: float
    consecutive_successes: int = 0
    consecutive_rate_limits: int = 0
    circuit_trips: int = 0

    @classmethod
    def initial(cls, throttle: ThrottleRuntime) -> PaceState:
        return cls(interval_sec=throttle.base_interval_sec)


def next_interval_sec(
    state: PaceState, *, rate_limited: bool, throttle: ThrottleRuntime
) -> PaceState:
    """1件ぶんの結果を受けて、次のペースを決める。

    429 を受けたら**そのリクエストだけリトライするのではなく、全体の間隔を倍にする**。
    Yahoo のレート制限は IP 単位で粘着的なため、1件だけ待って再開するとすぐまた踏む。
    全体のペースを落とす方が結果的に速く終わる。

    連続成功が続いたら少しずつ戻す。下限を割らない。
    """
    if rate_limited:
        widened = min(state.interval_sec * throttle.backoff_multiplier, throttle.max_interval_sec)
        return replace(
            state,
            interval_sec=widened,
            consecutive_successes=0,
            consecutive_rate_limits=state.consecutive_rate_limits + 1,
        )

    successes = state.consecutive_successes + 1
    if successes < throttle.recovery_after_successes:
        return replace(state, consecutive_successes=successes, consecutive_rate_limits=0)

    narrowed = max(state.interval_sec * throttle.recovery_factor, throttle.min_interval_sec)
    return replace(state, interval_sec=narrowed, consecutive_successes=0, consecutive_rate_limits=0)


def circuit_pause_sec(
    state: PaceState, breaker: CircuitBreakerRuntime
) -> tuple[float, PaceState] | None:
    """サーキットブレーカー。休むべきなら（秒数, 次の状態）を返す。

    適応制御でも収まらない場合の段階的退避。IP 単位のペナルティは数時間で解ける
    性質のものなので、短いリトライを繰り返すより長く待つ方が有効。
    丸1日使える前提なら6時間寝てから再開しても間に合う。

    段階は `pause_sec` の順に進み、最後の値で頭打ちにする。
    """
    if state.consecutive_rate_limits < breaker.consecutive_429_threshold:
        return None
    step = min(state.circuit_trips, len(breaker.pause_sec) - 1)
    pause = float(breaker.pause_sec[step])
    return pause, replace(state, consecutive_rate_limits=0, circuit_trips=state.circuit_trips + 1)


def sleep_seconds(state: PaceState, throttle: ThrottleRuntime, jitter: float) -> float:
    """次のリクエストまで待つ秒数。

    ``jitter`` は 0.0〜1.0 の乱数を呼び出し側から渡す。乱数生成をここに入れると
    純粋関数でなくなり、値を assert できなくなる。
    """
    if not 0.0 <= jitter <= 1.0:
        raise ValueError(f"jitter は 0.0〜1.0: {jitter}")
    return state.interval_sec + throttle.jitter_sec * jitter


def should_stop(elapsed_sec: float, budget_sec: float) -> bool:
    """時間予算を使い切ったか。

    超過したら取得を打ち切り、その時点のデータで以降の工程を走らせる。
    差分更新なので取れなかった分は次回の run に持ち越される。

    実測で試すとフレーキーになる（設定を0.05秒にしても CI の負荷次第で揺れる）ので、
    経過秒を引数に取る純粋関数にしてある。
    """
    if elapsed_sec < 0:
        raise ValueError(f"経過秒が負になっている: {elapsed_sec}")
    return elapsed_sec >= budget_sec
