"""設定の読み込みと検証。

実物の config/*.yaml が読めることと、壊れた設定が黙って通らないことの2点を見る。
閾値そのものの妥当性はここでは扱わない（Phase 4 で通過件数を見て詰める）。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stock_radar.config import (
    ConfigError,
    Criteria,
    Market,
    Threshold,
    load_criteria,
    load_runtime,
    require_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CRITERIA_PATH = REPO_ROOT / "config" / "criteria.yaml"
RUNTIME_PATH = REPO_ROOT / "config" / "runtime.yaml"


# --- 実物の設定ファイル -----------------------------------------------------


def test_shipped_criteria_loads() -> None:
    criteria = load_criteria(CRITERIA_PATH)
    assert criteria.currency(Market.US) == "USD"
    assert criteria.currency(Market.JP) == "JPY"


def test_shipped_runtime_loads() -> None:
    runtime = load_runtime(RUNTIME_PATH)
    # 「週1回完走すればよい」前提で丸1日近い予算を置いている（docs/architecture.md）。
    assert runtime.prices.budget.max_wall_clock_sec > 0
    pauses = runtime.prices.circuit_breaker.pause_sec
    assert pauses[0] < pauses[-1]


def test_shipped_criteria_us_thresholds_match_docs() -> None:
    """米国の閾値が docs/screening-criteria.md の値から静かにずれていないこと。"""
    criteria = load_criteria(CRITERIA_PATH)
    common = criteria.common.for_market(Market.US)
    assert common.market_cap.min == 300_000_000
    assert common.market_cap.max == 8_000_000_000
    assert criteria.track_a.revenue_cagr_3y.min == 0.10
    assert criteria.track_b.gross_margin.min == 0.40


def test_snapshot_round_trips() -> None:
    """criteria_snapshot から同じ条件を復元できること。

    これが崩れると screen_runs に残した記録から「どの閾値で出た結果か」を再現できない。
    """
    criteria = load_criteria(CRITERIA_PATH)
    assert Criteria.model_validate(criteria.snapshot()) == criteria


# --- Threshold --------------------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "value", "expected"),
    [
        (Threshold(min=0.1), 0.1, True),
        (Threshold(min=0.1), 0.09, False),
        # 米国トラックA の営業利益率は「黒字であること」なので 0 ちょうどは通さない。
        (Threshold(min=0.0, exclusive=True), 0.0, False),
        (Threshold(min=0.0, exclusive=True), 0.0001, True),
        (Threshold(max=2.5), 2.5, True),
        (Threshold(max=2.5), 2.6, False),
        (Threshold(min=-0.50, max=-0.20), -0.35, True),
        (Threshold(min=-0.50, max=-0.20), -0.60, False),
        (Threshold(min=-0.50, max=-0.20), -0.10, False),
    ],
)
def test_threshold_contains(threshold: Threshold, value: float, expected: bool) -> None:
    assert threshold.contains(value) is expected


def test_threshold_requires_a_bound() -> None:
    with pytest.raises(ValueError, match="min と max"):
        Threshold()


def test_threshold_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="上回っている"):
        Threshold(min=10, max=1)


def test_threshold_rejects_exclusive_without_min() -> None:
    with pytest.raises(ValueError, match="exclusive"):
        Threshold(max=1, exclusive=True)


# --- 壊れた設定 -------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "criteria.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _patched(path: Path, old: str, new: str) -> str:
    """実物の設定を一箇所だけ壊す。

    置換が空振りしたまま「壊れていない設定を読んで通った」で終わらないよう、
    元の文字列が実在することをここで確かめる。
    """
    text = path.read_text(encoding="utf-8")
    assert old in text, f"置換対象が見つからない: {old!r}"
    return text.replace(old, new)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="読めない"):
        load_criteria(tmp_path / "nope.yaml")


def test_broken_yaml_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "common: [unclosed\n")
    with pytest.raises(ConfigError, match="YAML として解釈できない"):
        load_criteria(path)


def test_non_mapping_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "- a\n- b\n")
    with pytest.raises(ConfigError, match="マッピング"):
        load_criteria(path)


def test_missing_key_is_reported(tmp_path: Path) -> None:
    text = _patched(CRITERIA_PATH, "  pbr: { max: 2.5 }              # B/M >= 0.4\n", "")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match=r"track_a\.pbr"):
        load_criteria(path)


def test_unknown_key_is_reported(tmp_path: Path) -> None:
    """綴り間違いを黙って無視すると、閾値を変えたつもりで変わっていない事故になる。"""
    text = _patched(CRITERIA_PATH, "  psr: { max: 8.0 }", "  psr_typo: { max: 8.0 }")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="psr_typo"):
        load_criteria(path)


def test_wrong_type_is_reported(tmp_path: Path) -> None:
    text = _patched(CRITERIA_PATH, "  psr: { max: 8.0 }", "  psr: { max: とても高い }")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match=r"track_b\.psr\.max"):
        load_criteria(path)


def test_inverted_threshold_is_reported(tmp_path: Path) -> None:
    text = _patched(
        CRITERIA_PATH,
        "  drawdown_from_52w_high: { min: -0.50, max: -0.20 }",
        "  drawdown_from_52w_high: { min: -0.20, max: -0.50 }",
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="上回っている"):
        load_criteria(path)


def test_market_without_common_thresholds_is_reported(tmp_path: Path) -> None:
    """markets にある市場の閾値を書き忘れたら落とす。

    宣言だけして閾値が無い市場を通すと、Phase 4 でその市場を指定したときに
    実行時まで気づけない。
    """
    text = _patched(
        CRITERIA_PATH,
        "    jp:\n"
        "      # 岡三は 50億〜500億円。上限は見解\n"
        "      market_cap: { min: 5_000_000_000, max: 100_000_000_000 }\n"
        "      avg_daily_value: { min: 50_000_000 }  # 見解\n",
        "",
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match=r"common\.by_market に市場 jp"):
        load_criteria(path)


def test_unknown_market_name_is_reported(tmp_path: Path) -> None:
    text = _patched(CRITERIA_PATH, "markets:\n  us:\n", "markets:\n  usa:\n")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="markets"):
        load_criteria(path)


def test_runtime_rejects_descending_pause_sec(tmp_path: Path) -> None:
    text = _patched(
        RUNTIME_PATH, "pause_sec: [1800, 7200, 21600, 43200]", "pause_sec: [43200, 1800]"
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="昇順"):
        load_runtime(path)


def test_runtime_rejects_base_interval_outside_range(tmp_path: Path) -> None:
    text = _patched(RUNTIME_PATH, "base_interval_sec: 3.0", "base_interval_sec: 999.0")
    path = tmp_path / "runtime.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="base_interval_sec"):
        load_runtime(path)


def test_runtime_rejects_message_longer_than_line_limit(tmp_path: Path) -> None:
    """LINE のテキストメッセージは5,000文字が上限。"""
    text = _patched(RUNTIME_PATH, "max_message_chars: 900", "max_message_chars: 9000")
    path = tmp_path / "runtime.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_message_chars"):
        load_runtime(path)


# --- 環境変数 ---------------------------------------------------------------


def test_require_env_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCK_RADAR_TEST_VAR", "value")
    assert require_env("STOCK_RADAR_TEST_VAR") == "value"


@pytest.mark.parametrize("value", [None, ""])
def test_require_env_rejects_unset_or_empty(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("STOCK_RADAR_TEST_VAR", raising=False)
    else:
        monkeypatch.setenv("STOCK_RADAR_TEST_VAR", value)
    with pytest.raises(ConfigError, match="STOCK_RADAR_TEST_VAR"):
        require_env("STOCK_RADAR_TEST_VAR")
