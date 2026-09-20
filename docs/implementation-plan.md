# 実装計画（米国株・第一弾）

第一弾のゴール：**`yuxsr-dev` クラスタ上で週次 CronJob が回り、候補 CSV が出て notificator に通知が飛ぶ。**

Kill 条件監視・フィードバック記録・日本株対応は第一弾のスコープ外（合意済み）。
コンテナ化とデプロイ（Phase 6）は当初スコープ外だったが、2026-09-20 に第一弾へ含めることをユーザーが決定した。

各フェーズに検証項目を置く。ここを飛ばすと、誤った財務値の上に条件を積んでしまい、
出てきた候補が正しいのかどうか判断できなくなる。

**各フェーズの「検証」は大半が人が目視で行うもの**（件数のオーダー確認、乖離率の分布、10-K との突合）。
自動テストにする範囲とその方針は `docs/testing.md` を参照。

## Phase 0：足場

- `uv init` / `pyproject.toml` / `uv.lock`
- ruff + pytest + `.env.example`（`SEC_USER_AGENT`, `NOTIFICATOR_ADDRESS`）
- テスト中の外部アクセスを遮断する設定（`pytest-socket` 等）。方針は `docs/testing.md`
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

## Phase 2a：データ構造の実地調査

**本実装の前に、`companyfacts` の実データを見て正規化ルールを決めるフェーズ。**
XBRL の読み方は仕様書だけでは決めきれず、ここを飛ばすと「動いてはいるが数字が正しいか分からない」実装になる。
成果物はコードではなく**調査ノートと決定事項**。使ったスクリプトは使い捨てで、本実装には持ち込まない。

### サンプル企業の選定

大型株だけを見ると罠を見落とす。**意図的に偏らせた15〜20社**を選ぶ。

| 観点 | 例 |
|---|---|
| 非12月決算 | Apple（9月）、Walmart（1月） |
| 複数クラス株 | Alphabet（GOOG/GOOGL）、Berkshire（BRK.A/BRK.B） |
| 粗利を開示しない業態 | サービス業・金融周辺 |
| ASC 606 移行をまたぐ古い企業 | `Revenues` → `RevenueFromContractWithCustomer...` の切替を見る |
| 決算期変更の経験企業 | 12ヶ月でない「年度」の実例 |
| 最近IPOした企業 | 履歴が3年未満で 3年CAGR が計算できないケース |
| **小型株** | **ユニバースの実際の中心層。ここが一番重要** |

### 調べること

**A. 年次レコードの選定ルール**
- 同一 `fy` に対してエントリがいくつ出るか（元の10-K + 翌年の比較欄での再掲）
- `filed` 違いで値が変わる頻度と幅（＝修正再提出の実態）。「当時報告された値」と「最新の値」のどちらを採るか決める
- `end - start` の分布。決算期変更による非12ヶ月期間が実在するか。350〜380日でガードする方針の妥当性
- BS項目（時点値、`end` のみ）と PL/CF項目（期間値、`start`+`end`）の扱い分け

**B. 四半期の YTD → 3ヶ月 変換**
- 四半期レコードが YTD か3ヶ月値か、`fp`（Q1/Q2/Q3/FY）と `start`/`end` の関係
- Q4 が単独で出るか（出ないなら 通期 − YTD(Q3) で導出）
- Q2 = YTD(Q2) − YTD(Q1) のような引き算ルールを確定する

**C. 複数クラス株の株数**
- `dei:EntityCommonStockSharesOutstanding` のエントリ数とクラスの区別方法
- **要検証の仮説：`companyfacts` は軸（dimension）付きファクトを含まない可能性がある。**
  もし含まないならクラス別株数はここから取れず、別ルート（`submissions` の表紙情報など）が必要になる
- どこまで厳密にやるか。近似で済ませてフラグを立てる判断もありうる

**D. 指標ごとの欠損率（zip 全体をスキャン）**

