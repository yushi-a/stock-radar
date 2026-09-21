"""コマンドラインの入口。

フェーズ単位で実行できるサブコマンドを並べてある（`fetch-universe` / `fetch-facts` /
`fetch-prices` / `screen`）。全体を一息で回す `run` は Phase 5。

各サブコマンドが表示する数字は、実装計画の「検証」に当たる目視確認の材料
（除外理由別の件数、指標の取得率、通過件数と落ちた条件の内訳）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from stock_radar.config import (
    DEFAULT_CRITERIA_PATH,
    DEFAULT_RUNTIME_PATH,
    Criteria,
    Market,
    Runtime,
    load_criteria,
    load_runtime,
)
from stock_radar.metrics.market import multi_class_ciks, rebuild_market_metrics
from stock_radar.output.csv_writer import csv_path_for, write_candidates
from stock_radar.output.notify import build_message, httpx_poster, notify, summary_lines
from stock_radar.screen.evaluate import Track
from stock_radar.screen.runner import (
    ScreenReport,
    prescreen_tickers,
    screen,
    store_run,
    warn_on_quality,
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
from stock_radar.storage import DEFAULT_DB_PATH, compact, open_database, utc_now

if TYPE_CHECKING:
    import duckdb

log = logging.getLogger("stock_radar")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-radar", description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME_PATH)
    parser.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA_PATH)
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

    screen_cmd = sub.add_parser(
        "screen", help="criteria.yaml の条件を当てて screen_runs / screen_results を書く"
    )
    screen_cmd.add_argument(
        "--no-price-filters",
        action="store_true",
        help=(
            "株価が要る条件（時価総額・売買代金・PBR・PSR・FCF利回り）を飛ばす。"
            "株価が揃う前に財務側だけを測るためのモード"
        ),
    )
    screen_cmd.add_argument(
        "--dry-run", action="store_true", help="判定して表示するだけで DB にも CSV にも書かない"
    )
    screen_cmd.add_argument(
        "--no-notify",
        action="store_true",
        help="notificator に通知しない（宛先はクラスタ内なのでローカルでは届かない）",
    )

    full = sub.add_parser("run", help="全工程を順に回す（CronJob が叩くのはこれ）")
    full.add_argument("--limit", type=int, help="株価を取る銘柄を先頭 N 件に絞る")
    full.add_argument("--tickers", help="株価を取る銘柄を直接指定する（カンマ区切り）")
    full.add_argument("--no-notify", action="store_true", help="notificator に通知しない")
    full.add_argument(
        "--force-download", action="store_true", help="手元の zip が新しくても取り直す"
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
    criteria: Criteria,
    db_path: Path,
    market: Market,
    *,
    limit: int | None,
    tickers: str | None,
) -> int:
    explicit = [t for t in (tickers or "").split(",") if t.strip()] or None
    with open_database(db_path) as con:
        names = explicit
        if names is None:
            # CLAUDE.md:「株価取得は必ず財務による足切りの後に実行する。
            # 先に対象を半減させることが 429 対策の中核になっている」
            if not con.execute("SELECT count(*) FROM fundamentals").fetchone()[0]:
                log.error("fundamentals が空。先に fetch-facts を実行する")
                return 1
            names = prescreen_tickers(con, criteria, market=market)
            log.info("財務による足切りを通った %s 銘柄を対象にする", f"{len(names):,}")
            if not names:
                log.error("足切りを通った銘柄が無い。条件か財務データを確認する")
                return 1
        else:
            log.info("銘柄が明示されたので財務による足切りを通さない")
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

        produced = rebuild_market_metrics(con, market=market.value)
        print(f"{'market_metrics':<22} {produced:>7,} 銘柄")
        approximate = con.execute(
            "SELECT count(*) FROM market_metrics m "
            "JOIN universe u ON u.ticker = m.ticker AND u.market = ? "
            "WHERE u.cik IN (SELECT cik FROM universe WHERE excluded_reason IS NULL "
            "                GROUP BY cik HAVING count(DISTINCT ticker) > 1)",
            [market.value],
        ).fetchone()[0]
        if approximate:
            print(f"{'うち時価総額が近似':<22} {approximate:>7,}（複数クラス株）")

        failures = con.execute(
            "SELECT error_class, count(*) FROM fetch_failures GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        if failures:
            print("\n=== 失敗の内訳 ===")
            for error_class, count in failures:
                print(f"{error_class:<22} {count:>7,}")
    return 0


def screen_command(
    runtime: Runtime,
    criteria: Criteria,
    db_path: Path,
    market: Market,
    *,
    with_price: bool,
    dry_run: bool,
    send_notification: bool,
) -> int:
    with open_database(db_path) as con:
        report = screen(con, criteria, market=market, with_price=with_price)
        if not report.evaluated:
            log.error("判定できる銘柄が無い。先に fetch-universe / fetch-facts を実行する")
            return 1

        label = "全条件" if with_price else "株価が要らない条件だけ"
        print(f"\n=== スクリーニング（{market.value} / {label}）===")
        print(f"{'ユニバース':<26} {report.universe_size:>7,} 銘柄")
        print(f"{'うち財務データ無しで除外':<26} {report.missing_fundamentals:>7,}")
        print(f"{'判定した銘柄':<26} {report.evaluated:>7,}")
        for track, count in report.track_counts.items():
            print(f"{'通過（トラック' + track + '）':<26} {count:>7,}")
        print(f"{'通過（実数）':<26} {len(report.passed):>7,}")
        print(f"{'株価取得の対象（足切り後）':<26} {len(report.price_targets):>7,}")
        if report.price_coverage is not None:
            print(f"{'price_coverage':<26} {report.price_coverage:>7.1%}")
            if with_price and report.price_coverage < runtime.prices.coverage_warn_threshold:
                log.warning(
                    "price_coverage が閾値 %.0f%% を下回った。"
                    "候補が少ないのは相場ではなく取りこぼしの可能性がある",
                    runtime.prices.coverage_warn_threshold * 100,
                )

        # 判定できた割合。docs/xbrl-findings.md の E と同じ定義で、データ側の回帰に気づくため。
        # 株価が要る条件は数えない（株価を取りに行っていない銘柄まで判定不能に数えると、
        # 財務データの質とは無関係に数字が動く）。
        print("\n=== トラックの財務条件を判定できた割合 ===")
        for track in Track:
            count = report.decidable(track)
            print(f"{'トラック' + track.value:<26} {count:>7,} = {count / report.evaluated:6.1%}")

        # 通過件数だけでは閾値をどちらに動かせばよいか分からない。
        # 閾値未満（緩めれば増える）と判定不能（緩めても増えない）を分けて出す。
        # パイプラインの順に合わせ、財務で落ちた分と株価で落ちた分を分けて並べる。
        blocked = report.blocked_counts()
        price_names = {
            "common.market_cap",
            "common.avg_daily_value",
            "track_a.fcf_yield",
            "track_a.pbr",
            "track_b.psr",
        }
        for title, wanted in (
            ("財務で落ちた内訳（延べ。閾値未満 / 判定不能）", False),
            ("株価で落ちた内訳（足切りを通った銘柄のみ）", True),
        ):
            rows = {
                name: counts for name, counts in blocked.items() if (name in price_names) is wanted
            }
            if not rows:
                continue
            print(f"\n=== {title} ===")
            for name, counts in sorted(rows.items(), key=lambda kv: -sum(kv[1].values())):
                print(f"{name:<42} {counts['fail']:>7,} / {counts['unknown']:>7,}")

        for message in warn_on_quality(report):
            log.warning("データ品質: %s", message)

        if report.passed:
            print("\n=== 上位候補（タイミング加点順）===")
            for item in report.passed[: runtime.notify.top_n]:
                candidate = item.candidate
                score = "-" if item.timing_score is None else f"{item.timing_score:.0f}"
                print(
                    f"{candidate.ticker:<8} {(candidate.name or '')[:28]:<30} "
                    f"トラック{item.track} 加点 {score}"
                )

        if dry_run:
            log.info("dry-run なので CSV も screen_runs / screen_results も書かない")
            return 0

        # CSV と screen_runs で基準時刻を揃える。別々に取ると、日付をまたいだ実行で
        # ファイル名と記録がずれる。
        moment = utc_now()
        path: Path | None = None
        if with_price:
            path = csv_path_for(runtime.output.csv_dir, market=market, as_of=moment.date())
            # 複数クラス株の時価総額は近似（docs/xbrl-findings.md の C）。近似は近似として示す。
            multi = multi_class_ciks(con)
            written = write_candidates(
                path,
                report.passed,
                market=market,
                data_source=report.data_source,
                as_of=moment,
                approximate_tickers=[
                    item.candidate.ticker for item in report.passed if item.candidate.cik in multi
                ],
            )
            log.info("%s 行を %s に書いた", f"{written:,}", path)
        else:
            # 株価条件を当てていない一覧は評価スキルに渡す候補リストではない。
            # 同じ名前で書くと、本番の run の CSV を測定用の中間結果で上書きしてしまう。
            log.info("株価条件を当てていないので CSV は書かない")

        run_id = store_run(
            con, report, criteria, csv_path=str(path) if path else None, run_at=moment
        )
        log.info("run_id=%s として記録した（通過 %s 件）", run_id, f"{len(report.passed):,}")

        if send_notification:
            _notify(runtime, report, moment=moment, market=market, csv_path=path)
    return 0


def _notify(
    runtime: Runtime,
    report: ScreenReport,
    *,
    moment: dt.datetime,
    market: Market,
    csv_path: Path | None,
) -> None:
    """結果の要約を notificator に送る。**失敗しても run は落とさない。**

    CSV が出ていれば運用は続けられるので、通知の不達をブロッカーにしない。
    """
    lines = summary_lines(
        run_at=moment,
        market=market.value,
        universe_size=report.universe_size,
        track_counts=report.track_counts,
        passed=report.passed,
        price_coverage=report.price_coverage,
        coverage_warn_threshold=runtime.prices.coverage_warn_threshold,
        csv_path=str(csv_path) if csv_path else None,
        top_n=runtime.notify.top_n,
    )
    message = build_message(lines, max_chars=runtime.notify.max_message_chars)
    result = notify(message, runtime.notify, poster=httpx_poster)
    if result.sent:
        log.info("notificator に通知した（%s 文字）", len(result.message))
    else:
        log.warning("notificator への通知に失敗した: %s", result.error)


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


def run_all(
    runtime: Runtime,
    criteria: Criteria,
    db_path: Path,
    market: Market,
    *,
    limit: int | None = None,
    tickers: str | None = None,
    send_notification: bool = True,
    force: bool = False,
) -> int:
    """全工程を順に回す。CronJob が叩くのはこれ。

    **順序は `docs/architecture.md` のパイプラインそのままで、崩さない**（CLAUDE.md）。
    株価取得（yfinance）は必ず財務による足切りの後に走る。先に対象を減らすことが
    429 対策の中核になっている。

    途中で失敗したらそこで止めて非ゼロを返す。**半端なデータで候補を出さない。**
    たとえば財務の取り込みに失敗した状態で先に進むと、古い `fundamentals` に
    新しい株価を掛けた時価総額で判定することになる。
    """
    steps: list[tuple[str, Callable[[], int]]] = [
        ("ユニバース確定", lambda: fetch_universe(runtime, db_path, market, force=force)),
        ("財務指標", lambda: fetch_facts(runtime, db_path, market, force=force)),
        (
            "株価",
            lambda: fetch_prices_command(
                runtime, criteria, db_path, market, limit=limit, tickers=tickers
            ),
        ),
        (
            "スクリーニング",
            lambda: screen_command(
                runtime,
                criteria,
                db_path,
                market,
                with_price=True,
                dry_run=False,
                send_notification=send_notification,
            ),
        ),
    ]
    for index, (label, step) in enumerate(steps, start=1):
        log.info("[%s/%s] %s", index, len(steps), label)
        code = step()
        if code != 0:
            log.error("%s で失敗した（終了コード %s）。後続は実行しない", label, code)
            return code
    return 0


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
            runtime,
            load_criteria(args.criteria),
            args.db,
            args.market,
            limit=args.limit,
            tickers=args.tickers,
        )
    if args.command == "screen":
        return screen_command(
            runtime,
            load_criteria(args.criteria),
            args.db,
            args.market,
            with_price=not args.no_price_filters,
            dry_run=args.dry_run,
            send_notification=not args.no_notify,
        )
    if args.command == "run":
        return run_all(
            runtime,
            load_criteria(args.criteria),
            args.db,
            args.market,
            limit=args.limit,
            tickers=args.tickers,
            send_notification=not args.no_notify,
            force=args.force_download,
        )
    raise AssertionError(f"未知のサブコマンド: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
