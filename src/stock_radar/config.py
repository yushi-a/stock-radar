"""設定ファイルの読み込みと型付け。

閾値をコードに直書きしないための土台（CLAUDE.md）。ファイルは2つに分かれている。

- ``config/criteria.yaml`` — 判定条件。``screen_runs.criteria_snapshot`` にそのまま記録する
- ``config/runtime.yaml``  — 運用パラメータ（スロットリング、時間予算など）

分けている理由は docs/architecture.md の「運用パラメータは config/runtime.yaml に
外出しする」を参照。混ぜると「閾値を変えていないのに snapshot が変わる」状態になる。
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "ConfigError",
    "Criteria",
    "Market",
    "Runtime",
    "Threshold",
    "load_criteria",
    "load_runtime",
    "require_env",
]

DEFAULT_CRITERIA_PATH = Path("config/criteria.yaml")
DEFAULT_RUNTIME_PATH = Path("config/runtime.yaml")


class ConfigError(Exception):
    """設定ファイルが読めない、または内容が不正。"""


class Market(StrEnum):
    US = "us"
    JP = "jp"


class _Model(BaseModel):
    """設定モデルの共通の作法。

    ``extra="forbid"`` が要。キーの綴りを間違えたまま黙って無視されると、
    閾値を変えたつもりで変わっていない状態で候補リストが出る。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- criteria.yaml ---------------------------------------------------------


class Threshold(_Model):
    """下限・上限。片側だけでもよい。"""

    min: float | None = None
    max: float | None = None
    # min を境界として含まない（``>`` であって ``>=`` ではない）。
    # 米国トラックA の営業利益率「黒字であること」がこれに当たる。
    exclusive: bool = False

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("min と max の少なくとも一方が必要")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) が max ({self.max}) を上回っている")
        if self.exclusive and self.min is None:
            raise ValueError("exclusive は min と一緒にしか使えない")
        return self

    def contains(self, value: float) -> bool:
        """``value`` が閾値の範囲に収まるか。"""
        if self.min is not None:
            if self.exclusive:
                if value <= self.min:
                    return False
            elif value < self.min:
                return False
        return not (self.max is not None and value > self.max)


class MarketInfo(_Model):
    # 金額の閾値がどちらの通貨で書かれているかを示す。
    currency: str = Field(min_length=3, max_length=3)


class MarketLimits(_Model):
    market_cap: Threshold
    avg_daily_value: Threshold


class ByMarketThreshold(_Model):
    """市場ごとに値が違う閾値。"""

    by_market: dict[Market, Threshold]

    def for_market(self, market: Market) -> Threshold:
        try:
            return self.by_market[market]
        except KeyError:
            raise ConfigError(f"市場 {market} の閾値が定義されていない") from None


class CommonCriteria(_Model):
    by_market: dict[Market, MarketLimits]
    equity_positive: bool
    revenue_positive: bool
    exclude_sectors: list[str] = Field(min_length=1)
    us_exclude_adr: bool

    def for_market(self, market: Market) -> MarketLimits:
        try:
            return self.by_market[market]
        except KeyError:
            raise ConfigError(f"市場 {market} の共通足切りが定義されていない") from None


class ProfitabilityAny(_Model):
    """どちらか一方を満たせばよい。"""

    roa: Threshold
    roe: Threshold


class TrackACriteria(_Model):
    """黒字・割安型（Yartseva 準拠）。"""

    revenue_cagr_3y: Threshold
    op_margin: ByMarketThreshold
    fcf_yield: Threshold
    pbr: Threshold
    profitability_any: ProfitabilityAny
    # 第一弾は EBITDA ではなく EBIT で代用する（docs/architecture.md）。
    asset_growth_minus_ebit_growth: Threshold


class FinancialBufferAny(_Model):
    """どちらか一方を満たせばよい。"""

    equity_ratio: Threshold
    current_ratio: Threshold


class TrackBCriteria(_Model):
    """高成長型（赤字許容、見解ベース）。"""

    revenue_growth_yoy: Threshold
    revenue_growth_latest_quarter_yoy: Threshold
    op_margin: Threshold
    gross_margin: Threshold
    psr: Threshold
    financial_buffer_any: FinancialBufferAny


class TimingBonus(_Model):
    """並べ替え用の加点。ハードフィルタにはしない。"""

    drawdown_from_52w_high: Threshold
    range_position_52w: Threshold
    jp_years_since_listing: Threshold
    jp_owner_in_top3_holders: bool


class Criteria(_Model):
    markets: dict[Market, MarketInfo]
    common: CommonCriteria
    track_a: TrackACriteria
    track_b: TrackBCriteria
    timing_bonus: TimingBonus

    @model_validator(mode="after")
    def _validate_markets(self) -> Self:
        known = set(self.markets)
        for label, defined in (
            ("common.by_market", set(self.common.by_market)),
            ("track_a.op_margin.by_market", set(self.track_a.op_margin.by_market)),
        ):
            missing = known - defined
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(f"{label} に市場 {names} の定義が無い")
        return self

    def currency(self, market: Market) -> str:
        try:
            return self.markets[market].currency
        except KeyError:
            raise ConfigError(f"市場 {market} が markets に無い") from None

    def snapshot(self) -> dict[str, Any]:
        """``screen_runs.criteria_snapshot`` に記録する形。

        ファイルの生テキストではなく、既定値の補完まで含めた「実際に使われた値」を書き出す。
        そうしないと「この結果はどの閾値で出たか」を後から再現できない。
        """
        return self.model_dump(mode="json")


