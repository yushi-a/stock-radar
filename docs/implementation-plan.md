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
  - `config/criteria.yaml`：スクリーニング閾値。リポジトリにコミットする
    （秘密値は含まず、K8s では ConfigMap でマウントするため実体が要る）
  - `config/runtime.yaml`：運用パラメータ（スロットリング、時間予算など）。`criteria.yaml` とは別ファイルにする

## Phase 1：ユニバース確定

- `company_tickers_exchange.json` で ticker / 取引所を取得
- `submissions.zip` で SIC コードと提出フォーム種別を取得
  （1.5GB。企業ごとの API だと 6,067リクエストになるため一括で取る）
- 除外：OTC / 取引所なし → SIC 6000番台（金融・REIT）→ 10-K 非提出（＝ ADR、20-F/40-F 提出企業）
- `universe` テーブルへ。**除外した銘柄も行として残し、`excluded_reason` を付ける**

### 検証結果（2026-09-20 実測）

| | ティッカー行 |
|---|---|
| SEC の全ティッカー | 10,438 |
| 除外: `otc` | 2,506 |
| 除外: `no_exchange`（取引所が `null`） | 219 |
| 除外: `financial_sic`（SIC 6000〜6999） | 2,555 |
| 除外: `no_10k`（20-F 896 / 40-F 94 / 年次報告なし 529 ほか） | 1,598 |
| **残存** | **3,560**（ユニーク CIK **3,189**） |

**想定していた「約4,000〜4,500社」を下回った。** 実装ミスを疑って中身を確認した結果、
除外はいずれも設計どおりで、見直すべきは想定値の方だった。

- `financial_sic` 2,555 の内訳は SPAC（SIC 6770）846、REIT（6798）343、銀行（6021/6022/6029）475、
  ETF・資産運用（6199/6221/6211/6282）ほか。**SPAC と ETF は売上ゼロ条件でどのみち落ちる**
- `no_10k` のうち年次報告を出していない529件の多くは**クローズドエンドファンド**（N-CSR 提出）
- **`filings.recent` の1,000件上限で 10-K を取り逃していないことを確認した。**
  SIC があるのに年次報告フォームが無い127社について古いシャードを全部見たが、
  10-K があったものは0件
- 大型株（AAPL / MSFT / NVDA / GOOGL / LLY / WMT）は残存。
  **Berkshire Hathaway は SIC 6331（保険）で除外される** — 合意済みの条件どおり

`submissions.zip` は `sec.submissions_max_age_days`（既定7日）より新しければ再取得しない。

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
- **判定は三値**（通過 / 閾値未満 / 判定不能）。判定不能は落とすが、記録は分ける
- **財務による足切りを `fetch-prices` に繋ぐ。** 株価が要らない条件を先に当てて
  対象を減らす（CLAUDE.md の 429 対策の中核）。Phase 3 の時点では足切りの実装が
  無く、ユニバース通過銘柄をそのまま取りに行っていた

**検証**：通過数が20〜30件のオーダーに収まるか。
外れている場合は閾値を調整するが、その試行は `screen_runs` に残る。

通過件数だけでは閾値をどちらに動かせばよいか分からないので、**落とした条件の内訳を
「閾値未満」と「判定不能」に分けて出す**。前者は緩めれば増えるが、後者は緩めても増えない。

⚠️ **全銘柄の株価はデプロイ後まで揃わない**（ゲート G2 の決定）。それまでは
`screen --no-price-filters` で株価が要らない条件だけを当て、**株価で絞る前の上限値**を見る。

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
  - `run` は上の4つを順に呼ぶだけ。**順序はパイプラインそのままで崩さない**（株価は財務の足切りの後）
  - 途中で失敗したらそこで止めて非ゼロを返す。半端なデータで候補を出さない
- `companyfacts.zip` はパース後に古い世代を削除する（最新1世代のみ保持）

## Phase 6：コンテナ化とデプロイ

マニフェストは **`yushi-a/helm`（別リポジトリ）** に置く。既存の `stock-notificator` チャートが最も近い前例。
クラスタの流儀は `docs/architecture.md` の「クラスタの流儀」を参照。

### 6-1. Dockerfile（本リポジトリ）✅ 2026-09-21

- uv ベースのマルチステージビルド、非 root 実行（uid 10001）
- ビルド層で `uv sync --locked --no-dev --no-editable`。実行層へ運ぶのは `.venv` と `config/` だけ
- **`curl` を実行層に入れてある。** Istio サイドカーの `/quitquitquit` に要る（無いと Job が完了しない）
- `data/` `output/` は相対パスで参照しているので `/app` 配下に器を作る。クラスタでは PVC をマウントする

実測（2026-09-21、ローカル）：

| | |
|---|---|
| イメージサイズ | 409MB（大半は yfinance が引く pandas / numpy） |
| ビルド時間 | 24秒（キャッシュなし） |
| `stock-radar --help` | 動く |
| 実行ユーザー | uid=10001(app)。`--user` で任意の UID を与えても動く |
| 実データでの `screen` | 手元の DuckDB をマウントして完走。候補22件・CSV 出力までローカルと一致 |

### 6-2. GHCR への push（本リポジトリ）✅ 2026-09-21

`.github/workflows/image.yml`。

- main への push とタグ（`v*.*.*`）でビルドして `ghcr.io/yushi-a/stock-radar` へ push する
- **PR ではビルドして起動確認だけ行い push しない。** Dockerfile の破損を main の手前で拾う
- タグは `sha-<full sha>` / `latest`（main のみ）/ semver。`GITHUB_TOKEN` で認証でき、追加のシークレットは不要
- **Helmfile から参照するのは `sha-` タグ**。どのコミットが動いているかをイメージ名だけで特定できる
- ビルド後に `--help` と「非 root であること・`curl` があること」を実際に走らせて見る。
  ビルドが通るだけでは実行層の壊れを拾えない

