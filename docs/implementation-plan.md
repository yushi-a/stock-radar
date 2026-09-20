# 実装計画（米国株・第一弾）

第一弾のゴール：**手元で `stock-radar run --market us` を叩くと、候補 CSV が出て notificator に通知が飛ぶ。**

Kill 条件監視・フィードバック記録・K3s CronJob 化・日本株対応は第一弾のスコープ外（合意済み）。

各フェーズに検証項目を置く。ここを飛ばすと、誤った財務値の上に条件を積んでしまい、
出てきた候補が正しいのかどうか判断できなくなる。

## Phase 0：足場

- `uv init` / `pyproject.toml` / `uv.lock`
- ruff + pytest + `.env.example`（`SEC_USER_AGENT`, `NTFY_URL`, `NTFY_TOPIC`）
- `storage.py`：DuckDB 接続とスキーマ定義
- `config.py`：設定ファイルの読み込みと型付け
  - `config/criteria.yaml`：スクリーニング閾値（`criteria.example.yaml` からコピーして使う）
  - `config/runtime.yaml`：運用パラメータ（スロットリング、時間予算など）。`criteria.yaml` とは別ファイルにする

## Phase 1：ユニバース確定

- `company_tickers_exchange.json` で ticker / 取引所を取得
- `submissions` で SIC コードと提出フォーム種別を取得
- 除外：SIC 6000番台（金融・REIT）／10-K 非提出（＝ ADR、20-F/6-K 提出企業）／OTC
- `universe` テーブルへ

**検証**：残存件数が約4,000〜4,500社のオーダーに収まるか。
極端にずれる場合は除外条件の実装ミスを疑う。除外理由別の件数も出す。

## Phase 2：財務指標

- `companyfacts.zip` をダウンロード（User-Agent 必須）してストリーム展開
- `concepts.py` の優先順位マッピングで年次レコードを正規化 → `fundamentals`
- 売上のみ四半期レコードも正規化（YTD / 3ヶ月値の判別が必要）→ `facts_quarterly`
- 計算：売上3年CAGR、営業利益率、粗利率、FCF（営業CF − 設備投資）、ROA（分子は純利益）、ROE、
  自己資本比率、流動比率、資産成長率、EBIT成長率

**検証**：
- 有名銘柄10社程度を選び、実際の 10-K と数値を手で突き合わせる
- 指標ごとの欠損率を出力する。粗利率（`GrossProfit` 未開示企業が一定数ある）と
  設備投資の欠損率は特に確認する
- 3年CAGR は「累積+33% ≒ 年率10%」の定義で自前計算していることをテストで固定する

## Phase 3：株価

- Phase 2 の通過銘柄のみ yfinance で日次1年3ヶ月分（315営業日）を取得。`auto_adjust=False`
- **429 対策**（詳細は `docs/architecture.md`）。パラメータはすべて `config/runtime.yaml`
  - 直列化（`threads=False`）＋ジッター付きスリープで、そもそも 429 を踏まない速度で回す
  - 429 を受けたら**全体の間隔を倍にする**（そのリクエストだけリトライしない）。`Retry-After` があれば優先
  - 連続429が続いたらサーキットブレーカーで 30分 → 2h → 6h → 12h と退避
  - 時間予算を超えたら打ち切って次の run に持ち越す（縮退運転）
  - リトライ上限を超えた銘柄は `fetch_failures` へ積んで次の銘柄へ進む。`error_class` で
    一時的（429）と恒久的（シンボル不正＝上場廃止・ティッカー変更の疑い）を区別する
- **進捗管理テーブルは作らない。** 再開対象は `prices_daily` と `fetch_failures` から引き算で導出する
- `prices_daily` へ差分追記。銘柄ごとに `max(date)` を見て起点を決め、履歴が無い銘柄はフル取得に分岐
- **株式分割の遡及調整への対策**：開始日を `max(date) − 5営業日` にして重複期間の `close` を既存と突き合わせ、
  食い違えばその銘柄をフル再取得して置き換える（詳細と理由は `docs/architecture.md`）
