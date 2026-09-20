"""コマンドラインの入口。

第一弾の本格的な CLI は Phase 5。ここにあるのは Phase 1 の検証を実行するための
最小限のサブコマンドだけ。
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from stock_radar.config import (
    DEFAULT_RUNTIME_PATH,
    Market,
    Runtime,
    load_runtime,
)
from stock_radar.sources.sec.client import SecClient
from stock_radar.sources.sec.submissions import (
    apply_submission_profiles,
    download_submissions,
    iter_submission_profiles,
    mark_missing_submissions,
)
from stock_radar.sources.sec.universe import (
    exclusion_counts,
    fetch_ticker_rows,
    replace_ticker_rows,
)
from stock_radar.storage import DEFAULT_DB_PATH, open_database

if TYPE_CHECKING:
    import duckdb

log = logging.getLogger("stock_radar")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-radar", description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--market", type=Market, choices=list(Market), default=Market.US)

    sub = parser.add_subparsers(dest="command", required=True)
    universe = sub.add_parser(
        "fetch-universe", help="SEC からユニバースを取得して universe テーブルを作る"
    )
    universe.add_argument(
        "--force-download",
        action="store_true",
        help="submissions.zip が新しくても取り直す",
    )
    return parser


def submissions_is_fresh(path: Path, max_age_days: int, *, now: dt.datetime | None = None) -> bool:
    """手元の submissions.zip を使い回してよいか。

    1.5GB を毎回落とさないための判断。ユニバース（上場・SIC・提出フォーム）は
    週次ではほとんど動かない。
    """
    if not path.exists():
        return False
    moment = now if now is not None else dt.datetime.now(dt.UTC)
    age = moment - dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
    return age <= dt.timedelta(days=max_age_days)


def fetch_universe(runtime: Runtime, db_path: Path, market: Market, *, force: bool) -> int:
    client = SecClient.from_runtime(runtime.sec)

    with open_database(db_path) as con:
        log.info("ティッカー一覧を取得中…")
        rows = fetch_ticker_rows(client)
        log.info(
            "%s 件を universe に書き込み", f"{replace_ticker_rows(con, rows, market=market):,}"
        )

        zip_path = runtime.sec.raw_dir / "submissions.zip"
        if force or not submissions_is_fresh(zip_path, runtime.sec.submissions_max_age_days):
            log.info("submissions.zip を取得中（1.5GB 超）…")
            download_submissions(client, runtime.sec)
        else:
            log.info("手元の submissions.zip を使う（%s）", zip_path)

        wanted = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT cik FROM universe WHERE market = ? AND excluded_reason IS NULL",
                [market.value],
            ).fetchall()
        }
        log.info("%s 社分の SIC / 提出フォームを展開中…", f"{len(wanted):,}")
        profiles = iter_submission_profiles(zip_path, wanted_ciks=wanted)
        apply_submission_profiles(con, profiles, market=market)
        mark_missing_submissions(con, market=market)

        _report(con, market)
    return 0


def _report(con: duckdb.DuckDBPyConnection, market: Market) -> None:
    """Phase 1 の検証項目：除外理由別の件数と残存社数。"""
    counts = exclusion_counts(con, market=market)
    total = sum(counts.values())
    print(f"\n=== ユニバース（{market.value}）===")
    print(f"{'ティッカー行（全体）':<22} {total:>7,}")
    for reason, count in sorted(counts.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
        label = "残存" if reason is None else f"除外: {reason}"
        print(f"{label:<22} {count:>7,}")
    remaining = con.execute(
        "SELECT count(DISTINCT cik) FROM universe WHERE market = ? AND excluded_reason IS NULL",
        [market.value],
    ).fetchone()[0]
    print(f"{'残存ユニーク CIK':<22} {remaining:>7,}")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    runtime = load_runtime(args.runtime)

    if args.command == "fetch-universe":
        return fetch_universe(runtime, args.db, args.market, force=args.force_download)
    raise AssertionError(f"未知のサブコマンド: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