### 6-3. Helm チャートとリリース定義（helm リポジトリ）✅ 2026-09-21（PR 提出済み・マージ待ち）

`yushi-a/helm` の [PR #8](https://github.com/yushi-a/helm/pull/8)。検証に `helmfile diff` が要るため、
チャート（issue #1 の18番）とリリース定義（19番）を1つの PR にまとめた。

`charts/stock-radar/` を `stock-notificator` に倣って作った。差分は以下。

- **PVC（local-path）を追加する。** 既存チャートに前例が無いので新規に書く。
  DuckDB ファイルと `data/raw/` を置く。容量は 10GB 程度（`docs/architecture.md` の見積もり）
- **`affinity.nodeAffinity` で `pollux` に固定する**（local PVC はノードローカルのため）
- **Istio サイドカーの終了処理**を command の末尾に入れる。
  これが無いと本体が終わっても Job が完了しない
- `concurrencyPolicy: Forbid`（`stock-notificator` のテンプレートでは既定で入っている）
- `activeDeadlineSeconds` は時間予算 + 余裕、`backoffLimit` は低め
- `criteria.yaml` / `runtime.yaml` の ConfigMap は**既定で作らない**。値を渡したときだけ
  その1ファイルを `subPath` で差し替える。イメージの外に第2の正本を作らないため
- namespace は新規に **`stock`** を切る（2026-09-22 決定）。既存が `web` / `notification` /
  `monitoring` と役割を表す語なのに合わせた。日本株版や Kill 条件の監視など、後から増える
  株式まわりのワークロードを同じ ns に置ける。`createNamespace: true` で `apply` 時に作られる
- **イメージが public なので registry-secret は不要**
- **namespace にラベルを付けなくても Istio のサイドカーは入る。** クラスタの injector は
  「`istio-injection` も `istio.io/rev` も無い namespace ＋ Pod ラベル `sidecar.istio.io/inject: "true"`」
  で発火する webhook を持っている。`PeerAuthentication` は未設定（mTLS は PERMISSIVE）
- ~~**SOPS の secret は不要の見込み。**~~ → **使うことにした（2026-09-21）。**
  notificator に認証が無いのは変わらないが、`SEC_USER_AGENT` は連絡先（メールアドレス）であり、
  平文でリポジトリに残さない方針（`CLAUDE.md`）を優先した。private リポジトリでも履歴には残る

**検証**（2026-09-21 実施）：

- `helm lint` / `helm template`（設定差し替えの分岐も含む）
- `kubectl apply --dry-run=client` で Secret / PVC / CronJob の3つが通る
- `helmfile -f stock-radar.yaml diff` が SOPS の復号込みで通る（3リソースが added）

実地で分かったこと：

| | |
|---|---|
| ストレージクラス | `local-path` が既定（`WaitForFirstConsumer` / reclaim は **Delete**）。PVC を消すとデータも消えるので `helm.sh/resource-policy: keep` を付けた |
| `pollux` のディスク | **322GB 空き**。10Gi の PVC には十分（未確認だった宿題を解消） |
| イメージの公開範囲 | `ghcr.io/yushi-a/stock-radar` は **public**。匿名 pull を確認したので **registry-secret は不要** |
| `NOTIFICATOR_ADDRESS` | **スキームまで含めた URL を渡す**（`http://notificator.notification.svc.cluster.local:50051`）。Connect の JSON を素の HTTP POST で叩くため、`stock-notificator` の gRPC ダイアル先とは形が違う |
| SEC の User-Agent | SOPS の `environments/secrets/stock-radar.yaml` から渡す。**中身はプレースホルダなので apply 前に差し替える** |

**`apply` はしていない。** 実環境での確認は 6-4（🔴 G5）。

### 6-4. デプロイと実環境での動作確認 🔴 ゲート

`helmfile -f stock-radar.yaml diff` → `apply` の後、手動トリガーで確認する。

```bash
kubectl create job --from=cronjob/stock-radar stock-radar-manual-1 -n stock
```

| 確認項目 | 結果（2026-09-22 実測） |
|---|---|
| 少数銘柄での疎通 | ✅ `--limit 10` で **7分58秒** |
| **Job が Complete になる** | ✅ サイドカーも落ちた（`0/2 Completed`） |
| 全銘柄での完走 | ✅ **20分**。候補21件、`price_coverage` 100%。zip は PVC 上のものを再利用した |
| PVC の永続 | ✅ 2回目は **2分30秒**。株価の対象は **0銘柄**（5日以内なので取り直さない）、zip も再利用 |
| リソース実測 | ✅ ピーク **778Mi**（財務の取り込み）、CPU は **1コア張り付き**（単一プロセス）。requests 200m / 768Mi、limits メモリ 3Gi（実測の約4倍）に確定。**CPU の limits は置かない**（1000m で切ると取り込みと株価取得がそのまま遅くなる） |
| notificator への通知 | ✅ **🔵 G4 通過。** HTTP 200、240文字、LINE に着信 |

**⚠️ ここで `compact()` の外部キーのバグを踏んだ**（#49 / #50）。
`COPY FROM DATABASE` はテーブルを外部キーの順に並べてくれず、スクリーニングを1回でも
実行した後の DB では必ず落ちる。**週次運用では2回目以降が毎回失敗する**バグで、
手元では `fetch-facts` を先に流していたため Phase 4・5 の間ずっと踏まなかった。

**`restartPolicy` は `Never` にしてある。** `OnFailure` だと backoffLimit を超えた時点で
Pod ごと消えてログが残らず、最初の失敗は原因を追えなかった。

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
