"""CLI。引数の組み立てと、ファイルを使い回す判断だけを見る。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from stock_radar.cli import build_parser, submissions_is_fresh
from stock_radar.config import Market


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
