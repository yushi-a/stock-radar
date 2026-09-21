"""共通足切り → トラックA / トラックB → タイミング加点、を1銘柄に対して通す。

`docs/architecture.md` のパイプライン④に当たる層。ここも**純粋関数**で、
DuckDB との受け渡しは `screen/runner.py` が持つ。

判定の結果は「通ったか」だけでなく**当てた条件すべて**を残す。
`screen_results.passed_filters`（通過理由）と、落ちた理由の内訳の両方が要るため。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stock_radar.config import Criteria
from stock_radar.screen import timing, track_a, track_b
from stock_radar.screen.filters import Candidate, Check, Verdict, common_checks

__all__ = ["Evaluation", "Track", "evaluate", "undecidable_count"]


class Track(StrEnum):
    """`screen_results.track` に入る値。"""

    A = "A"
    B = "B"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """1銘柄ぶんの判定結果。"""

    candidate: Candidate
    common: tuple[Check, ...]
    track_a: tuple[Check, ...]
    track_b: tuple[Check, ...]
    timing: tuple[Check, ...]
    tracks: tuple[Track, ...]
    timing_score: float | None

    @property
    def passed(self) -> bool:
        return bool(self.tracks)

    @property
    def track(self) -> Track | None:
        """代表トラック。両方通ったら A を採る。

        `screen_results` は1銘柄1行で `track` は1つしか持てない。実証研究の
        裏付けがあるのは A（Yartseva）で、B は見解ベースなので A を優先する。
        両方通ったことは `passed_filters` に両方の印が入るので失われない。
        """
        return self.tracks[0] if self.tracks else None

    @property
    def checks(self) -> tuple[Check, ...]:
        return self.common + self.track_a + self.track_b + self.timing

    @property
    def passed_filters(self) -> tuple[str, ...]:
        """満たした条件の名前。通過理由の記録（`screen_results.passed_filters`）。

        先頭に通過トラックの印（``track_a`` / ``track_b``）を置く。
        個別の条件名は ``track_a.pbr`` のようにドットが付くので混ざらない。
        """
        marks = tuple(f"track_{track.value.lower()}" for track in self.tracks)
        return marks + tuple(item.name for item in self.checks if item.passed)

    @property
    def blocking(self) -> tuple[Check, ...]:
        """落ちた理由。通過していれば空。

        共通足切りで落ちていればそれだけを返す。トラックまで進んでいれば、
        A と B の**両方**の落ちた条件を返す。「B の粗利率さえ取れていれば通った」
        のような分布を数えるのに要る（ゲート G3 の判断材料）。
        """
        if self.passed:
            return ()
        blocked = tuple(item for item in self.common if not item.passed)
        if blocked:
            return blocked
        return tuple(item for item in self.track_a + self.track_b if not item.passed)

    def value_of(self, name: str) -> float | None:
        """条件名で判定に使った値を引く。CSV と `screen_results` の列埋めに使う。"""
        for item in self.checks:
            if item.name == name:
                return item.value
        return None


def evaluate(candidate: Candidate, criteria: Criteria, *, with_price: bool = True) -> Evaluation:
    """1銘柄を判定する。

    ``with_price=False`` のときは株価が要る条件（時価総額・売買代金・PBR・PSR・
    FCF利回り）を当てない。**株価取得の前に財務だけで足切りする**ためのモードで、
    CLAUDE.md の「先に対象を半減させることが 429 対策の中核」がこれに当たる。
    タイミング加点は株価そのものなので、このモードでは常に判定不能・スコア無しになる。
    """
    common = common_checks(candidate, criteria, with_price=with_price)
    a = track_a.evaluate(candidate, criteria, with_price=with_price)
    b = track_b.evaluate(candidate, criteria, with_price=with_price)
    bonus = timing.evaluate(candidate, criteria)

    passed_common = all(item.passed for item in common)
    tracks: list[Track] = []
    if passed_common:
        if all(item.passed for item in a):
            tracks.append(Track.A)
        if all(item.passed for item in b):
            tracks.append(Track.B)

    return Evaluation(
        candidate=candidate,
        common=tuple(common),
        track_a=tuple(a),
        track_b=tuple(b),
        timing=tuple(bonus),
        tracks=tuple(tracks),
        timing_score=timing.score(bonus, has_prices=candidate.quotes is not None),
    )


def undecidable_count(evaluations: list[Evaluation]) -> dict[str, int]:
    """条件ごとの「判定不能で落ちた」件数。

    閾値を緩めても増えないのがここ。緩めて増えるのは ``Verdict.FAIL`` の方なので、
    調整の前に両者を分けて数える（docs/xbrl-findings.md の「欠測の性質は2種類ある」）。
    """
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        for item in evaluation.checks:
            if item.verdict is Verdict.UNKNOWN:
                counts[item.name] = counts.get(item.name, 0) + 1
    return counts
