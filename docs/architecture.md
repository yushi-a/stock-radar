# システム構成（2026-09-20 合意）

米国株先行版の設計。日本株は同じパイプライン骨格に別ソースを差し込む形で後から追加する。

## 決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| 初回対象 | 米国株 | 10倍銘柄の発生率が日本の3倍以上（米 約7〜8%/15年 vs 日 約2%/10年）。かつ SEC + yfinance で完全無料・データ制約なし。J-Quants は Free のままだと12週遅延・2年分のみで3年CAGRが計算できない |
| 財務ソース | SEC EDGAR `companyfacts.zip` | 全社分を1回のダウンロードで取得でき、レート制限を気にせず全銘柄処理できる |
| 株価ソース | yfinance（失敗時 FMP） | 無料。429 対策として適応型スロットリング・差分取得・再開可能性を設計に織り込む |
| 性能要件 | 週1回完走すればよい。**1回の実行に丸1日以上かかっても構わない** | この緩さが 429 対策の設計方針を決めている（後述） |
| 言語 / パッケージ管理 | Python + uv | `pyproject.toml` + `uv.lock`。Docker イメージ化も容易 |
| ストレージ | DuckDB 単一ファイル（`data/stock_radar.duckdb`） | local PVC が使えるため、単一ファイルがそのまま載る。SQL で閾値適用と履歴比較が書ける |
| 実行基盤 | まずローカル CLI → 安定後に K3s CronJob（クラスタ `yuxsr-dev`） | コンテナ化を見据えた構成にはしておく |
| 出力 | CSV + 通知（notificator） | 同一クラスタ内にデプロイ済みの自前アプリ `notificator` に送る。Service 名で到達できる。インターフェースは実装時に確認する |
| 会計期間 | 年次（10-K）主、売上のみ四半期（10-Q）も保持 | Yartseva も年次ベース。四半期は トラックB の売上 YoY にのみ使う |
| 実行タイミング | **未定**（CronJob 化フェーズで決定） | 第一弾は手動実行のため実害なし。推奨は土曜朝 JST（米国市場の週の取引終了直後） |

## パイプライン

処理順が設計の要。**株価取得を最後に回す**ことでネットワーク負荷が最も重い工程の対象を半減させる。

```
① ユニバース確定
   SEC company_tickers_exchange.json + submissions.zip
   → OTC / 取引所なし除外 → SIC で金融・REIT 除外 / 10-K 提出企業のみ（ADR 除外）
   6,067社 → 3,189社                             [小さなAPI 1回 + zip 1.5GB を1回]
   （2026-09-20 実測。内訳は docs/implementation-plan.md の Phase 1）

② 財務指標を全社計算
   companyfacts.zip を1回ダウンロード → 全社分を正規化
   → 自己資本プラス・売上ゼロ除外・売上成長率で足切り
   3,189社 → 約1,400社（①の実測に比例させた見込み。Phase 2b で実測する）
                                                [ネットワーク1回・あとはローカル処理]

③ 株価を取得
   ②の通過分だけ yfinance で日次1年3ヶ月分（差分追記）
   → 時価総額・平均売買代金・52週高安
   約1,400社                                     [ここだけが重い。初回フル、以降は差分]

④ 条件判定・出力
   DuckDB 上で config/criteria.yaml の閾値を適用
   → 共通足切り → トラックA / トラックB → タイミング加点で並べ替え
   → CSV 出力 + notificator へ通知
```

時価総額は `dei:EntityCommonStockSharesOutstanding`（XBRL 表紙の発行済株式数）× 株価で自前計算する。
yfinance の `marketCap` には依存せず、照合にのみ使う（「一次情報優先」方針に沿う）。
ただし `marketCap` の取得は `history()` とは別リクエストになりリクエスト数が倍になるため、
**照合は Phase 3 の検証時にサンプル（200銘柄程度）で1回行うだけにし、定常運用では取得しない。**

## モジュール構成（案）