サンプルではなく全社で測る。**条件そのものの見直しにつながりうる、最も重要な調査。**

| 指標 | 見る理由 |
|---|---|
| `GrossProfit` | 粗利率はトラックBの条件。欠損が多ければ足切りに使えない |
| 設備投資 | FCF の計算に必須 |
| `OperatingIncomeLoss` | EBIT はトラックAの条件。タグが安定している前提の検証 |
| 売上（各タグ） | 優先順位リストの妥当性 |

### 成果物

- `docs/xbrl-findings.md`（調査結果と、A〜C の決定とその根拠）
- `docs/architecture.md` への決定事項の反映
- `concepts.py` のタグ優先順位リストの初版
- **欠損率が高い指標があれば、スクリーニング条件の見直し提案**
- サンプル企業の `companyfacts` JSON を**テストフィクスチャとして確定**する
  （SEC データに再配布制限は無いため、テスト用に数社分をコミットする方針とする。
  `docs/architecture.md` の「SEC も J-Quants と同じ扱い」はこの用途を想定していなかったので併せて整理する）

## Phase 2b：財務指標の実装

Phase 2a で決めたルールを実装する。

- `companyfacts.zip` をダウンロード（User-Agent 必須）してストリーム展開
- `concepts.py` の優先順位マッピングで年次レコードを正規化 → `fundamentals`
- 売上のみ四半期レコードも正規化 → `facts_quarterly`
- 計算：売上3年CAGR、営業利益率、粗利率、FCF（営業CF − 設備投資）、ROA（分子は純利益）、ROE、
  自己資本比率、流動比率、資産成長率、EBIT成長率

**検証**：
- Phase 2a のサンプル企業について、実際の 10-K と数値を手で突き合わせる
- フィクスチャを使ったゴールデンテストで、A〜C の判断ルールを固定する
  （決算期変更、YTD→3ヶ月変換、複数クラス株のそれぞれにテストを置く）。詳細は `docs/testing.md`
- 指標計算の境界値テスト（分母ゼロ、自己資本マイナス、履歴3年未満、欠損の伝播）
- 指標ごとの欠損率が Phase 2a の計測値と一致するか
- 3年CAGR は「累積+33% ≒ 年率10%」の定義で自前計算していることをテストで固定する

## Phase 3：株価

- Phase 2b の通過銘柄のみ yfinance で日次1年3ヶ月分（315営業日）を取得。`auto_adjust=False`
- **HTTP 層は差し替え可能にする。** 429 を返す偽レスポンスでのテストと、FMP への差し替え（要件R5）の両方に要る
- **待ち時間の「計算」と「実際に待つ」を分ける。** 計算側を純粋関数にすればテストで一度も待たずに済む（`docs/testing.md`）
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
- 通知：実行日、ユニバース件数、通過件数（トラック別）、`price_coverage`、上位数銘柄、CSV パス
  - 送信先は `notificator`。**Connect プロトコルの JSON を `httpx` で POST する**（詳細は `docs/architecture.md`）。
    `grpcio` や proto のコード生成は不要
  - **バックエンドが LINE のため、送れるのは単一の文字列で実用上は数百文字。
    候補リスト全体は送らず要約に絞る。CSV は PVC 上に置いてパスだけ載せる**
  - 通知は Phase 5 の最後に回す。CSV 出力までが動けば運用は始められるため、ここをブロッカーにしない
- CLI：`stock-radar run --market us --criteria config/criteria.yaml --runtime config/runtime.yaml`
  フェーズ単位でも実行できるようにする（`fetch-universe` / `fetch-facts` / `fetch-prices` / `screen`）
- `companyfacts.zip` はパース後に古い世代を削除する（最新1世代のみ保持）

## Phase 6：コンテナ化とデプロイ

マニフェストは **`yushi-a/helm`（別リポジトリ）** に置く。既存の `stock-notificator` チャートが最も近い前例。
クラスタの流儀は `docs/architecture.md` の「クラスタの流儀」を参照。

