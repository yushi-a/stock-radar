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
from stock_radar.sources.prices.fetcher import fetch_prices
from stock_radar.sources.prices.yfinance_client import YFinanceSource
from stock_radar.sources.sec.client import SecClient
from stock_radar.sources.sec.companyfacts import (
    download_companyfacts,
    iter_company_facts,
    store_facts,
    wanted_ciks,
)
from stock_radar.sources.sec.normalize import normalize_universe
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
from stock_radar.storage import DEFAULT_DB_PATH, compact, open_database

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

    facts = sub.add_parser(
        "fetch-facts",
        help="companyfacts.zip を取り込んで facts_annual / facts_quarterly を作る",
    )
    facts.add_argument(
        "--force-download",
        action="store_true",
        help="companyfacts.zip が新しくても取り直す",
    )

    prices = sub.add_parser(
        "fetch-prices", help="yfinance で日次株価を差分取得して prices_daily を更新する"
    )
    # 全銘柄の実行はデプロイ後に環境上で行う。それまではここで絞る。
    prices.add_argument("--limit", type=int, help="先頭 N 銘柄だけ取得する")
    prices.add_argument("--tickers", help="銘柄を直接指定する（カンマ区切り。例: AAPL,MSFT）")
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


def fetch_facts(runtime: Runtime, db_path: Path, market: Market, *, force: bool) -> int:
    client = SecClient.from_runtime(runtime.sec)
    zip_path = download_companyfacts(client, runtime.sec, force=force)

    with open_database(db_path) as con:
        keep = wanted_ciks(con, market=market)
        if not keep:
            log.error("universe が空。先に fetch-universe を実行する")
            return 1
        log.info("%s 社分を展開中…", f"{len(keep):,}")
        annual, quarterly = store_facts(
            con, iter_company_facts(zip_path, wanted=keep), market=market
        )
        print(f"\n=== 縦持ちテーブル（{market.value}）===")
        print(f"{'facts_annual':<22} {annual:>9,} 行")
        print(f"{'facts_quarterly':<22} {quarterly:>9,} 行")
        covered = con.execute("SELECT count(DISTINCT cik) FROM facts_annual").fetchone()[0]
        print(f"{'facts がある企業':<22} {covered:>9,} / {len(keep):,}")

        produced = normalize_universe(con)
        print(f"{'fundamentals':<22} {produced:>9,} 行")
        judgeable = con.execute(
            "SELECT "
            "  count(*) FILTER (WHERE revenue IS NOT NULL), "
            "  count(*) FILTER (WHERE gross_profit IS NOT NULL), "
            "  count(*) FILTER (WHERE operating_income IS NOT NULL), "
            "  count(*) FILTER (WHERE capex IS NOT NULL), "
            "  count(DISTINCT cik) "
            "FROM fundamentals f WHERE f.period_end = ("
            "  SELECT max(period_end) FROM fundamentals g WHERE g.cik = f.cik)"
        ).fetchone()
        companies = judgeable[4]
        print(f"\n=== 直近年度で値が取れた割合（{companies:,} 社）===")
        for label, count in zip(
            ("売上", "粗利", "営業利益", "設備投資"), judgeable[:4], strict=True
        ):
            print(f"{label:<22} {count:>9,} = {count / companies:6.1%}")

        # Phase 2a の欠損率は「いずれかの年度で取れるか」で測っている。
        # 回帰に気づけるよう、同じ定義でも出す（docs/xbrl-findings.md の D）。
        print("\n=== いずれかの年度で取れた割合（Phase 2a と同じ定義）===")
        for label, column, measured in (
            ("売上", "revenue", "91.5%"),
            ("営業利益", "operating_income", "93.5%"),
            ("設備投資", "capex", "92.9%"),
        ):
            count = con.execute(
                f"SELECT count(DISTINCT cik) FROM fundamentals WHERE {column} IS NOT NULL"
            ).fetchone()[0]
            print(
                f"{label:<22} {count:>9,} = {count / companies:6.1%}"
                f"   （Phase 2a の実測 {measured}）"
            )

    # 125万行を丸ごと入れ替えるため、これをやらないと毎回約25MB 増え続ける。
    # DuckDB は DELETE した領域をファイルに返さず、VACUUM も CHECKPOINT も効かない。
    before, after = compact(db_path)
    log.info(
        "DuckDB ファイルを作り直した（%.0f MB → %.0f MB）",
        before / 1024 / 1024,
        after / 1024 / 1024,
    )
    return 0


def fetch_prices_command(
    runtime: Runtime,
    db_path: Path,
    market: Market,
    *,
    limit: int | None,
    tickers: str | None,
) -> int:
    names = [t for t in (tickers or "").split(",") if t.strip()] or None
    with open_database(db_path) as con:
        report = fetch_prices(
            con,
            YFinanceSource(),
            runtime.prices,
            market=market,
            limit=limit,
            tickers=names,
        )

        print(f"\n=== 株価取得（{market.value}）===")
        print(f"{'対象':<22} {report.targets:>7,} 銘柄")
        print(f"{'成功':<22} {report.succeeded:>7,}")
        print(f"{'失敗':<22} {report.failed:>7,}")
        print(f"{'初回フル取得':<22} {report.initial_fetches:>7,}")
        print(f"{'遡及調整で再取得':<22} {report.split_refetches:>7,}")
        print(f"{'書き込んだ日次バー':<22} {report.bars_written:>7,}")
        if report.coverage is not None:
            print(f"{'price_coverage':<22} {report.coverage:>7.1%}")
            if report.coverage < runtime.prices.coverage_warn_threshold:
                log.warning(
                    "price_coverage が閾値 %.0f%% を下回った。"
                    "候補が少ないのは相場ではなく取りこぼしの可能性がある",
                    runtime.prices.coverage_warn_threshold * 100,
                )
        if report.stopped_on_budget:
            log.warning("時間予算で打ち切った。残りは次の run に持ち越される")

        failures = con.execute(
            "SELECT error_class, count(*) FROM fetch_failures GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        if failures:
            print("\n=== 失敗の内訳 ===")
            for error_class, count in failures:
                print(f"{error_class:<22} {count:>7,}")
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
    if args.command == "fetch-facts":
        return fetch_facts(runtime, args.db, args.market, force=args.force_download)
    if args.command == "fetch-prices":
        return fetch_prices_command(
            runtime, args.db, args.market, limit=args.limit, tickers=args.tickers
        )
    raise AssertionError(f"未知のサブコマンド: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
