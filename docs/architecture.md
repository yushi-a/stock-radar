# システム構成（2026-09-20 合意）

米国株先行版の設計。日本株は同じパイプライン骨格に別ソースを差し込む形で後から追加する。

## 決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| 初回対象 | 米国株 | 10倍銘柄の発生率が日本の3倍以上（米 約7〜8%/15年 vs 日 約2%/10年）。かつ SEC + yfinance で完全無料・データ制約なし。J-Quants は Free のままだと12週遅延・2年分のみで3年CAGRが計算できない |
| 財務ソース | SEC EDGAR `companyfacts.zip` | 全社分を1回のダウンロードで取得でき、レート制限を気にせず全銘柄処理できる |
| 株価ソース | yfinance（失敗時 FMP） | 無料。429 対策としてスロットリング・差分取得・再開可能性を設計に織り込む |
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
   SEC company_tickers.json + submissions
   → SIC で金融・REIT 除外 / 10-K 提出企業のみ（ADR 除外）
   約6,000社 → 約4,500社                        [コスト0・小さなAPI呼び出しのみ]

② 財務指標を全社計算
   companyfacts.zip を1回ダウンロード → 全社分を正規化
   → 自己資本プラス・売上ゼロ除外・売上成長率で足切り
   約4,500社 → 約2,000社                        [ネットワーク1回・あとはローカル処理]

③ 株価を取得
   ②の通過分だけ yfinance で日次1年3ヶ月分（差分追記）
   → 時価総額・平均売買代金・52週高安
   約2,000社                                     [ここだけが重い。初回フル、以降は差分]

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
  config.py                 criteria.yaml の読み込みと型付け
  storage.py                DuckDB 接続・スキーマ定義・マイグレーション
  sources/
    sec/
      universe.py           company_tickers / submissions → ユニバース
      companyfacts.py       zip のダウンロードとストリーム展開
      concepts.py           XBRL タグの優先順位マッピング（後述）
      normalize.py          年次 / 四半期レコードの正規化
    prices/
      yfinance_client.py    差分取得・スロットリング・リトライ
  metrics/
    fundamentals.py         売上CAGR・利益率・FCF・ROA/ROE・資産成長率
    market.py               時価総額・売買代金・52週高安・PBR/PSR/FCF利回り
  screen/
    filters.py              共通足切り
    track_a.py / track_b.py
    timing.py               並べ替え用スコア
  output/
    csv_writer.py
    notify.py               notificator への送信（インターフェースは実装時に確認）