```
src/stock_radar/
  cli.py                    エントリポイント
  config.py                 criteria.yaml / runtime.yaml の読み込みと型付け
  storage.py                DuckDB 接続・スキーマ定義・マイグレーション
  sources/
    sec/
      universe.py           company_tickers / submissions → ユニバース
      companyfacts.py       zip のダウンロードとストリーム展開
      concepts.py           XBRL タグの優先順位マッピング（後述）
      normalize.py          年次 / 四半期レコードの正規化
    prices/
      yfinance_client.py    差分取得・適応型スロットリング・サーキットブレーカー・リトライ
  metrics/
    fundamentals.py         売上CAGR・利益率・FCF・ROA/ROE・資産成長率
    market.py               時価総額・売買代金・52週高安・PBR/PSR/FCF利回り
  screen/
    filters.py              判定の三値（通過 / 不通過 / 判定不能）と共通足切り
    track_a.py / track_b.py
    timing.py               並べ替え用スコア
    evaluate.py             上記を1銘柄に通す（純粋関数の最上位）
    runner.py               DuckDB からの読み出しと screen_runs / screen_results への記録
  output/
    csv_writer.py
    notify.py               notificator への送信（インターフェースは実装時に確認）
```

## DuckDB スキーマ

**実体は `src/stock_radar/storage.py`。** 列の意図はそこのコメントに書いてある。

| テーブル | 役割 | 永続 |
|---|---|---|
| `universe` | market, ticker, cik, name, sic, exchange, form_type, `excluded_reason`, updated_at。主キー `(market, ticker)` | 再生成可 |
| `facts_annual` | 縦持ち。cik, fiscal_year, `period_start`, period_end, concept, value, unit, accn, filed_at。**一意制約は付けない**（後述） | 再生成可（zip があれば） |
| `facts_quarterly` | 同上 + `fiscal_period`（Q1〜Q4/FY、NULL 可）。売上関連のみ | 再生成可 |
| `fundamentals` | 横持ち・正規化後。**主キーは `(cik, period_end)`**（後述）。cik, fiscal_year, period_start/end, `period_days`, revenue, gross_profit, operating_income, net_income, total_assets, equity, `current_assets`, `current_liabilities`, cfo, capex, shares_outstanding, `currency`, accn, filed_at, `source_concepts`(JSON) | **必要** |
| `prices_daily` | ticker, date, open, high, low, close, volume。主キー `(ticker, date)` | **必要**（差分追記） |
| `fetch_failures` | ticker, `error_class`, attempt_count, last_attempt, last_error。リトライ用のキュー兼、再開時のスキップ判定 | **必要** |
| `market_metrics` | ticker, as_of, market_cap, avg_daily_value, high_52w, low_52w, range_position_52w, drawdown_from_52w_high, `latest_price_date` | 再生成可 |
| `screen_runs` | run_id, run_at, market, `criteria_snapshot`(JSON), universe_size, passed_count, `price_coverage`, csv_path | **必要** |
| `screen_results` | run_id, market, ticker, cik, name, track, passed_filters, timing_score, 各指標値（`criteria.yaml` の閾値と1対1）, 出典と基準日 | **必要** |

`screen_runs.criteria_snapshot` に閾値そのものを保存する。
これにより「この結果はどの閾値で出たか」を後から完全に再現でき、閾値調整の試行錯誤が記録として残る。

### 設計時の表から増やした列とその理由

- `universe.market` / `screen_runs.market` / `screen_results.market` —
  条件が市場ごとに分かれており（`config/criteria.yaml`）、CSV の想定列にも `market` がある
- `universe.excluded_reason` — Phase 1 の検証項目「除外理由別の件数も出す」に要る。
  除外した銘柄も行として残し、`NULL` のものが残存ユニバースになる
- `facts_annual.period_start` / `facts_quarterly.period_start` —
  PL/CF は期間値、BS は時点値という区別と、`end - start` による決算期変更の検知（Phase 2a の A）に要る
- `facts_quarterly.fiscal_period` — YTD → 3ヶ月変換（Phase 2a の B）に要る
- `fundamentals.period_days` — 非12ヶ月の「年度」を 3年CAGR から外す判断に使う
- `fundamentals.current_assets` / `current_liabilities` — トラックB の流動比率
- `fundamentals.currency` — 「USD 建てで報告しない企業の扱い」（未決）を判断できるようにする
- `market_metrics.latest_price_date` — 時間予算で打ち切ったときの鮮度フラグ

