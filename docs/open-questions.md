# 未決事項・次のステップ

## 決定済み（2026-09-20）

詳細は `docs/architecture.md` を参照。

- 初回対象は**米国株**。SEC EDGAR + yfinance で完全無料。日本株は第一弾より後。
- 実行基盤：まずローカル CLI、安定後に K3s CronJob（クラスタ `yuxsr-dev` / ノード `pollux`、local PVC 利用）。
- ストレージ：DuckDB 単一ファイル（`data/stock_radar.duckdb`）。生の zip はファイルで保持。
- スタック：Python + uv。
- 出力：CSV + 通知。通知先は同一クラスタ内にデプロイ済みの自前アプリ `notificator`（Service 名で到達可能）。
- 会計期間：年次（10-K）主、売上のみ四半期（10-Q）も保持。
- ユニバース：金融・REIT 除外（SIC 6000番台）、ADR 除外（10-K 提出企業のみ）、売上ゼロ除外。
- トラックB の「予想売上成長率」は米国に会社予想が無いため、**直近四半期 YoY ≥ 20%** で代替。
- 「資産成長率 − EBITDA成長率」は減価償却費のタグ揺れが重いため、**EBIT（営業利益）で代用**。後から差し替え可能にする。
- 第一弾のスコープ：スクリーニング + CSV + 通知まで。Kill 条件監視とフィードバック記録は後回し。

## 未決事項

### 実装開始までに決めるもの
- [ ] `notificator` のインターフェース（エンドポイント・ペイロード形式・認証）の確認。実装時でよい
- [ ] local PVC を使う場合の DuckDB ファイルのバックアップ方法（local PVC はノードローカルで冗長性が無い）
- [ ] `pollux` ノードのディスク空き容量の確認。定常3〜5GB（`companyfacts.zip` 1世代 + DuckDB + 作業領域）を必要とする

### Phase 2a（データ構造の実地調査）で決めたもの

2026-09-20 に実測して確定。**結論と根拠は `docs/xbrl-findings.md`。**

- [x] A：年次レコードの選定ルール。`fy` は使わず `start`/`end` で束ね、350〜380日でガードし、`filed` が最新を採る
- [x] B：四半期の YTD → 3ヶ月変換は**不要だった**（92.4% が3ヶ月値を直接報告している）
- [x] C：**`companyfacts` に軸付きファクトは含まれない**（仮説は当たり）。複数クラス株では `dei` の株数タグが欠落するため、`WeightedAverageNumberOfDilutedSharesOutstanding` を第1候補にする
- [x] D：欠損率を計測。**`GrossProfit` は 28.8% が算出不能**
- [x] XBRL タグの優先順位リスト → `src/stock_radar/sources/sec/concepts.py`

- [ ] 🔴 **`track_b.gross_margin` をハードフィルタのまま残すか**（`docs/xbrl-findings.md` の提案を参照）。ユーザー判断待ち

### 実装しながら決めるもの
- [ ] 各閾値の最終確定（`config/criteria.yaml` の初版をたたき台に、Phase 4 の通過件数を見て調整）
- [ ] タイミング加点のスコアリング方法（重み付け）。**Phase 4 で並べ替えに使うため第一弾に必要**
- [ ] `config/runtime.yaml` のスロットリング初期値（Phase 3 で 429 の発生状況を見て詰める）
- [ ] 出力 CSV の `as_of` と出典列の具体化（決算期末 / 提出日 / 株価日付 / 取得日のどれを持つか）
- [ ] USD 建てで報告しない企業の扱い（除外するか）

### 第一弾より後に決めるもの
- [ ] CronJob の実行時刻（推奨：土曜朝 JST）
- [ ] J-Quants のプラン。Free のままでは3年CAGR（2年分しか遡れない）とオーナー系判定（大株主状況が Standard 以上）が実装できない
- [ ] 日本株を追加する際の米国版とのコード共通化の粒度
- [ ] 評価スキルへの CSV 受け渡し方法

## 進め方

`docs/implementation-plan.md` の Phase 0 〜 5。
