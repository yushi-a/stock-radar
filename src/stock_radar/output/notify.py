"""notificator への通知。

`docs/architecture.md` の「通知の設計」を実地確認した結果に基づく。

## Connect プロトコルの JSON を素の HTTP POST で叩く

サーバは Connect-go のハンドラを h2c で :50051 に立てており、Connect / gRPC /
gRPC-Web を同一ポートで受ける。**Connect の unary + JSON は HTTP/1.1 の普通の POST**
なので `httpx` だけで完結する。

```
POST {NOTIFICATOR_ADDRESS}/yuxsr.notification.v1.NotificatorService/Notify
Content-Type: application/json

{"message": "..."}
```

`grpcio` / `protobuf` への依存も、proto からのコード生成も要らない。`yuxsr-dev-pb` は
Go と TypeScript しか生成しておらず、gRPC で話そうとすると自前でコード生成基盤を持つことになる。

## 送れるのは短い文字列1つだけ

`NotifyRequest` のフィールドは `message`（string）のみで、バックエンドは LINE Bot の
push message。CSV は添付できないので Drive に上げて URL を載せる（`output/drive.py`）。
アップロードに失敗したら PVC 上のパスと失敗の理由を載せる。

## 候補一覧は `max_listed` 件まで載せ、省略したら省略したと書く

LINE に流せる長さの都合で全件は載らないことがある。`notify.max_listed` 件までを載せ、
**あふれた分は「省略した」と1行書いてから落とす。** 黙って切ると、届いた一覧が候補の
全部だと読めてしまう。

見出しは「上位」ではなく「タイミング加点順」。並びはタイミング加点（`screen/timing.py`）
の順であって銘柄の優劣ではないので、順位として読ませない。

## 失敗しても run を落とさない

通知が届かなくても CSV が出ていれば運用は続けられる。例外を投げず、結果を返して
呼び出し側がログに出す。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from stock_radar.config import NotifyRuntime, require_env
from stock_radar.screen.evaluate import Evaluation

__all__ = [
    "NotifyResult",
    "Poster",
    "build_message",
    "httpx_poster",
    "notify",
    "summary_lines",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """送信の結果。**失敗しても例外にしない。**"""

    sent: bool
    message: str
    error: str | None = None


class Poster(Protocol):
    """HTTP POST の差し替え口。

    テストで実際に送らないために要る（`docs/testing.md`「HTTP 層は差し替え可能にする」）。
    実際の疎通はクラスタ内でしか確かめられないので、ゲート G4 として Phase 6-4 にある。
    """

    def __call__(self, url: str, *, json_body: dict[str, str], timeout_sec: float) -> int:
        """ステータスコードを返す。"""
        ...


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def summary_lines(
    *,
    run_at: dt.datetime,
    market: str,
    universe_size: int,
    track_counts: dict[str, int],
    passed: Sequence[Evaluation],
    price_coverage: float | None,
    coverage_warn_threshold: float,
    csv_path: str | None,
    max_listed: int,
    csv_url: str | None = None,
    upload_error: str | None = None,
) -> list[str]:
    """通知に載せる行。**判断はしない**（スコアも期待倍率も出さない。CLAUDE.md）。"""
    tracks = " / ".join(f"{name}:{count}" for name, count in sorted(track_counts.items()))
    lines = [
        f"stock-radar {run_at:%Y-%m-%d} ({market})",
        f"候補 {len(passed)}件（{tracks}）/ {universe_size:,}銘柄中",
    ]
    if price_coverage is not None:
        line = f"株価取得 {_percent(price_coverage)}"
        if price_coverage < coverage_warn_threshold:
            # これが無いと「候補が少ない」のが相場のせいか取りこぼしのせいか受け手に分からない。
            line += " ⚠️取りこぼしの可能性"
        lines.append(line)
    if passed:
        listed = passed[:max_listed]
        if listed:
            # 見出しで並び順を明示する。「上位」と書くと評価の順位に読めるが、
            # 実際はタイミング加点（screen/timing.py）の順であって優劣ではない。
            lines.append("タイミング加点順:")
            for item in listed:
                candidate = item.candidate
                track = item.track.value if item.track is not None else "-"
                lines.append(f"  {candidate.ticker} ({track}) {(candidate.name or '')[:20]}")
        omitted = len(passed) - len(listed)
        if omitted > 0:
            # 黙って切ると、届いた一覧が候補の全部だと読めてしまう。
            lines.append(f"ほか{omitted}件は省略。CSV を参照")
    if csv_url:
        lines.append(f"CSV: {csv_url}")
    elif csv_path and upload_error:
        # 失敗は毎週の通知で目に入るようにする。トークン失効は放っておいても直らない。
        lines.append(f"CSV: {csv_path}（Drive へのアップロード失敗: {upload_error[:80]}）")
    elif csv_path:
        lines.append(f"CSV: {csv_path}")
    return lines


def build_message(lines: Sequence[str], *, max_chars: int) -> str:
    """行をつないで、上限に収める。

    LINE のテキストは5,000文字が上限だが、実用上は数百文字（`notify.max_message_chars`）。
    **切るのは行単位で。** 途中で文字を切ると「タイミング加点順:」の途中で終わる。

    落とすのは**真ん中から**。先頭行（実行日と市場）と最終行は残す。一覧を省略したときの
    CSV パスが通知の主役になるので、末尾から素直に落とすと真っ先にそれを捨ててしまう。
    max_listed を上げれば候補行が伸びて上限に届きうるので、届いたときに何が残るかを
    ここで決めておく。
    """
    message = "\n".join(lines)
    if len(message) <= max_chars:
        return message
    if len(lines) <= 2:
        # 先頭と最終しか無いなら落とす余地が無い。素直に文字で切る。
        return message[:max_chars]
    first, last = lines[0], lines[-1]
    middle = list(lines[1:-1])
    while middle and len("\n".join([first, *middle, "…", last])) > max_chars:
        middle.pop()
    kept = "\n".join([first, *middle, "…", last])
    # 先頭・省略記号・最終行だけでも入らない設定。そのときは素直に文字で切る。
    return kept if len(kept) <= max_chars else message[:max_chars]


def notify(message: str, runtime: NotifyRuntime, *, poster: Poster) -> NotifyResult:
    """notificator に送る。**例外を投げない。**

    宛先は環境変数（`notify.address_env`）。認証は無い（クラスタ内通信で、proto にも
    notificator の実装にも認証の要素が無い）。
    """
    try:
        address = require_env(runtime.address_env)
    except Exception as exc:  # 環境変数が無いだけで run を落とさない
        return NotifyResult(sent=False, message=message, error=str(exc))

    url = address.rstrip("/") + runtime.path
    try:
        status = poster(url, json_body={"message": message}, timeout_sec=runtime.timeout_sec)
    except Exception as exc:
        return NotifyResult(sent=False, message=message, error=f"{type(exc).__name__}: {exc}")
    if status != 200:
        return NotifyResult(sent=False, message=message, error=f"HTTP {status}")
    return NotifyResult(sent=True, message=message)


def httpx_poster(url: str, *, json_body: dict[str, str], timeout_sec: float) -> int:
    """実際に送る `Poster`。依存は httpx だけ。"""
    import httpx

    response = httpx.post(
        url,
        content=json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=timeout_sec,
    )
    return response.status_code