### 時刻列は tz 無しの UTC

時刻列は `TIMESTAMP`（tz 無し）で、値は常に UTC で入れる。`TIMESTAMPTZ` にすると
Python へ読み戻すのに `pytz` が要り、かつセッションのタイムゾーンで表示が変わるため、
ローカル（JST）とコンテナ（UTC）で CSV の値がずれる。
書く側は `storage.utc_now()` / `storage.as_utc_naive()` を通す。

### `fundamentals` の主キーが会計年度でない理由

決算期がずれると**同じ暦年に2つの年度が並ぶ**。BK Technologies は期末 2020-01-01（FY2019）と
2020-12-31（FY2020）の両方を持つ。会計年度をキーにすると衝突するので、期末をキーにする。

会計年度のラベルは「その年度が終わる暦年」。ただし期末が1月7日以前なら前年とみなす
（12/31 から 1/1 に1日ずれただけで翌年度扱いになるのを避けるため）。
Walmart（1月末決算）は 2024-01-31 → FY2024 で、会社自身の呼び方と一致する。

### 縦持ちテーブルに一意制約を付けない理由

`facts_annual` / `facts_quarterly` は SEC の生の写しで、主キーを持たない。

**SEC は同じ `(cik, concept, unit, period_end, accn)` を2回報告することがある。**
違うのは `period_start` だけで、実測で年次104件・四半期61件。**うち14件は値まで食い違う。**

```
Coeur Mining 2011年度（同じ 10-K、同じ accn）
  2011-01-01 .. 2011-12-31  365日  1,021,200,000
  2011-01-10 .. 2011-12-31  356日    246,911,000   ← 4倍違う
```

どちらも 350〜380日のガードを通る。一意制約を付けると片方を黙って捨てることになり、
売上を4分の1に取り違える。**どちらを採るかは `normalize.py` の責務**にする。

### スキーマの移行

`schema_meta` テーブルに `version` を持つ。**バージョンが合わないときは自動で作り直さず、落とす。**
`prices_daily` のフル取得には丸1日かかり、`fundamentals` も元の `companyfacts.zip` を
1世代しか残さないため、黙って捨ててよいテーブルではない。

再生成可のテーブル（`universe` / `facts_annual` / `facts_quarterly` / `market_metrics`）だけは
`drop_rebuildable()` で捨てて作り直せる。タグ優先順位や除外条件を変えたときに使う。

生の `companyfacts.zip` は DuckDB には入れず `data/raw/sec/companyfacts_YYYY-MM-DD.zip` としてファイルで保持する。
**保持は最新1世代のみ**とし、取得・パース後に古い世代を削除する。1ファイルが1GB超あり、
週次で世代を残すと年50GBを超えるため。過去時点の財務値を再現したくなった場合は、
`fundamentals.accn`（提出番号）から SEC に個別に取りに行く。

### 永続化の要件（合意済み）

- R1: レート制限の厳しい API を再実行のたびに叩かない
- R2: 日次株価を差分追記で1年以上保持（52週高安・平均売買代金に必須）
- R3: 週次スナップショットを履歴として残し、週次で差分比較できる
- R4: 閾値変更 → 再取得なしで再スクリーニングできる（`fundamentals` + `market_metrics` が残っていれば満たせる）
- R5: データソースを差し替えられる（yfinance → FMP、将来の J-Quants Free → Standard）
- R6: ローカル実行で育て、K3s の CronJob に載せ替えても状態が引き継げる（PVC に DuckDB ファイルを置く）

J-Quants 規約によりデータはリポジトリにコミットしない（`data/` は `.gitignore` 済み）。
米国 SEC データに再配布制限は無いが、運用を揃えるため同じ扱いにする。

**例外：テストフィクスチャ。** XBRL 正規化のテストには実データが要る。SEC データに再配布制限は無いため、
Phase 2a で選んだサンプル企業の `companyfacts` JSON を数社分だけリポジトリにコミットする。
J-Quants のデータは規約上この例外を適用できないため、日本株対応時には別の方法を検討する。