- `adj_close` は保存しない（使っている指標が無く、配当のたびに遡及的に書き換わるため）
- `market_metrics` を計算：時価総額（株数 × 株価）、平均売買代金、52週高安、レンジ内位置、高値からの下落率
- `screen_runs.price_coverage`（株価取得の成功率）を記録し、閾値を下回ったら通知で警告する

**検証**：
- 自前計算した時価総額を yfinance の `marketCap` と照合し、乖離率の分布を見る。
  大きくずれる銘柄は発行済株式数の取得ミス（複数クラス株など）を疑う。
  **`marketCap` は `history()` と別リクエストになるため、照合はサンプル200銘柄程度に限定する**（全銘柄だとリクエスト数が倍になる）
- 分割が起きた銘柄を1つ以上選び、重複期間チェックが実際にフル再取得へ分岐することを確認する
- **429 の発生状況を記録して `config/runtime.yaml` の初期値を詰める。** 初期値は保守的（間隔・予算ともに長め）に置き、
  実測してから短くする。ここで得た実行時間が CronJob 化フェーズの `activeDeadlineSeconds` の根拠になる
- 途中で中断して再実行し、取得済み銘柄をスキップして続きから走ることを確認する

## Phase 4：スクリーニング

- 共通足切り → トラックA / トラックB → タイミング加点による並べ替え
- 通過理由（どの条件を満たしたか）を銘柄ごとに記録
- `screen_runs`（`criteria_snapshot` 込み）/ `screen_results` へ保存

**検証**：通過数が20〜30件のオーダーに収まるか。
外れている場合は閾値を調整するが、その試行は `screen_runs` に残る。

## Phase 5：出力

- CSV：`output/2026-09-20_us.csv`
  列は `docs/skill-integration.md` のたたき台＋各指標のデータソースと基準日（決算期・取得日）
- 通知：実行日、ユニバース件数、通過件数（トラック別）、上位銘柄、CSV パス
  - 送信先は同一クラスタ内にデプロイ済みの自前アプリ `notificator`（CronJob 化後は Service 名で到達可能）
  - **インターフェース（エンドポイント・ペイロード形式・認証）は未確認。実装時に確認する**
  - 通知は Phase 5 の最後に回す。CSV 出力までが動けば運用は始められるため、ここをブロッカーにしない
- CLI：`stock-radar run --market us --criteria config/criteria.yaml --runtime config/runtime.yaml`
  フェーズ単位でも実行できるようにする（`fetch-universe` / `fetch-facts` / `fetch-prices` / `screen`）
- `companyfacts.zip` はパース後に古い世代を削除する（最新1世代のみ保持）

## 第一弾より後

1. K3s CronJob 化（実行時刻をここで決定。推奨は土曜朝 JST）
   - local PVC を使う場合、Pod は `pollux` ノードに固定される（`docs/architecture.md` 参照）
   - **`concurrencyPolicy: Forbid` を設定する**（DuckDB は同時書き込み不可。Phase 3 が丸1日かかりうるため重複起動は現実的なリスク）
   - `activeDeadlineSeconds` は時間予算 + 余裕。`backoffLimit` は低め（リトライはアプリ側の責務）、
     `restartPolicy: OnFailure` で再起動しても続きから走る
   - DuckDB ファイルのバックアップ方法もここで決める（書き込み中のコピーは壊れるため、ジョブ実行時間外に取る）
2. 前週差分・Kill 条件監視（`screen_results` の履歴を使う）
3. 日本株対応（J-Quants。プラン選定を再検討する）
4. 評価スキルへの CSV 入口の追加、フィードバック記録

## 依存関係

```
Phase 0 ─→ Phase 1 ─→ Phase 2 ─→ Phase 3 ─→ Phase 4 ─→ Phase 5
                          └─ ここが最もコストが高い（XBRL 正規化）
```

Phase 2 が山。ここの検証を厚くして、以降のフェーズが誤った財務値の上に乗らないようにする。
