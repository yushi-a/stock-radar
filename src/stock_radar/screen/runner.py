"""判定を DuckDB に繋ぎ、`screen_runs` / `screen_results` に記録する。

`screen/evaluate.py` までは純粋関数で、**DB を触るのはここだけ**（docs/testing.md）。

## 2つのモード

| | 当てる条件 | 使いどころ |
|---|---|---|
| `with_price=True` | 全部 | 本番のスクリーニング |
| `with_price=False` | 株価が要らないものだけ | **株価取得の前の足切り**（`prescreen_tickers`） |

後者は CLAUDE.md の「**株価取得は必ず財務による足切りの後に実行する。先に対象を
半減させることが 429 対策の中核**」に対応する。Phase 3 の時点では足切りの実装が
無く、ユニバース通過銘柄をそのまま取りに行っていた。

## 落ちた理由を数える

通過件数だけでは閾値をどちらに動かせばよいか分からない。**閾値未満で落ちた数**と
**判定不能で落ちた数**を条件ごとに分けて数える。前者は閾値を緩めれば増えるが、
後者は緩めても増えない（docs/xbrl-findings.md の「欠測の性質は2種類ある」）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stock_radar.config import Criteria, Market
from stock_radar.metrics.fundamentals import compute_universe_metrics
from stock_radar.metrics.market import MarketMetrics
from stock_radar.screen import timing
from stock_radar.screen.evaluate import Evaluation, Track, evaluate
from stock_radar.screen.filters import Candidate
from stock_radar.sources.sec.normalize import Fundamentals
from stock_radar.storage import utc_now

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "ScreenReport",
    "load_candidates",
    "prescreen_tickers",
    "screen",
    "store_run",
    "warn_on_quality",
]

log = logging.getLogger(__name__)

_UNIVERSE_SQL = (
    "SELECT ticker, cik, name FROM universe "
    "WHERE market = ? AND excluded_reason IS NULL AND cik IS NOT NULL ORDER BY ticker"
)

# 直近年度だけを見る。過去年度は指標計算（3年CAGR など）の中で使われる。
_LATEST_FUNDAMENTALS_SQL = (
    "SELECT cik, fiscal_year, period_start, period_end, period_days, revenue, gross_profit, "
    "  operating_income, net_income, total_assets, equity, current_assets, "
    "  current_liabilities, cfo, capex, shares_outstanding, currency, accn, filed_at "
    "FROM fundamentals f WHERE f.period_end = ("
    "  SELECT max(period_end) FROM fundamentals g WHERE g.cik = f.cik)"
)

_MARKET_METRICS_SQL = (
    "SELECT ticker, as_of, market_cap, avg_daily_value, high_52w, low_52w, "
    "  range_position_52w, drawdown_from_52w_high, latest_price_date FROM market_metrics"
)

_RESULT_COLUMNS = (
    "run_id",
    "market",
    "ticker",
    "cik",
    "name",
    "track",
    "passed_filters",
    "timing_score",
    "market_cap",
    "avg_daily_value",
    "revenue_cagr_3y",
    "op_margin",
    "fcf_yield",
    "pbr",
    "roa",
    "roe",
    "asset_growth_minus_ebit_growth",
    "revenue_growth_yoy",
    "revenue_growth_latest_quarter_yoy",
    "gross_margin",
    "psr",
    "equity_ratio",
    "current_ratio",
    "drawdown_from_52w_high",
    "range_position_52w",
    "fundamentals_fiscal_year",
    "fundamentals_period_end",
    "fundamentals_filed_at",
    "price_as_of",
    "data_source",
    "as_of",
)


@dataclass(frozen=True, slots=True)
class ScreenReport:
    """1回の判定の結果。`screen_runs` に書くのはここから。"""

    market: Market
    with_price: bool
    universe_size: int
    missing_fundamentals: int
    evaluations: list[Evaluation] = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return len(self.evaluations)

    @property
    def passed(self) -> list[Evaluation]:
        """通過した銘柄。タイミング加点の順に並べる。"""
        return sorted(
            (item for item in self.evaluations if item.passed),
            key=lambda item: timing.ranking_key(
                item.timing_score, item.candidate.range_position_52w, item.candidate.ticker
            ),
        )

    @property
    def track_counts(self) -> dict[str, int]:
        """トラック別の通過数。両方通った銘柄は両方に数える。"""
        counts = {track.value: 0 for track in Track}
        for item in self.evaluations:
            for track in item.tracks:
                counts[track.value] += 1
        return counts

    @property
    def price_targets(self) -> list[Evaluation]:
        """株価を取りに行った（べき）銘柄。財務の足切りを通ったもの。"""
        return [item for item in self.evaluations if item.prescreened]

    @property
    def price_coverage(self) -> float | None:
        """**株価を取りに行った銘柄のうち**、実際に株価がある割合。

        低い run は「相場のせいで候補が少ない」のではなく「取りこぼしのせい」。

        ⚠️ 分母をユニバース全体にしてはいけない。パイプラインは財務の足切りが
        先で、落ちた銘柄の株価はそもそも取りに行っていない。全体を分母にすると
        取りこぼしゼロでも 6.9% と出て、警告が鳴りっぱなしになる。
        """
        targets = self.price_targets
        if not targets:
            return None
        with_quotes = sum(1 for item in targets if item.candidate.quotes is not None)
        return with_quotes / len(targets)

    @property
    def data_source(self) -> str:
        """この run が何で判定したか。CSV と `screen_results` の両方に載せる。

        指標ごとに採用した XBRL タグは `fundamentals.source_concepts` にある。
        ここに書くのは「どのデータ源まで使ったか」で、株価を当てていない run は
        その旨が残る。
        """
        return "sec_companyfacts+yfinance" if self.with_price else "sec_companyfacts"

    def blocked_counts(self, *, needs_price: bool | None = None) -> dict[str, Counter[str]]:
        """条件ごとに、閾値未満（``fail``）と判定不能（``unknown``）を分けて数える。

        落ちた銘柄の**すべての**不通過条件を数えるので、合計は銘柄数と一致しない。
        「B の粗利率さえ取れていれば通った」のような分布を見るための数字。

        ``needs_price`` を渡すと、株価が要る条件だけ / 要らない条件だけに絞る。
        パイプラインの段（財務 → 株価）ごとに内訳を読むため。
        """
        counts: dict[str, Counter[str]] = {}
        for item in self.evaluations:
            for check in item.blocking:
                if needs_price is not None and check.needs_price is not needs_price:
                    continue
                counts.setdefault(check.name, Counter())[check.verdict.value] += 1
        return counts

    def decidable(self, track: Track) -> int:
        """そのトラックの**財務条件**を判定できた銘柄数（判定不能が1つも無い）。

        docs/xbrl-findings.md の E と同じ定義で、データ側の回帰に気づくために出す。

        株価が要る条件は数えない。株価は財務の足切りの後にしか取らないので、
        含めると「株価を取りに行かなかった銘柄はすべて判定不能」になり、
        財務データの質とは無関係に数字が動く。
        """
        checks = {Track.A: lambda item: item.track_a, Track.B: lambda item: item.track_b}[track]
        return sum(
            1
            for item in self.evaluations
            if not any(check.undecidable for check in checks(item) if not check.needs_price)
        )


def load_candidates(
    con: duckdb.DuckDBPyConnection, *, market: Market
) -> tuple[list[Candidate], int, int]:
    """判定材料を組み立てる。``(候補, ユニバース件数, 財務が無い件数)``。

    **財務は CIK 単位、株価はティッカー単位。** 複数クラス株では1つの CIK に
    複数のティッカーがぶら下がるので、財務側は同じ行を共有する。
    """
    metrics = compute_universe_metrics(con)

    latest: dict[int, Fundamentals] = {}
    for row in con.execute(_LATEST_FUNDAMENTALS_SQL).fetchall():
        cik = int(row[0])
        latest[cik] = Fundamentals(
            cik=cik,
            fiscal_year=row[1],
            period_start=row[2],
            period_end=row[3],
            period_days=row[4],
            revenue=row[5],
            gross_profit=row[6],
            operating_income=row[7],
            net_income=row[8],
            total_assets=row[9],
            equity=row[10],
            current_assets=row[11],
            current_liabilities=row[12],
            cfo=row[13],
            capex=row[14],
            shares_outstanding=row[15],
            currency=row[16],
            accn=row[17],
            filed_at=row[18],
        )

    quotes: dict[str, MarketMetrics] = {}
    for row in con.execute(_MARKET_METRICS_SQL).fetchall():
        quotes[row[0]] = MarketMetrics(
            ticker=row[0],
            as_of=row[1],
            market_cap=row[2],
            avg_daily_value=row[3],
            high_52w=row[4],
            low_52w=row[5],
            range_position_52w=row[6],
            drawdown_from_52w_high=row[7],
            latest_price_date=row[8],
        )

    candidates: list[Candidate] = []
    universe_size = 0
    missing = 0
    for ticker, cik, name in con.execute(_UNIVERSE_SQL, [market.value]).fetchall():
        universe_size += 1
        cik = int(cik)
        fundamentals = latest.get(cik)
        company = metrics.get(cik)
        if fundamentals is None or company is None:
            # companyfacts に XBRL が無い、または年度を1つも確定できなかった企業。
            missing += 1
            continue
        candidates.append(
            Candidate(
                market=market,
                ticker=ticker,
                cik=cik,
                name=name,
                fundamentals=fundamentals,
                metrics=company,
                quotes=quotes.get(ticker),
            )
        )
    return candidates, universe_size, missing


def screen(
    con: duckdb.DuckDBPyConnection,
    criteria: Criteria,
    *,
    market: Market = Market.US,
    with_price: bool = True,
) -> ScreenReport:
    """ユニバース全体を判定する。DB への書き込みはしない。"""
    candidates, universe_size, missing = load_candidates(con, market=market)
    return ScreenReport(
        market=market,
        with_price=with_price,
        universe_size=universe_size,
        missing_fundamentals=missing,
        evaluations=[
            evaluate(candidate, criteria, with_price=with_price) for candidate in candidates
        ],
    )


def prescreen_tickers(
    con: duckdb.DuckDBPyConnection, criteria: Criteria, *, market: Market = Market.US
) -> list[str]:
    """株価を取りに行く価値がある銘柄。

    株価が要らない条件をすべて当てて、**どちらのトラックにも残らない銘柄を落とす**。
    CLAUDE.md の「先に対象を半減させることが 429 対策の中核」がこれ。
    """
    report = screen(con, criteria, market=market, with_price=False)
    return sorted(item.candidate.ticker for item in report.evaluations if item.passed)


def store_run(
    con: duckdb.DuckDBPyConnection,
    report: ScreenReport,
    criteria: Criteria,
    *,
    csv_path: str | None = None,
    run_at: dt.datetime | None = None,
) -> int:
    """`screen_runs` と `screen_results` に書いて `run_id` を返す。

    `criteria_snapshot` には**既定値の補完まで含めた実際に使われた値**を入れる
    （`Criteria.snapshot()`）。ファイルの生テキストだと、後から結果を再現できない。
    """
    moment = run_at if run_at is not None else utc_now()
    passed = report.passed
    row = con.execute(
        "INSERT INTO screen_runs "
        "  (run_at, market, criteria_snapshot, universe_size, passed_count, "
        "   price_coverage, csv_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING run_id",
        [
            moment,
            report.market.value,
            json.dumps(criteria.snapshot(), ensure_ascii=False),
            report.universe_size,
            len(passed),
            report.price_coverage,
            csv_path,
        ],
    ).fetchone()
    run_id = int(row[0])

    source = report.data_source
    rows = []
    for item in passed:
        candidate = item.candidate
        metrics = candidate.metrics
        quotes = candidate.quotes
        rows.append(
            (
                run_id,
                report.market.value,
                candidate.ticker,
                candidate.cik,
                candidate.name,
                item.track.value if item.track is not None else Track.A.value,
                list(item.passed_filters),
                item.timing_score,
                candidate.market_cap,
                candidate.avg_daily_value,
                metrics.revenue_cagr_3y,
                metrics.op_margin,
                candidate.fcf_yield,
                candidate.pbr,
                metrics.roa,
                metrics.roe,
                metrics.asset_growth_minus_ebit_growth,
                metrics.revenue_growth_yoy,
                metrics.revenue_growth_latest_quarter_yoy,
                metrics.gross_margin,
                candidate.psr,
                metrics.equity_ratio,
                metrics.current_ratio,
                candidate.drawdown_from_52w_high,
                candidate.range_position_52w,
                candidate.fundamentals.fiscal_year,
                candidate.fundamentals.period_end,
                candidate.fundamentals.filed_at,
                quotes.latest_price_date if quotes is not None else None,
                source,
                moment,
            )
        )
    if rows:
        placeholders = ", ".join("?" for _ in _RESULT_COLUMNS)
        con.executemany(
            f"INSERT INTO screen_results ({', '.join(_RESULT_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
    return run_id


def warn_on_quality(report: ScreenReport) -> list[str]:
    """通過銘柄に付いているデータ品質の警告を集める。

    テストで捕まえられないデータ起因の問題はここでしか捕まらない（docs/testing.md）。
    落とさずに記録する。
    """
    messages: list[str] = []
    for item in report.passed:
        for warning in item.candidate.metrics.warnings:
            messages.append(f"{item.candidate.ticker}: {warning}")
    return messages
