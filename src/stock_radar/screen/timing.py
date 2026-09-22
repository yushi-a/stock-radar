"""タイミング加点。**除外ではなく並べ替えに使う。**

株価の下落が「割安になった」のか「業績が壊れた」のかは株価だけでは区別できない。
ハードフィルタにすると後者を掴むので、加点に留める（`docs/screening-criteria.md` の④）。

用途は2つだけ。CSV の並び順と、通知に載せる候補一覧の並び順。

**この順序は評価の順位ではない。** 銘柄の優劣を測っていないので、上位N件だけを
切り出して見せると意味の無い序列を伝えてしまう。通知は全件載せるか一覧ごと省略するかの
二択にしてある（`runtime.yaml` の `notify.max_listed`、`output/notify.py`）。

## 方式：満たした条件の数（🔵 2026-09-21 ユーザー決定）

`docs/open-questions.md` の未決事項だった「スコアリング方法（重み付け）」。
**0〜2 の整数**にする。同点は `range_position_52w` の昇順、つまり52週安値に近い順。
Yartseva の「12ヶ月高値付近は劣後し、12ヶ月安値付近・直近6ヶ月下落後が起点になりやすい」
に沿う並びになる。

連続値の重み付けは採らない。

- **下落率は「深いほど良い」ではなく帯**（−20% に届かないのは高値圏、−50% 超は
  業績悪化の疑い）。連続化すると山形の関数の頂点と裾を根拠なく決めることになる
- 2つの指標は重複が大きい。どちらも「高値からどれだけ離れたか」を見ており、
  独立な情報は安値の位置だけ。重みを分けても順位はほとんど変わらない
- 恣意性を閾値だけに閉じておくと、帯をずらした影響が `criteria_snapshot` から読める

## 日本株向けの項目

`timing_bonus.jp_years_since_listing` と `jp_owner_in_top3_holders` はここでは評価しない。
上場経過年数も大株主情報も第一弾（米国）では取得しておらず、常に判定不能になって
スコアを歪めるだけだから。日本株対応のときに足す。
"""

from __future__ import annotations

from stock_radar.config import Criteria
from stock_radar.screen.filters import Candidate, Check, check

__all__ = ["NO_POSITION", "evaluate", "ranking_key", "score"]

# 並べ替えで、レンジ内位置が取れない銘柄を最後に送るための値。
# レンジ内位置は 0〜1 なので、1 より大きければ何でもよい。
NO_POSITION = 2.0


def evaluate(candidate: Candidate, criteria: Criteria) -> list[Check]:
    """加点対象の条件を当てる。落とす判断には使わない。"""
    bonus = criteria.timing_bonus
    checks = [
        check(
            "timing.drawdown_from_52w_high",
            candidate.drawdown_from_52w_high,
            bonus.drawdown_from_52w_high,
        ),
        check(
            "timing.range_position_52w",
            candidate.range_position_52w,
            bonus.range_position_52w,
        ),
    ]
    return checks


def score(checks: list[Check], *, has_prices: bool) -> float | None:
    """満たした条件の数。株価が無ければ None。

    株価が取れていない銘柄を 0点として並べると「加点条件を満たさなかった」のと
    区別できない。取れなかったことは空欄で示す。
    """
    if not has_prices:
        return None
    return float(sum(1 for item in checks if item.passed))


def ranking_key(
    timing_score: float | None, range_position: float | None, ticker: str
) -> tuple[float, float, str]:
    """並べ替えの鍵。スコア降順 → レンジ内位置の昇順 → ティッカー。

    ティッカーまで入れるのは、同点同値のときに実行のたびに順序が変わらないようにするため。
    """
    return (
        -(timing_score if timing_score is not None else 0.0),
        range_position if range_position is not None else NO_POSITION,
        ticker,
    )