## XBRL タグ正規化

`docs/data-sources.md` で「最大の手間」と書いた部分。
**概念 → 候補タグの優先順位リスト**を `sources/sec/concepts.py` に定義し、先頭から順にその会計期間のデータが存在するものを採用する。

```python
REVENUE = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606 以降の標準
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",                                       # ASC 606 以前
]
```

採用したタグを `fundamentals.source_concepts` に記録する（CLAUDE.md「数値の出典を記録する」）。
閾値ではないため YAML ではなく Python 定数に置くが、選定根拠はコメントで残す。

**優先順位は会計期間ごとに適用する。** 1社が複数のタグを併用しているのが普通で
（ASC 606 の移行をまたぐ企業は特に）、「この会社はこのタグ」と固定すると古い年度が欠ける。

### 正規化のルール（Phase 2a で実測して確定）

**全文と根拠は `docs/xbrl-findings.md`。** ここは結論だけ。

| 論点 | 決定 |
|---|---|
| **A. 年次レコードの選定** | `fy` は「報告した提出書類」の年度であって期間の年ではない（**66% でずれる**）。`start` / `end` で束ねる。年次は `end - start + 1` が **350〜380日**のものだけ（10-K の中に四半期・半期のエントリが載っている）。同じ期間が複数回出たら **`filed` が最新**を採り、`accn` と `filed` を残す。**期末が数日ずれた同一年度が19社で実在する**ので、340日未満の間隔は同一年度として束ねる。3年CAGR は「3行前」ではなく期末が1,000〜1,190日前の年度で計算する。BS項目に `start` は付かない（実測 0/2,383） |
| **B. 四半期の YTD → 3ヶ月変換** | **不要だった。** 米国 XBRL では3ヶ月値が直接報告されている（母集団の **92.4%**）。80〜100日のエントリをそのまま採る。Q4 の導出も要らない（トラックB は直近四半期 YoY しか使わない） |
| **C. 複数クラス株の株数** | **仮説は当たり。`companyfacts` に軸（dimension）付きファクトは含まれない。** そのため複数クラス株の企業では `dei:EntityCommonStockSharesOutstanding` が**丸ごと欠落する**（Alphabet・Reddit・Funko で確認）。代わりに `WeightedAverageNumberOfDilutedSharesOutstanding`（出現率 97.4%、軸なしの全クラス合計）を第1候補にする。時価総額は全クラス合計株数 × 株価で近似し、複数クラス株にはフラグを立てる |
| **D. 指標ごとの欠損率** | EBIT 93.5% / 設備投資 92.9% / 営業CF 98.9% / 売上 91.5% と、設計の前提は成り立った。ただし **`GrossProfit` はそのまま取れるのが 54.4%、原価から導出しても 71.2%**。🔴 条件の扱いは `docs/xbrl-findings.md` の提案を参照 |

タグの優先順位は `sources/sec/concepts.py` に、実測の出現率つきで書いてある。

**EBITDA は第一弾では扱わない。** 減価償却費はタグ揺れが最も激しく（`DepreciationDepletionAndAmortization` /
`DepreciationAmortizationAndAccretionNet` / CF計算書にしか現れない / 未開示）、ここに実装コストの大半が乗る。
Yartseva の「資産を膨らませているのに収益が伴わない企業を外す」という趣旨は EBIT（`OperatingIncomeLoss`、タグが安定）で保てるため、
**資産成長率 − EBIT成長率 ≤ 0** で代用する。後から EBITDA に差し替えられる形にしておく。

## 株価取得の設計（③）

### 前提：yfinance は1銘柄1リクエスト

`yf.download()` に複数ティッカーを渡しても、内部ではティッカーごとに個別のHTTPリクエストを投げている。
**リクエスト数は対象銘柄数そのもの**（約2,000回）で、差分取得しても減らない。減るのはペイロードだけ。

### 方針：429 から回復するのではなく、429 を踏まない

