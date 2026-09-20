"""CLI。Phase 1 の検証を実行するための最小限のサブコマンドだけがある。"""

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
        build_parser().parse_args(["screen"])


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
