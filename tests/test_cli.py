"""CLI。引数の組み立てと、ファイルを使い回す判断だけを見る。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from stock_radar import cli
from stock_radar.cli import build_parser, submissions_is_fresh
from stock_radar.config import Criteria, Market, Runtime, load_criteria, load_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


def _runtime() -> Runtime:
    return load_runtime(REPO_ROOT / "config" / "runtime.yaml")


def _criteria() -> Criteria:
    return load_criteria(REPO_ROOT / "config" / "criteria.yaml")


def test_defaults_to_the_us_market() -> None:
    args = build_parser().parse_args(["fetch-universe"])
    assert args.market is Market.US
    assert args.force_download is False


def test_rejects_an_unknown_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backtest"])


def test_screen_applies_price_filters_by_default() -> None:
    args = build_parser().parse_args(["screen"])
    assert args.no_price_filters is False
    assert args.dry_run is False


def test_screen_can_skip_price_filters() -> None:
    """株価が揃う前に財務側だけ測るモード（ゲート G3 の実測に使う）。"""
    args = build_parser().parse_args(["screen", "--no-price-filters", "--dry-run"])
    assert args.no_price_filters is True
    assert args.dry_run is True


def test_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- submissions.zip を使い回す判断 -----------------------------------------


def _with_mtime(tmp_path: Path, age: dt.timedelta) -> Path:
    path = tmp_path / "submissions.zip"
    path.write_bytes(b"zip")
    stamp = (dt.datetime.now(dt.UTC) - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_missing_file_is_not_fresh(tmp_path: Path) -> None:
    assert submissions_is_fresh(tmp_path / "nope.zip", 7) is False


def test_recent_file_is_fresh(tmp_path: Path) -> None:
    """1.5GB を毎回落とさないための判断。"""
    assert submissions_is_fresh(_with_mtime(tmp_path, dt.timedelta(days=2)), 7) is True


def test_old_file_is_not_fresh(tmp_path: Path) -> None:
    assert submissions_is_fresh(_with_mtime(tmp_path, dt.timedelta(days=8)), 7) is False


def test_zero_max_age_always_refetches(tmp_path: Path) -> None:
    assert submissions_is_fresh(_with_mtime(tmp_path, dt.timedelta(minutes=1)), 0) is False


# --- run（全工程を一息で回す） ----------------------------------------------


def test_run_takes_the_price_scoping_options() -> None:
    args = build_parser().parse_args(["run", "--limit", "10", "--no-notify"])
    assert (args.limit, args.tickers, args.no_notify) == (10, None, True)


def test_run_stops_at_the_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """半端なデータで候補を出さない。失敗したら後続を呼ばない。"""
    called: list[str] = []

    def ok(name: str) -> object:
        def step(*args: object, **kwargs: object) -> int:
            called.append(name)
            return 0

        return step

    def fails(*args: object, **kwargs: object) -> int:
        called.append("facts")
        return 1

    monkeypatch.setattr(cli, "fetch_universe", ok("universe"))
    monkeypatch.setattr(cli, "fetch_facts", fails)
    monkeypatch.setattr(cli, "fetch_prices_command", ok("prices"))
    monkeypatch.setattr(cli, "screen_command", ok("screen"))

    code = cli.run_all(_runtime(), _criteria(), Path("x.duckdb"), Market.US)
    assert code == 1
    assert called == ["universe", "facts"]


def test_run_keeps_the_pipeline_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """株価取得は必ず財務の足切りの後（CLAUDE.md）。"""
    called: list[str] = []

    for name in ("fetch_universe", "fetch_facts", "fetch_prices_command", "screen_command"):
        monkeypatch.setattr(
            cli,
            name,
            lambda *args, _name=name, **kwargs: (called.append(_name), 0)[1],
        )

    assert cli.run_all(_runtime(), _criteria(), Path("x.duckdb"), Market.US) == 0
    assert called == [
        "fetch_universe",
        "fetch_facts",
        "fetch_prices_command",
        "screen_command",
    ]