性能要件が「週1回完走すればよい」であり、1回の実行に丸1日以上かけても構わない。
2,000リクエストに24時間を割り当てれば1件あたり43秒まで許容できるので、速度の余裕は桁違いにある。
したがって**速く回して429を捌く設計ではなく、最初から遅く回して429を踏まない設計**にする。
②で対象を半減させておくこと、直列化（`threads=False`）、十分なスリープが主役になる。

### 適応型スロットリング

固定間隔ではなく、Yahoo が許す速度に自動収束させる。

- ベース間隔にジッターを加えて直列実行する
- **429 を受けたら、そのリクエストだけリトライするのではなく、全体の間隔を倍にする。**
  Yahoo のレート制限は IP 単位で粘着的なため、1件だけ待って再開するとすぐまた踏む。
  全体のペースを落とす方が結果的に速く終わる
- `Retry-After` ヘッダがあればそれを優先する
- 連続成功が一定回数続いたら間隔を少しずつ戻す（下限あり）

### サーキットブレーカー

適応制御でも収まらない場合の段階的退避。連続429が閾値を超えたら、30分 → 2時間 → 6時間 → 12時間と
休止時間を伸ばす。IP単位のペナルティは数時間で解ける性質のものなので、
短いリトライを繰り返すより長く待つ方が有効。丸1日使える前提なら6時間寝てから再開しても間に合う。

### 時間予算と縮退運転

run に壁時計の予算を設ける。超過したら取得を打ち切り、**その時点のデータで④以降を走らせる**。
差分更新なので、取れなかった分は次回の run に持ち越される。

ただし「どれだけ欠けた状態で判定したか」は必ず記録する。

- `screen_runs` に `price_coverage`（株価取得の成功率）を記録する
- coverage が閾値を下回ったら**通知に警告を出す**
- 株価が一定日数以上古い銘柄は CSV に鮮度フラグを付ける

これが無いと、「今週は候補が5件しか出なかった」のが相場のせいなのか取得失敗のせいなのか判別できない。

### 差分更新と、株式分割の遡及調整

銘柄ごとに `prices_daily` の `max(date)` を見て、その翌日以降を取得する。履歴が無い新規銘柄はフル取得に分岐する。

ここで必ず手当てが要るのが**株式分割**。yfinance が返す `close` / `high` / `low` / `volume` は分割調整済みのため、
分割が起きると**過去の株価がすべて遡及的に書き換わる**。差分追記では古い行が調整前の値のまま残り、
分割をまたいだ瞬間に 52週高値が実態とかけ離れた値になる（存在しない暴落として記録され、タイミング加点が誤る）。
`volume` も同じ理由で壊れ、平均売買代金が誤る。

対策として、**差分取得の開始日を `max(date) − 5営業日` にして、重複期間の `close` を既存データと突き合わせる。**

- 一致する → そのまま追記（通常ケース）
- 食い違う → 遡及調整が入った → **その銘柄だけフル再取得して置き換える**

5営業日分の余計な取得はリクエスト数を変えない（1銘柄1リクエストのため）ので、コストはゼロ。
株式分割・株式併合に加え、Yahoo 側の過去データ修正も同じ仕組みで捕捉できる。
保険として月1回はウィンドウ全体をフル再取得する。

### `adj_close` は保存しない

配当調整済み価格で、配当が出るたびに過去の値が遡及的に書き換わる。
かつ**現在の指標で使っている箇所が1つも無い**（52週高安は `high`/`low`、平均売買代金は `close × volume`、
時価総額は `close`）。保存すると「古い値が残り続けるが誰も気づかない」罠だけが残るため、列ごと持たない。

取得は `auto_adjust=False` で行う。`close`/`high`/`low` は分割調整済み・配当未調整となり、
「実際に株価がどこを通ってきたか」を見る 52週高安の用途に合う。

### 失敗時の扱いと再開

リトライ上限を超えた銘柄は `fetch_failures` に積んで**次の銘柄へ進む**（全体を止めない）。

**進捗管理用のテーブルは作らない。** 再開に必要な情報は既存の2テーブルから引き算で導出できる。

```
対象 = ユニバース通過銘柄
       − prices_daily が最新営業日に達しているもの
       − fetch_failures で恒久的失敗と判定済みのもの
```

