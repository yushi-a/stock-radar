# 実装計画（米国株・第一弾）

第一弾のゴール：**手元で `stock-radar run --market us` を叩くと、候補 CSV が出て notificator に通知が飛ぶ。**

Kill 条件監視・フィードバック記録・pollux の CronJob 化・日本株対応は第一弾のスコープ外（合意済み）。

各フェーズに検証項目を置く。ここを飛ばすと、誤った財務値の上に条件を積んでしまい、
出てきた候補が正しいのかどうか判断できなくなる。

## Phase 0：足場

- `uv init` / `pyproject.toml` / `uv.lock`
- ruff + pytest + `.env.example`（`SEC_USER_AGENT`, `NTFY_URL`, `NTFY_TOPIC`）
- `storage.py`：DuckDB 接続とスキーマ定義
- `config.py`：`config/criteria.yaml` の読み込みと型付け（`criteria.example.yaml` からコピーして使う）

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

- Phase 2 の通過銘柄のみ yfinance で日次2年分を取得
- バッチ分割（50〜100銘柄）、リクエスト間スリープ、指数バックオフ、429 は失敗キューへ
- `prices_daily` に既存最終日以降だけ追記（差分）
- `market_metrics` を計算：時価総額（株数 × 株価）、平均売買代金、52週高安、レンジ内位置、高値からの下落率

**検証**：自前計算した時価総額を yfinance の `marketCap` と照合し、乖離率の分布を見る。
大きくずれる銘柄は発行済株式数の取得ミス（複数クラス株など）を疑う。

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
  - 送信先は yuxsr-dev クラスタ（k8s）にデプロイ済みの自前アプリ `notificator`
  - **インターフェース（エンドポイント・ペイロード形式・認証）は未確認。実装時に確認する**
  - 通知は Phase 5 の最後に回す。CSV 出力までが動けば運用は始められるため、ここをブロッカーにしない
- CLI：`stock-radar run --market us --config config/criteria.yaml`
  フェーズ単位でも実行できるようにする（`fetch-universe` / `fetch-facts` / `fetch-prices` / `screen`）

## 第一弾より後

1. pollux の K3s CronJob 化（実行時刻をここで決定。推奨は土曜朝 JST）
2. 前週差分・Kill 条件監視（`screen_results` の履歴を使う）
3. 日本株対応（J-Quants。プラン選定を再検討する）
4. 評価スキルへの CSV 入口の追加、フィードバック記録

## 依存関係

```
Phase 0 ─→ Phase 1 ─→ Phase 2 ─→ Phase 3 ─→ Phase 4 ─→ Phase 5
                          └─ ここが最もコストが高い（XBRL 正規化）
```

Phase 2 が山。ここの検証を厚くして、以降のフェーズが誤った財務値の上に乗らないようにする。
