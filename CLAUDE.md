# CLAUDE.md

このリポジトリで作業する Claude Code 向けの指示。

## プロジェクトの目的

全上場銘柄（米国・日本）から、数年で数倍になりうる候補を週次で抽出し、CSV 等で出力する。
出力は既存の評価スキル `growth-stock-evaluation` の入力になる。詳細は `docs/` を参照。

## 現在地

**第一弾（米国株）完了。2026-09-22 にクラスタで稼働を開始した。**
経緯と決定ログは [issue #1](https://github.com/yushi-a/stock-radar/issues/1)（クローズ済み）。

- `uv run stock-radar run` でユニバース → 財務 → 株価 → スクリーニング → CSV → 通知まで一息で回る
- クラスタでは毎週土曜08:00 JST に `stock` namespace の CronJob が回る。
  マニフェストは `yushi-a/helm` の `charts/stock-radar`
- 実測：全銘柄20分、2回目以降の差分は2分30秒、メモリのピーク 778Mi、候補は21件前後

**次に手を付けるときは、まず開いている issue を見る。** 判明している宿題：

- [#48](https://github.com/yushi-a/stock-radar/issues/48) CSV の取り出し方。
  いまは PVC に置くだけで、参照するのに `sudo` が要る
- local PVC のバックアップ方法（`docs/open-questions.md`）
- 評価スキルとの前提のずれ（[#54](https://github.com/yushi-a/stock-radar/issues/54) モート・カタリストの条件が無い、
  [#55](https://github.com/yushi-a/stock-radar/issues/55) トラックAが成長性軸で低く出る）。
  どちらも `docs/open-questions.md` 行きで、判断の前に候補20件前後の簡易評価の結果が要る
- 日本株対応、Kill 条件監視、フィードバック記録（いずれも第一弾より後と合意済み）

## 作業前に必ず読むもの

- `docs/architecture.md` — システム構成。パイプラインの処理順、DuckDB スキーマ、技術選定の根拠
- `docs/implementation-plan.md` — フェーズ分けした実装計画と各フェーズの検証項目
- `docs/testing.md` — テスト方針。何を自動テストにし、何を手動確認に留めるか
- `docs/screening-criteria.md` — 条件と閾値、およびその根拠（米国版の読み替えを含む）
- `docs/skill-integration.md` — スクリーナーが「やらないこと」
- `docs/open-questions.md` — 未決事項。ここにある項目は独断で決めず、ユーザーに確認する

## 進め方

- 1 PR = 1タスク（目安300〜500行）。**テストが緑なら Claude が自動マージして次へ進む**（ユーザー承認済み）
- **マージは squash ではなくマージコミットを作る**（`gh pr merge --merge`）。PR 内のコミット粒度を main に残す
- ゲート G1〜G5（issue #1 参照）に当たったら止まってユーザーに確認する
- タスク issue はフェーズに入る直前に作る。マージのたびに issue #1 のチェックと決定ログを更新する

## 守ること

- **システム構成は合意済み。変更するときはユーザーに確認する。** 決定内容と、その決定に至った理由は `docs/architecture.md` にある。要点：
  - 初回対象は米国株（SEC EDGAR + yfinance）。日本株は第一弾より後。
  - Python + uv / DuckDB 単一ファイル（`data/stock_radar.duckdb`）。
  - まずローカル CLI で育て、安定後に K3s CronJob（クラスタ `yuxsr-dev` / ノード `pollux`）。
  - 出力は CSV + 通知（同一クラスタ内の自前アプリ `notificator`）。
- **パイプラインの処理順を崩さない。** 株価取得（yfinance）は必ず財務による足切りの後に実行する。先に対象を半減させることが 429 対策の中核になっている。
- **第一弾のスコープを広げない。** スクリーニング + CSV + 通知 + デプロイ（Phase 6）まで。Kill 条件監視・フィードバック記録・日本株対応は後回しと合意済み。
- **マニフェストは別リポジトリ。** K8s 関連は `yushi-a/helm`（Helmfile + Helm + SOPS）で管理する。既存の `stock-notificator` チャートが最も近い前例。
- **通知先 `notificator` は Connect プロトコルの JSON を素の HTTP POST で叩く**（`notificator.notification.svc.cluster.local:50051`）。`grpcio` や proto のコード生成は不要。バックエンドが LINE なので送れるのは**単一の短い文字列**。候補一覧は `notify.max_listed`（20件）まで載せ、あふれた分は「ほかN件は省略」と書いてから落とす。一覧の見出しに「上位」は使わない（並びはタイミング加点順であって評価の順位ではない）。
- **Istio サイドカーの終了処理を忘れない。** CronJob の command 末尾で `curl -X POST http://localhost:15020/quitquitquit` を呼ばないと Job が完了しない。
- **一次情報優先。** 財務値は日本 = J-Quants、米国 = SEC EDGAR XBRL を正とする。yfinance / FMP の値は株価補完と照合用。
- **スクリーナーで投資判断をしない。** 期待倍率・スコア・目標株価を算出しない。割安度は PBR / FCF 利回り / PSR 等の粗い指標まで。
- **閾値はコードに直書きしない。** `config/` の YAML で管理し、根拠（研究・見解）をコメントで残す。
- **数値の出典を記録する。** 出力 CSV には各指標のデータソースと基準日（決算期・取得日）を含める。
- **API 利用規約を守る。**
  - J-Quants: 取得データそのものの再配布、分析結果の継続的な第三者提供は禁止。データをリポジトリにコミットしない。
  - SEC: User-Agent に連絡先を入れる等、SEC の Developer FAQ に従う。一括取得は `companyfacts.zip` を使い、企業ごとの API を連打しない。
  - yfinance: 非公式・個人利用前提。レート制限（429）を前提にスロットリング・キャッシュ・リトライを入れる。
- **シークレットも連絡先もコミットしない。** `.env` を使う。
  - **メールアドレスを絶対にファイルへ直書きしない。** SEC の User-Agent に入れる連絡先も含む。
    秘密ではないが、公開リポジトリに残ると履歴から消せない。値は環境変数
    `SEC_USER_AGENT` で外から渡す（`config.require_env()`）。
    `config/*.yaml` が持つのは変数名（`sec.user_agent_env`）だけで、値は持たない。
  - `tests/test_no_committed_contacts.py` が追跡ファイルを走査して CI で落とす。
    プレースホルダは `example.com` 等（RFC 2606）のみ許可。

## 言語

- ドキュメント・コメントは日本語。コード上の識別子は英語。