進捗テーブルはこの引き算を書き写すだけで情報が増えないため、持たない。
run が終わったかどうか、`price_coverage` がいくつかも同じ2テーブルから導出する。

ただし `fetch_failures` は失敗の**種類**を区別する必要がある。

| `error_class` | 扱い |
|---|---|
| 429 | 一時的。次の run でもリトライする |
| シンボル不正 / データなし | 恒久的の可能性。上場廃止・ティッカー変更を疑い、N回連続なら以降スキップして `universe` が古いシグナルとして扱う |
| その他 | リトライ |

これを区別しないと、上場廃止銘柄を毎週リトライし続けることになる。逆にここさえ押さえれば進捗管理は不要。

### yfinance のバージョン

429 の挙動は yfinance のバージョンに強く依存する（cookie / crumb の取得方式、ブラウザ偽装の有無など、
この領域は上流で頻繁に修正されている）。`pyproject.toml` でバージョンをピン留めした上で、
429 が多発したら**まず上流の新しいバージョンを試す**（自前の制御を疑う前に）。

### 運用パラメータは `config/runtime.yaml` に外出しする

スロットリングも時間予算も、実際に回してみないと適正値が分からない。すべて設定ファイルに置く。

**スクリーニング閾値（`config/criteria.yaml`）とは別ファイルにする。**
`screen_runs.criteria_snapshot` に記録したいのは判定条件であって、スロットリング設定ではない。
混ぜると「閾値を変えていないのに snapshot が変わる」状態になる。

```yaml
prices:
  window_days: 315            # 取得ウィンドウ。**営業日**（52週 ≒ 252営業日 + 余裕）
  overlap_days: 5             # 遡及調整の検知に使う重複日数

  throttle:
    base_interval_sec: 3.0
    jitter_sec: 2.0
    min_interval_sec: 3.0
    max_interval_sec: 120.0
    backoff_multiplier: 2.0
    recovery_after_successes: 200
    recovery_factor: 0.9

  circuit_breaker:
    consecutive_429_threshold: 5
    pause_sec: [1800, 7200, 21600, 43200]   # 30分 → 2h → 6h → 12h

  budget:
    max_wall_clock_sec: 72000        # 20時間。あくまで目安であり調整前提
    on_exceeded: continue_next_run   # 打ち切って次回に持ち越す

  retry:
    max_attempts_per_run: 3
    permanent_error_threshold: 3

  coverage_warn_threshold: 0.90
```

初期値は保守的（間隔は長め、予算も長め）に置き、429 の発生状況を見ながら詰める。

## ファイルサイズの見積もり

前提：ユニバース4,500社 / 株価取得2,000銘柄 / 年次財務10年分 / 株価1年3ヶ月（315営業日）/ 週次52回。

| テーブル | 行数 | 初期 | 年間増分 |
|---|---|---|---|
| `universe` | 約10,000（除外分含む） | 2MB | 0 |
| `facts_annual` | **125万行**（2026-09-21 実測） | — | 入れ替えなので増えない |
| `facts_quarterly` | **31万行**（同） | — | 同上 |
| `fundamentals` | 4.5万行 | 10〜20MB | +2MB |
| `prices_daily` | 約63万行 | 20〜25MB | +20MB |
| `market_metrics` | 2,000行 | 1MB | 0 |
| `screen_runs` / `screen_results` | 約1,500行/年 | — | +0.6MB |

`universe` + `facts_annual` + `facts_quarterly` を入れた実測は **51MB**（作り直し後）。
株価が10年分積み上がっても数百MB のオーダーで、制約にはならない。
**ただし作り直しを省くと週25MB ずつ増える**（前述）。

容量を食うのは生の zip の方で、こちらは最新1世代のみ保持する（前述）。

| ファイル | 実測 |
|---|---|
| `submissions.zip` | **1.5GB**（2026-09-20 実測。991,042 エントリ） |
| `companyfacts.zip` | **1.4GB**（2026-09-20 実測。20,390社） |
| DuckDB（`universe` のみ 10,438行） | 2.3MB |

`submissions.zip` はユニバースの確定にしか使わず、上場・SIC・提出フォームは週次では
ほとんど動かない。毎回落とさず `sec.submissions_max_age_days`（既定7日）より
新しければ手元のものを使う。