### 6-1. Dockerfile（本リポジトリ）

- uv ベースのマルチステージビルド、非 root 実行
- ローカルでビルドして `stock-radar --help` が動くところまで確認

### 6-2. GHCR への push（本リポジトリ）

- GitHub Actions で main への push 時にビルドして `ghcr.io/yushi-a/stock-radar` へ
- タグは git SHA と semver。`GITHUB_TOKEN` で認証できるため追加のシークレットは不要

### 6-3. Helm チャート（helm リポジトリ）

`charts/stock-radar/` を `stock-notificator` に倣って作る。差分は以下。

- **PVC（local-path）を追加する。** 既存チャートに前例が無いので新規に書く。
  DuckDB ファイルと `data/raw/` を置く。容量は 10GB 程度（`docs/architecture.md` の見積もり）
- **`affinity.nodeAffinity` で `pollux` に固定する**（local PVC はノードローカルのため）
- **Istio サイドカーの終了処理**を command の末尾に入れる。
  これが無いと本体が終わっても Job が完了しない
- `concurrencyPolicy: Forbid`（`stock-notificator` のテンプレートでは既定で入っている）
- `activeDeadlineSeconds` は時間予算 + 余裕、`backoffLimit` は低め
- `criteria.yaml` / `runtime.yaml` は ConfigMap でマウントする
- namespace は新規に切る想定。private イメージなら registry-secret も新 namespace に要る
- **SOPS の secret は不要の見込み。** notificator に認証は無く、`SEC_USER_AGENT` は
  SEC に開示する連絡先であって秘密ではない

**検証**：`helm template` / `helm lint` と `helmfile diff` まで。

### 6-4. デプロイと実環境での動作確認 🔴 ゲート

`helmfile -f stock-radar.yaml diff` → `apply` の後、手動トリガーで確認する。

```bash
kubectl create job --from=cronjob/stock-radar stock-radar-manual-1 -n <ns>
```

| 確認項目 | 見るもの |
|---|---|
| 少数銘柄での疎通 | `--limit 10` 相当で一周する |
| **Job が Complete になる** | Istio サイドカーが落ちているか。ここが最も踏みやすい |
| 全銘柄での完走 | 丸1日走らせて最後まで行くか。429 の実挙動もここで分かる |
| PVC の永続 | 2回目の実行が差分で走るか |
| リソース実測 | メモリ・ディスクを測り、`resources` の limits を確定する |
| notificator への通知 | Connect の JSON POST が通り、LINE に届くか。`curl` 1回で確認できる |

## 第一弾より後

1. 前週差分・Kill 条件監視（`screen_results` の履歴を使う）
2. 日本株対応（J-Quants。プラン選定を再検討する）
3. 評価スキルへの CSV 入口の追加、フィードバック記録
4. DuckDB ファイルのバックアップ（書き込み中のコピーは壊れるため、ジョブ実行時間外に取る）

## 依存関係

```
Phase 0 ─→ Phase 1 ─→ Phase 2a ─→ Phase 2b ─→ Phase 3 ─→ Phase 4 ─→ Phase 5 ─→ Phase 6
                      （調査）     （実装）                                      （デプロイ）
                         └──────────┘
                         ここが山（XBRL 正規化）
```

Phase 2 が山。調査（2a）と実装（2b）を分けているのは、XBRL の読み方が仕様書だけでは決まらず、
実データを見ないと正規化ルールを確定できないため。ここの検証を厚くして、
以降のフェーズが誤った財務値の上に乗らないようにする。

なお Phase 2a の欠損率調査の結果によっては、**スクリーニング条件そのものの見直しが必要になりうる**
（例：`GrossProfit` の欠損が多ければ、トラックBの粗利率をハードフィルタにできない）。
その場合は Phase 2b に進む前にユーザーと条件を再合意する。