```

## DuckDB スキーマ（案）

| テーブル | 役割 | 永続 |
|---|---|---|
| `universe` | cik, ticker, name, sic, exchange, form_type, updated_at | 再生成可 |
| `facts_annual` | 縦持ち。cik, fiscal_year, period_end, concept, value, unit, accn, filed_at | 再生成可（zip があれば） |
| `facts_quarterly` | 同上。売上関連のみ | 再生成可 |
| `fundamentals` | 横持ち・正規化後。revenue, operating_income, net_income, total_assets, equity, cfo, capex, gross_profit, shares_outstanding, `source_concepts`(JSON) | **必要** |
| `prices_daily` | ticker, date, open, high, low, close, volume。主キー `(ticker, date)` | **必要**（差分追記） |
| `fetch_failures` | ticker, last_attempt, error, retry_count。翌日リトライ用のキュー | **必要** |
| `market_metrics` | ticker, as_of, market_cap, avg_daily_value, high_52w, low_52w, range_position_52w, drawdown_from_52w_high | 再生成可 |
| `screen_runs` | run_id, run_at, `criteria_snapshot`(JSON), universe_size, passed_count | **必要** |
| `screen_results` | run_id, cik, ticker, track, passed_filters, timing_score, 各指標値, data_source, as_of | **必要** |

`screen_runs.criteria_snapshot` に閾値そのものを保存する。
これにより「この結果はどの閾値で出たか」を後から完全に再現でき、閾値調整の試行錯誤が記録として残る。

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

**EBITDA は第一弾では扱わない。** 減価償却費はタグ揺れが最も激しく（`DepreciationDepletionAndAmortization` /
`DepreciationAmortizationAndAccretionNet` / CF計算書にしか現れない / 未開示）、ここに実装コストの大半が乗る。
Yartseva の「資産を膨らませているのに収益が伴わない企業を外す」という趣旨は EBIT（`OperatingIncomeLoss`、タグが安定）で保てるため、
**資産成長率 − EBIT成長率 ≤ 0** で代用する。後から EBITDA に差し替えられる形にしておく。

## 株価取得の設計（③）

### 前提：yfinance は1銘柄1リクエスト

`yf.download()` に複数ティッカーを渡しても、内部ではティッカーごとに個別のHTTPリクエストを投げている。
**リクエスト数は対象銘柄数そのもの**（約2,000回）で、差分取得しても減らない。減るのはペイロードだけ。
429 対策の主役は、②で対象を半減させておくことと、直列化（`threads=False`）＋スリープ。
1リクエスト1秒として30〜40分。週次なら許容範囲とする。

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

### 失敗時の扱い

429 は指数バックオフ。リトライ上限を超えた銘柄は `fetch_failures` に積んで**次の銘柄へ進む**（全体を止めない）。
失敗分は翌日リトライ。取得失敗が継続する銘柄は上場廃止・ティッカー変更の可能性があるため、
`universe` の情報が古いというシグナルとして扱う。

## ファイルサイズの見積もり

前提：ユニバース4,500社 / 株価取得2,000銘柄 / 年次財務10年分 / 株価1年3ヶ月（315営業日）/ 週次52回。

| テーブル | 行数 | 初期 | 年間増分 |
|---|---|---|---|
| `universe` | 約10,000（除外分含む） | 2MB | 0 |
| `facts_annual` | 約68万行 | 15〜20MB | +2MB |
| `facts_quarterly` | 約54万行 | 5〜10MB | +1MB |
| `fundamentals` | 4.5万行 | 10〜20MB | +2MB |
| `prices_daily` | 約63万行 | 20〜25MB | +20MB |
| `market_metrics` | 2,000行 | 1MB | 0 |
| `screen_runs` / `screen_results` | 約1,500行/年 | — | +0.6MB |

**DuckDB ファイルは差分追記でも10年で270MB程度**。制約にならない。

容量を食うのは `companyfacts.zip`（1GB超）の方で、こちらは最新1世代のみ保持する（前述）。
定常的に必要な容量は **3〜5GB 程度**（zip 1世代 + DuckDB + 作業領域）。

なお DuckDB は DELETE してもファイルが縮まない（解放ブロックは内部で再利用される）。
分割検知によるフル再取得が多発した場合などにファイルが膨らんだら、`VACUUM` か再構築で縮める。

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
- **通知の到達経路**。`notificator` は同一クラスタ内で動いているため、
  Service 名（ClusterIP）で直接叩ける。Ingress を経由する必要はない。
- **CronJob には `concurrencyPolicy: Forbid` を設定する**。DuckDB は1プロセスしか書き込みモードで
  ファイルを開けない。③が30〜40分かかるため、前回の実行が終わる前に次が起動する事態は現実に起こりうる。
- **ノードのディスク空き容量は未確認**。定常3〜5GB を前提に、local PVC を切る前に確認する。

## SEC アクセスの作法

- User-Agent に連絡先を含める（SEC Developer FAQ）
- 一括取得は `companyfacts.zip`（毎晩 ET 3:00頃再生成）を使い、企業ごとの API 連打はしない
- `submissions` は初回のみ全社分、以降は差分

## リスクと緩和

| リスク | 緩和策 |
|---|---|
| yfinance の 429 で③が止まる | 差分取得＋再開可能設計。失敗ティッカーをキューに残して翌日リトライ。保険として FMP Starter（$22/月） |
| XBRL の欠損率が想定より高い | Phase 2 の検証で欠損率を計測して出力し、早期に判明させる |
| 通過数が0件または数百件になる | `criteria_snapshot` を使って閾値調整の試行を記録。Phase 4 の検証項目に「通過数が20〜30件のオーダーか」を入れる |
| 通知先 `notificator` のインターフェースが未確認 | 第一弾では通知を最後に実装する。CSV 出力までが動けば運用は始められるため、ブロッカーにはしない |
| 株式分割の遡及調整で `prices_daily` が壊れる | 差分取得時に重複期間の `close` を突き合わせて検知し、該当銘柄をフル再取得（前述）。月1回のフル再取得も併用 |