定常的に必要な容量は **6〜8GB 程度**（zip 2種を1世代ずつで約3GB + DuckDB + 展開の作業領域）。

### 一括ロードは CSV 経由（`storage.bulk_writer()`）

行単位の INSERT は DuckDB では極端に遅い。**実測で20万行に240秒**かかり、
CSV に書き出して COPY すると **125万行が2.4秒**で入る。列指向のストレージに
1行ずつ追記させないための回り道で、依存は増えない。

**行を溜めずに書き出す。** 125万行をいったんリストに持つとピークメモリが 1.4GB になる。

### ファイルの作り直し（`storage.compact()`）

DuckDB は DELETE した領域をファイルに返さない。**`VACUUM` も `CHECKPOINT` も効かないことを
実測で確認した**（176MB のまま変わらない）。解放ブロックの再利用も十分には効かず、
`facts_annual` / `facts_quarterly` を週次で丸ごと入れ替えると**1回あたり約25MB 増え続ける**。

対策は別ファイルへの `COPY FROM DATABASE`。実測で **201MB → 51MB を1.1秒**で、
データもスキーマバージョンもそのまま残る。`fetch-facts` の最後で必ず実行する。

書き上がるまで元のファイルには触らないので、途中で落ちても壊れない。

## デプロイ先の前提（k8s）

| 用語 | 指すもの |
|---|---|
| `yuxsr-dev` | クラスタ名 |
| `pollux` | クラスタ内のノード名 |

第一弾ではローカル CLI 実行のみだが、CronJob 化フェーズで効いてくる制約を先に書いておく。

- **local PVC はノードローカル**。DuckDB ファイルを local-path の PVC に置く場合、
  そのボリュームは `pollux` ノードのディスク上に作られ、**Pod は `pollux` に固定される**
  （local-path provisioner は PV にノードアフィニティを付けるため、実質的に自動で pin される）。
  マルチノード構成にしてもこの CronJob は `pollux` でしか動かない点を前提にする。
- **バックアップはノード障害に対して無防備**。local PVC はレプリケーションされない。
  `fundamentals` / `prices_daily` / `screen_results` は再取得に時間がかかる（特に株価の履歴）ため、
  CronJob 化のタイミングで DuckDB ファイルの退避方法を決める。
### クラスタの流儀（`yushi-a/helm` リポジトリを実地確認）

マニフェストは **`yushi-a/helm`（別リポジトリ）** で管理されている。Helmfile + Helm + SOPS。

```
charts/<name>/                      アプリごとの Helm チャート
<name>.yaml                         Helmfile のリリース定義
environments/values/<name>.yaml.gotmpl   値
environments/secrets/<name>.yaml         SOPS(age) で暗号化した秘密値
```

デプロイは `helmfile -f <name>.yaml diff` → `apply`。

既存の `stock-notificator`（CronJob）が最も近い前例なので、チャートはこれに倣う。

- **通知先 `notificator`** は `notificator.notification.svc.cluster.local:50051`、
  環境変数 `NOTIFICATOR_ADDRESS` で渡す。詳細は後述の「通知の設計」
- **Istio サイドカーの終了処理が必須。** Pod には `sidecar.istio.io/inject: "true"` が付くため、
  本体プロセスが終わってもサイドカーが残り **Job が完了しない**。既存の `stock-notificator` は
  コマンドの末尾で `curl -X POST http://localhost:15020/quitquitquit` を呼んで明示的に落としている。
  丸1日走る本ジョブでは特に致命的なので、同じ形にする
- **ノード固定は `affinity.nodeAffinity` で hostname 指定する前例がある**（`notificator` が `low-instance-1`）。
  local PVC で `pollux` に固定する際もこれに倣う
- イメージは GHCR を使う（`ghcr.io/yushi-a/notificator` に実績あり）。
  パッケージが private の場合は対象 namespace に registry-secret が要る
