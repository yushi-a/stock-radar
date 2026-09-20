"""DuckDB の接続とスキーマ定義。

スキーマの意図と「再生成可 / 永続が必要」の区別は docs/architecture.md の
「DuckDB スキーマ」を参照。ここはその表の実体。

単一ファイル（既定 ``data/stock_radar.duckdb``）に全部入れる。DuckDB は書き込みモードで
1プロセスしかファイルを開けないため、CronJob には ``concurrencyPolicy: Forbid`` が要る。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from stock_radar.config import Market

__all__ = [
    "ALL_TABLES",
    "DEFAULT_DB_PATH",
    "PERSISTENT_TABLES",
    "REBUILDABLE_TABLES",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "StorageError",
    "apply_schema",
    "as_utc_naive",
    "connect",
    "drop_rebuildable",
    "open_database",
    "schema_version",
    "utc_now",
]

DEFAULT_DB_PATH = Path("data/stock_radar.duckdb")

# 時刻列は TIMESTAMP（tz 無し）で、値は常に UTC とする。
#
# TIMESTAMPTZ にすると (1) Python へ読み戻すのに pytz が要り、(2) セッションの
# タイムゾーンで表示が変わる。ローカル（JST）とコンテナ（UTC）で CSV の値が
# ずれることになるので、UTC の naive に統一して書く側で揃える。

# スキーマを非互換に変えたら上げる。上げ忘れると古いファイルを黙って読むことになる。
SCHEMA_VERSION = 2

# zip や API から作り直せるテーブル。閾値やタグ優先順位を変えたときに捨てて再構築する。
REBUILDABLE_TABLES = (
    "universe",
    "facts_annual",
    "facts_quarterly",
    "market_metrics",
)

# 失うと取り直しに時間がかかるテーブル。特に prices_daily は 429 対策のせいで
# フル取得に丸1日かかる。fundamentals も元の companyfacts.zip を1世代しか残さないため、
# 捨てると再ダウンロードが要る。
PERSISTENT_TABLES = (
    "fundamentals",
    "prices_daily",
    "fetch_failures",
    "screen_runs",
    "screen_results",
)

ALL_TABLES = REBUILDABLE_TABLES + PERSISTENT_TABLES

_MARKETS = ", ".join(f"'{market.value}'" for market in Market)
_ERROR_CLASSES = "'rate_limited', 'invalid_symbol', 'other'"


class StorageError(Exception):
    """DuckDB ファイルを開けない、またはスキーマが扱えない。"""


class SchemaVersionError(StorageError):
    """既存ファイルのスキーマバージョンがコードと合わない。"""


_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""

_DDL: tuple[str, ...] = (
    # --- universe -----------------------------------------------------------
    # 除外した銘柄も残す。Phase 1 の検証で「除外理由別の件数」を出すため、
    # excluded_reason が NULL のものだけが残存ユニバースになる。
    f"""
    CREATE TABLE IF NOT EXISTS universe (
        market          VARCHAR NOT NULL CHECK (market IN ({_MARKETS})),
        ticker          VARCHAR NOT NULL,
        cik             INTEGER,
        name            VARCHAR,
        -- SIC は先頭ゼロがありうるので文字列で持つ。6000番台が金融・REIT。
        sic             VARCHAR,
        exchange        VARCHAR,
        -- 10-K / 20-F など。ADR 除外の判定に使う。
        form_type       VARCHAR,
        -- NULL なら残存。'financial_sic' / 'no_10k' / 'otc' などを入れる。
        excluded_reason VARCHAR,
        updated_at      TIMESTAMP   NOT NULL,
        PRIMARY KEY (market, ticker)
    );
    """,
    # --- facts_annual -------------------------------------------------------
    # companyfacts を縦持ちのまま入れる。同じ会計年度が複数回出る（元の10-K +
    # 翌年の比較欄での再掲、修正再提出）ため accn までキーに含める。
    # どのエントリを採るかは Phase 2a で決めて normalize.py で解決する。
    """
    CREATE TABLE IF NOT EXISTS facts_annual (
        cik          INTEGER NOT NULL,
        fiscal_year  INTEGER NOT NULL,
        -- PL/CF は期間値で start を持ち、BS は時点値で NULL になる。
        -- end - start は決算期変更（非12ヶ月の「年度」）の検知に使う。
        period_start DATE,
        period_end   DATE    NOT NULL,
        concept      VARCHAR NOT NULL,
        value        DOUBLE,
        unit         VARCHAR NOT NULL,
        accn         VARCHAR NOT NULL,
        filed_at     DATE,
        PRIMARY KEY (cik, concept, unit, period_end, accn)
    );
    """,
    # --- facts_quarterly ----------------------------------------------------
    # 売上関連のみ。トラックB の「直近四半期 YoY」に要る。
    # 多くの企業は YTD でしか報告しないため、fiscal_period を持って
    # Q2 = YTD(Q2) - YTD(Q1) のような引き算をできるようにする（Phase 2a の B）。
    """
    CREATE TABLE IF NOT EXISTS facts_quarterly (
        cik           INTEGER NOT NULL,
        fiscal_year   INTEGER NOT NULL,
        fiscal_period VARCHAR NOT NULL CHECK (fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4', 'FY')),
        period_start  DATE,
        period_end    DATE    NOT NULL,
        concept       VARCHAR NOT NULL,
        value         DOUBLE,
        unit          VARCHAR NOT NULL,
        accn          VARCHAR NOT NULL,
        filed_at      DATE,
        PRIMARY KEY (cik, concept, unit, period_end, accn)
    );
    """,
    # --- fundamentals -------------------------------------------------------
    # 正規化後の横持ち。companyfacts.zip は最新1世代しか残さないので、
    # ここを失うと再ダウンロードが要る。
    """
    CREATE TABLE IF NOT EXISTS fundamentals (
        cik                 INTEGER NOT NULL,
        fiscal_year         INTEGER NOT NULL,
        period_start        DATE,
        period_end          DATE NOT NULL,
        -- 決算期変更で12ヶ月でない「年度」が実在する。350〜380日の外なら
        -- 3年CAGR の計算から外す判断に使う（Phase 2a の A）。
        period_days         INTEGER,
        revenue             DOUBLE,
        gross_profit        DOUBLE,
        operating_income    DOUBLE,
        net_income          DOUBLE,
        total_assets        DOUBLE,
        equity              DOUBLE,
        current_assets      DOUBLE,
        current_liabilities DOUBLE,
        cfo                 DOUBLE,
        capex               DOUBLE,
        shares_outstanding  DOUBLE,
        -- USD 建てで報告しない企業の扱いは未決（docs/open-questions.md）。
        -- 判断できるよう通貨は必ず持つ。
        currency            VARCHAR,
        -- 出典（CLAUDE.md「数値の出典を記録する」）。accn から SEC の該当提出に戻れる。
        accn                VARCHAR,
        filed_at            DATE,
        -- 指標ごとに採用した XBRL タグ。{"revenue": "Revenues", ...}
        source_concepts     JSON,
        PRIMARY KEY (cik, fiscal_year)
    );
    """,
    # --- prices_daily -------------------------------------------------------
    # adj_close は列ごと持たない。配当のたびに過去が遡及的に書き換わるうえ、
    # 使っている指標が1つも無い（docs/architecture.md）。
    """
    CREATE TABLE IF NOT EXISTS prices_daily (
        ticker VARCHAR NOT NULL,
        date   DATE    NOT NULL,
        open   DOUBLE,
        high   DOUBLE,
        low    DOUBLE,
        close  DOUBLE,
        volume BIGINT,
        PRIMARY KEY (ticker, date)
    );
    """,
    # --- fetch_failures -----------------------------------------------------
    # リトライ用のキュー兼、再開時のスキップ判定。進捗管理テーブルは作らず、
    # prices_daily との引き算で再開対象を導出する（docs/architecture.md）。
    f"""
    CREATE TABLE IF NOT EXISTS fetch_failures (
        ticker        VARCHAR NOT NULL,
        -- 一時的（429）と恒久的（上場廃止・ティッカー変更の疑い）を区別しないと、
        -- 上場廃止銘柄を毎週リトライし続けることになる。
        error_class   VARCHAR NOT NULL CHECK (error_class IN ({_ERROR_CLASSES})),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_attempt  TIMESTAMP   NOT NULL,
        last_error    VARCHAR,
        PRIMARY KEY (ticker)
    );
    """,
    # --- market_metrics -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS market_metrics (
        ticker                 VARCHAR NOT NULL,
        as_of                  DATE    NOT NULL,
        market_cap             DOUBLE,
        avg_daily_value        DOUBLE,
        high_52w               DOUBLE,
        low_52w                DOUBLE,
        range_position_52w     DOUBLE,
        drawdown_from_52w_high DOUBLE,
        -- 使った株価の最終日。時間予算で打ち切ったときの鮮度フラグに要る。
        latest_price_date      DATE,
        PRIMARY KEY (ticker, as_of)
    );
    """,
    # --- screen_runs --------------------------------------------------------
    "CREATE SEQUENCE IF NOT EXISTS screen_run_id_seq START 1;",
    f"""
    CREATE TABLE IF NOT EXISTS screen_runs (
        run_id            BIGINT PRIMARY KEY DEFAULT nextval('screen_run_id_seq'),
        run_at            TIMESTAMP   NOT NULL,
        market            VARCHAR NOT NULL CHECK (market IN ({_MARKETS})),
        -- 判定に使った閾値そのもの。これがあると「この結果はどの閾値で出たか」を
        -- 後から完全に再現でき、閾値調整の試行錯誤が記録として残る。
        criteria_snapshot JSON NOT NULL,
        universe_size     INTEGER,
        passed_count      INTEGER,
        -- 株価取得の成功率。低い run は「相場のせい」ではなく「取りこぼしのせい」。
        price_coverage    DOUBLE,
        csv_path          VARCHAR
    );
    """,
    # --- screen_results -----------------------------------------------------
    # 指標の列は criteria.yaml の閾値と1対1で対応させる。判定に使った値を
    # そのまま残さないと、なぜ通った / 落ちたかを後から追えない。
    f"""
    CREATE TABLE IF NOT EXISTS screen_results (
        run_id                            BIGINT  NOT NULL REFERENCES screen_runs(run_id),
        market                            VARCHAR NOT NULL CHECK (market IN ({_MARKETS})),
        ticker                            VARCHAR NOT NULL,
        cik                               INTEGER,
        name                              VARCHAR,
        track                             VARCHAR NOT NULL CHECK (track IN ('A', 'B')),
        -- 満たした条件名の一覧。通過理由を銘柄ごとに記録する。
        passed_filters                    VARCHAR[],
        timing_score                      DOUBLE,
        -- 共通足切り
        market_cap                        DOUBLE,
        avg_daily_value                   DOUBLE,
        -- トラックA
        revenue_cagr_3y                   DOUBLE,
        op_margin                         DOUBLE,
        fcf_yield                         DOUBLE,
        pbr                               DOUBLE,
        roa                               DOUBLE,
        roe                               DOUBLE,
        asset_growth_minus_ebit_growth    DOUBLE,
        -- トラックB
        revenue_growth_yoy                DOUBLE,
        revenue_growth_latest_quarter_yoy DOUBLE,
        gross_margin                      DOUBLE,
        psr                               DOUBLE,
        equity_ratio                      DOUBLE,
        current_ratio                     DOUBLE,
        -- タイミング加点
        drawdown_from_52w_high            DOUBLE,
        range_position_52w                DOUBLE,
        -- 出典と基準日（CLAUDE.md「数値の出典を記録する」）
        fundamentals_fiscal_year          INTEGER,
        fundamentals_period_end           DATE,
        fundamentals_filed_at             DATE,
        price_as_of                       DATE,
        data_source                       VARCHAR,
        as_of                             TIMESTAMP,
        PRIMARY KEY (run_id, market, ticker)
    );
    """,
)


