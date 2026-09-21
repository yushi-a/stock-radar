"""候補 CSV の書き出し。評価スキルへの受け渡し口（docs/skill-integration.md）。

## 何を載せるか

たたき台は `docs/skill-integration.md` の想定列だが、それに**判定に使った全指標**と
**出典・基準日**を足してある。

評価スキルは一次情報から取り直す前提で、ここの数値は「手がかり」扱い（同ドキュメントの
設計ルール1）。だからこそ**乖離を見るために手がかりの側の値と、その出どころが要る**。
どの決算期の、いつ提出された数字で、株価はいつ時点かが分からないと、食い違ったときに
どちらが古いのか判断できない。

## 判断はしない

期待倍率・スコア・目標株価は出さない（CLAUDE.md）。`timing_score` は並べ替えのための
加点であって評価ではない。
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterable, Sequence
from pathlib import Path

from stock_radar.config import Market
from stock_radar.screen.evaluate import Evaluation

__all__ = ["COLUMNS", "csv_path_for", "write_candidates"]

# 並び順は「識別 → 通過理由 → 判定に使った値 → 出典と基準日」。
# 先頭を評価スキルのたたき台に合わせてあるので、そのまま読み込める。
COLUMNS: tuple[str, ...] = (
    "ticker",
    "market",
    "name",
    "track",
    "passed_filters",
    "timing_score",
    # --- 共通足切り
    "market_cap",
    "avg_daily_value",
    # --- トラックA
    "revenue_cagr_3y",
    "op_margin",
    "fcf_yield",
    "pbr",
    "roa",
    "roe",
    "asset_growth_minus_ebit_growth",
    # --- トラックB
    "revenue_growth_yoy",
    "revenue_growth_latest_quarter_yoy",
    "gross_margin",
    "psr",
    "equity_ratio",
    "current_ratio",
    # --- タイミング加点
    "drawdown_from_52w_high",
    "range_position_52w",
    # --- 出典と基準日（CLAUDE.md「数値の出典を記録する」）
    "fundamentals_fiscal_year",
    "fundamentals_period_end",
    "fundamentals_filed_at",
    "price_as_of",
    "market_cap_is_approximate",
    "data_quality_warnings",
    "data_source",
    "as_of",
)

# 通過理由は1セルに収める。カンマだと CSV の区切りと紛らわしいのでセミコロンにする。
FILTER_SEPARATOR = ";"


def csv_path_for(directory: Path | str, *, market: Market, as_of: dt.date) -> Path:
    """出力先のパス。``output/2026-09-21_us.csv``。"""
    return Path(directory) / f"{as_of.isoformat()}_{market.value}.csv"


def _row(
    evaluation: Evaluation,
    *,
    market: Market,
    data_source: str,
    as_of: dt.datetime,
    approximate: bool,
) -> dict[str, object]:
    candidate = evaluation.candidate
    metrics = candidate.metrics
    fundamentals = candidate.fundamentals
    quotes = candidate.quotes
    return {
        "ticker": candidate.ticker,
        "market": market.value,
        "name": candidate.name,
        "track": evaluation.track.value if evaluation.track is not None else None,
        "passed_filters": FILTER_SEPARATOR.join(evaluation.passed_filters),
        "timing_score": evaluation.timing_score,
        "market_cap": candidate.market_cap,
        "avg_daily_value": candidate.avg_daily_value,
        "revenue_cagr_3y": metrics.revenue_cagr_3y,
        "op_margin": metrics.op_margin,
        "fcf_yield": candidate.fcf_yield,
        "pbr": candidate.pbr,
        "roa": metrics.roa,
        "roe": metrics.roe,
        "asset_growth_minus_ebit_growth": metrics.asset_growth_minus_ebit_growth,
        "revenue_growth_yoy": metrics.revenue_growth_yoy,
        "revenue_growth_latest_quarter_yoy": metrics.revenue_growth_latest_quarter_yoy,
        "gross_margin": metrics.gross_margin,
        "psr": candidate.psr,
        "equity_ratio": metrics.equity_ratio,
        "current_ratio": metrics.current_ratio,
        "drawdown_from_52w_high": candidate.drawdown_from_52w_high,
        "range_position_52w": candidate.range_position_52w,
        "fundamentals_fiscal_year": fundamentals.fiscal_year,
        "fundamentals_period_end": fundamentals.period_end,
        "fundamentals_filed_at": fundamentals.filed_at,
        "price_as_of": quotes.latest_price_date if quotes is not None else None,
        # 複数クラス株では全クラス合計の株数に片方のクラスの株価を掛けている
        # （docs/xbrl-findings.md の C）。実測の乖離は1%以内だが、近似は近似として示す。
        "market_cap_is_approximate": approximate,
        # 売上マイナス・自己資本比率1超など、実行時に見つけた異常。
        # 手がかりが怪しいことを受け手に伝える（docs/testing.md）。
        "data_quality_warnings": FILTER_SEPARATOR.join(metrics.warnings),
        "data_source": data_source,
        "as_of": as_of.isoformat(sep=" ", timespec="seconds"),
    }


def write_candidates(
    path: Path | str,
    evaluations: Sequence[Evaluation],
    *,
    market: Market,
    data_source: str,
    as_of: dt.datetime,
    approximate_tickers: Iterable[str] = (),
) -> int:
    """通過銘柄を CSV に書いて行数を返す。``evaluations`` の順をそのまま使う。

    **通過0件でもヘッダだけのファイルを書く。** 「候補が無かった run」と
    「出力に失敗した run」をファイルの有無で区別できるようにする。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    approximate = set(approximate_tickers)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="raise")
        writer.writeheader()
        for evaluation in evaluations:
            writer.writerow(
                _row(
                    evaluation,
                    market=market,
                    data_source=data_source,
                    as_of=as_of,
                    approximate=evaluation.candidate.ticker in approximate,
                )
            )
    return len(evaluations)