- **既存チャートに PVC を使うものは無い。** local-path PVC は本アプリが初めてになる
- **CronJob には `concurrencyPolicy: Forbid` を設定する**。DuckDB は1プロセスしか書き込みモードで
  ファイルを開けない。③は丸1日かかりうるため、前回の実行が終わる前に次が起動する事態は現実的なリスク。
  `activeDeadlineSeconds` は時間予算 + 余裕、`backoffLimit` は低め（リトライはアプリ側の責務）、
  `restartPolicy: OnFailure` で再起動しても続きから走る。
- **ノードのディスク空き容量は未確認**。定常3〜5GB を前提に、local PVC を切る前に確認する。

## 通知の設計（notificator）

`yushi-a/yuxsr-dev-pb` と `yushi-a/notificator` を実地確認した結果。

### インターフェース

```proto
package yuxsr.notification.v1;

service NotificatorService {
  rpc Notify(NotifyRequest) returns (NotifyResponse);
}
message NotifyRequest { string message = 1; }
message NotifyResponse {}
```

サーバは **Connect-go のハンドラ**を h2c で `:50051` に立てている
（`notificationv1connect.NewNotificatorServiceHandler`）。
Connect / gRPC / gRPC-Web を同一ポートで受けるため、**gRPC で話す必要がない。**

### Python からは素の HTTP POST で叩く

```
POST http://notificator.notification.svc.cluster.local:50051/yuxsr.notification.v1.NotificatorService/Notify
Content-Type: application/json

{"message": "..."}
```

Connect プロトコルの unary + JSON は HTTP/1.1 の普通の POST なので、`httpx` だけで完結する。
**`grpcio` / `protobuf` への依存も、proto からのコード生成も、proto のベンダリングも不要。**
`yuxsr-dev-pb` は Go と TypeScript しか生成しておらず、Python 向けの生成物は無いため、
gRPC で話そうとすると自前でコード生成基盤を持つことになる。それを避けられる。

**未検証**：実際の疎通は確認していない（クラスタに到達できないため）。
`curl` 1回で済むので Phase 6-4 の確認項目に入れている。

### 送れるのは単一の文字列だけ

`NotifyRequest` のフィールドは `message`（string）のみ。構造化されたフィールドは無い。
通知内容はすべて1つの文字列に組み立てる。

### バックエンドは LINE

notificator は受け取ったメッセージを LINE Bot の push message として送る。ここから制約が来る。

- **LINE のテキストメッセージは5,000文字が上限。** 実用上は数百文字に収める
- **候補リスト全体は送れない。** 送るのは要約に限る（実行日、ユニバース件数、
  トラック別の通過件数、`price_coverage`、上位数銘柄、CSV のパス）
- **CSV の添付はこの経路ではできない。** CSV は PVC 上に置き、通知にはパスだけ載せる

### 認証は不要の見込み

proto にも notificator の実装にも認証の要素が無く、クラスタ内通信のため。
**SOPS の secret は不要になる可能性が高い**（Phase 6 で確定させる）。

## SEC アクセスの作法

- User-Agent に連絡先を含める（SEC Developer FAQ）
- 一括取得は `companyfacts.zip`（毎晩 ET 3:00頃再生成）を使い、企業ごとの API 連打はしない
- `submissions` は初回のみ全社分、以降は差分

## リスクと緩和

| リスク | 緩和策 |
|---|---|
| yfinance の 429 で③が止まる | 適応型スロットリングで踏まないようにする。踏んだらサーキットブレーカーで長く待つ。時間予算を超えたら縮退運転（coverage を記録して通知で警告）。保険として FMP Starter（$22/月） |
| XBRL の欠損率が想定より高い | Phase 2 の検証で欠損率を計測して出力し、早期に判明させる |
| 通過数が0件または数百件になる | `criteria_snapshot` を使って閾値調整の試行を記録。Phase 4 の検証項目に「通過数が20〜30件のオーダーか」を入れる |
| 通知先 `notificator` のインターフェースが未確認 | 第一弾では通知を最後に実装する。CSV 出力までが動けば運用は始められるため、ブロッカーにはしない |
| 株式分割の遡及調整で `prices_daily` が壊れる | 差分取得時に重複期間の `close` を突き合わせて検知し、該当銘柄をフル再取得（前述）。月1回のフル再取得も併用 |