# --- runtime.yaml ----------------------------------------------------------


class SecRuntime(_Model):
    # User-Agent の実際の値は環境変数で渡す。連絡先なので設定ファイルには置かない。
    user_agent_env: str = Field(min_length=1)
    raw_dir: Path
    # companyfacts.zip は1ファイル1GB超。週次で世代を残すと年50GBを超える。
    keep_generations: int = Field(ge=1)
    # SEC の fair access は 10 リクエスト/秒。余裕を見てそれを下回る間隔で回す。
    min_request_interval_sec: float = Field(gt=0)
    max_attempts: int = Field(ge=1)
    retry_backoff_sec: float = Field(ge=0)


class ThrottleRuntime(_Model):
    base_interval_sec: float = Field(gt=0)
    jitter_sec: float = Field(ge=0)
    min_interval_sec: float = Field(gt=0)
    max_interval_sec: float = Field(gt=0)
    # 429 を受けたら、そのリクエストだけリトライせず全体の間隔を倍にする。
    backoff_multiplier: float = Field(gt=1)
    recovery_after_successes: int = Field(ge=1)
    recovery_factor: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _validate_interval_range(self) -> Self:
        if self.min_interval_sec > self.max_interval_sec:
            raise ValueError(
                f"min_interval_sec ({self.min_interval_sec}) が "
                f"max_interval_sec ({self.max_interval_sec}) を上回っている"
            )
        if not (self.min_interval_sec <= self.base_interval_sec <= self.max_interval_sec):
            raise ValueError(
                f"base_interval_sec ({self.base_interval_sec}) が "
                f"[{self.min_interval_sec}, {self.max_interval_sec}] の外にある"
            )
        return self


class CircuitBreakerRuntime(_Model):
    consecutive_429_threshold: int = Field(ge=1)
    # 段階的に伸ばす休止時間。長くなる順に並べる。
    pause_sec: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_pauses(self) -> Self:
        if any(sec <= 0 for sec in self.pause_sec):
            raise ValueError("pause_sec は正の値でなければならない")
        if list(self.pause_sec) != sorted(self.pause_sec):
            raise ValueError("pause_sec は昇順（段階的に長くなる順）で並べる")
        return self


class BudgetRuntime(_Model):
    max_wall_clock_sec: int = Field(gt=0)
    # 超過したら取得を打ち切り、その時点のデータで以降の工程を走らせる。
    on_exceeded: Literal["continue_next_run"]


class RetryRuntime(_Model):
    max_attempts_per_run: int = Field(ge=1)
    # 恒久的失敗（上場廃止・ティッカー変更の疑い）と判定するまでの連続回数。
    permanent_error_threshold: int = Field(ge=1)


class PricesRuntime(_Model):
    window_days: int = Field(gt=0)
    # 遡及調整（株式分割）の検知に使う重複日数。
    overlap_days: int = Field(ge=0)
    throttle: ThrottleRuntime
    circuit_breaker: CircuitBreakerRuntime
    budget: BudgetRuntime
    retry: RetryRuntime
    coverage_warn_threshold: float = Field(ge=0, le=1)


class OutputRuntime(_Model):
    csv_dir: Path


class NotifyRuntime(_Model):
    address_env: str = Field(min_length=1)
    path: str = Field(min_length=1)
    timeout_sec: float = Field(gt=0)
    # LINE のテキストメッセージは5,000文字が上限。実用上は数百文字に収める。
    max_message_chars: int = Field(gt=0, le=5000)
    top_n: int = Field(ge=0)


class Runtime(_Model):
    sec: SecRuntime
    prices: PricesRuntime
    output: OutputRuntime
    notify: NotifyRuntime


# --- 読み込み ---------------------------------------------------------------


def load_criteria(path: Path | str = DEFAULT_CRITERIA_PATH) -> Criteria:
    """判定条件を読み込む。"""
    return _build(Criteria, path)


def load_runtime(path: Path | str = DEFAULT_RUNTIME_PATH) -> Runtime:
    """運用パラメータを読み込む。"""
    return _build(Runtime, path)


def require_env(name: str) -> str:
    """環境変数を取り出す。未設定なら落とす。

    SEC の User-Agent のように、無いまま走らせると外部に迷惑をかける値に使う。
    """
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"環境変数 {name} が設定されていない（.env.example を参照）")
    return value


def _build[M: _Model](model: type[M], path: Path | str) -> M:
    path = Path(path)
    data = _read_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path} の内容が不正:\n{_format_errors(exc)}") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"設定ファイルを読めない: {path} ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML として解釈できない: {path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"最上位がマッピングになっていない: {path}")
    return data


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(最上位)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)