def utc_now() -> dt.datetime:
    """いまの UTC 時刻。tz は落としてある（上の方針）。"""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def as_utc_naive(value: dt.datetime) -> dt.datetime:
    """DuckDB に入れる形に揃える。

    tz 付きなら UTC に直して tz を落とす。naive はすでに UTC として扱う。
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.UTC).replace(tzinfo=None)


def connect(
    path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """DuckDB ファイルに接続する。親ディレクトリが無ければ作る。

    ``":memory:"`` を渡すとインメモリになる（テスト用）。
    """
    if str(path) == ":memory:":
        return duckdb.connect(":memory:")
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return duckdb.connect(str(path), read_only=read_only)
    except duckdb.Error as exc:
        raise StorageError(f"DuckDB ファイルを開けない: {path} ({exc})") from exc


def schema_version(con: duckdb.DuckDBPyConnection) -> int | None:
    """記録されているスキーマバージョン。未適用なら ``None``。

    読み取り専用の接続でも呼べるよう、ここでは何も作らない。
    """
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'schema_meta'"
    ).fetchone()[0]
    if not exists:
        return None
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return int(row[0]) if row else None


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """スキーマを適用する。何度実行しても同じ状態になる。

    既存ファイルのバージョンが合わない場合は作り直さずに落とす。prices_daily の
    フル取得には丸1日かかるので、黙って捨ててよいテーブルではない。
    """
    con.execute(_SCHEMA_META_DDL)
    current = schema_version(con)
    if current is not None and current != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"スキーマバージョンが合わない（ファイル: {current} / コード: {SCHEMA_VERSION}）。"
            "移行手順を決めるまで自動では作り直さない。"
        )
    for statement in _DDL:
        con.execute(statement)
    con.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [str(SCHEMA_VERSION)],
    )


def drop_rebuildable(con: duckdb.DuckDBPyConnection) -> None:
    """再生成可のテーブルだけを捨てる。

    タグ優先順位や除外条件を変えて作り直したいときに使う。永続が必要なテーブル
    （株価履歴、スクリーニング履歴）には触らない。
    """
    for table in REBUILDABLE_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {table}")


@contextmanager
def open_database(
    path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    """接続してスキーマを適用し、終わったら閉じる。

    読み取り専用で開いた場合はスキーマを適用せず、バージョンだけ確かめる。
    """
    con = connect(path, read_only=read_only)
    try:
        if read_only:
            current = schema_version(con)
            if current != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"スキーマバージョンが合わない（ファイル: {current} / "
                    f"コード: {SCHEMA_VERSION}）。"
                )
        else:
            apply_schema(con)
        yield con
    finally:
        con.close()
