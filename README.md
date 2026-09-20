# stock-radar

米国株・日本株の全銘柄から、「数年で株価が数倍になりうる」候補を機械的に絞り込むスクリーナー。
絞り込んだ候補は、既存の銘柄評価スキル `growth-stock-evaluation` に渡して深掘り評価する。

## ステータス

- 設計完了。**Phase 0（足場・設定・DuckDB スキーマ）まで実装済み**（2026-09-20時点）。進捗は [issue #1](https://github.com/yushi-a/stock-radar/issues/1)。
- **初回対象は米国株**（SEC EDGAR + yfinance で完全無料）。日本株は第一弾より後。
- システム構成は合意済み：Python + uv / DuckDB 単一ファイル / まずローカル CLI、安定後に K3s CronJob（クラスタ `yuxsr-dev`） / 出力は CSV + 通知（notificator）。
- 実装計画は [docs/implementation-plan.md](docs/implementation-plan.md)。

## 開発

Python は uv で管理する（`.python-version` のバージョンが自動で用意される）。

```bash
uv sync                      # 依存の取得と仮想環境の作成
uv run ruff check .          # lint
uv run ruff format .         # 整形
uv run pytest                # テスト
```

テストはソケットを塞いだ状態で走る（`pytest-socket`）。実 API への疎通確認は自動テストにせず、
手動スモークテストに留める。方針は [docs/testing.md](docs/testing.md) を参照。

環境変数は `.env.example` をコピーして `.env` を作り、`set -a; source .env; set +a` で読み込む。
（CLI からの自動読み込みは Phase 5 で入れる。K8s では ConfigMap と env で渡す。）

## ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けの作業指示・前提・禁止事項 |
| [docs/architecture.md](docs/architecture.md) | システム構成・パイプライン・DuckDB スキーマ・技術選定の根拠 |
| [docs/implementation-plan.md](docs/implementation-plan.md) | フェーズ分けした実装計画と各フェーズの検証項目 |
| [docs/testing.md](docs/testing.md) | テスト方針。何を自動テストにし、何を手動確認に留めるか |
| [docs/screening-criteria.md](docs/screening-criteria.md) | スクリーニング条件（根拠となる実証研究と閾値、米国版の読み替え） |
| [docs/data-sources.md](docs/data-sources.md) | 証券会社スクリーナー／API の調査結果 |
| [docs/skill-integration.md](docs/skill-integration.md) | 評価スキルとの役割分担・連携方針 |
| [docs/open-questions.md](docs/open-questions.md) | 未決事項と次のステップ |
| [config/criteria.yaml](config/criteria.yaml) | スクリーニング条件（閾値と根拠） |
| [config/runtime.yaml](config/runtime.yaml) | 運用パラメータ（スロットリング・時間予算・出力先） |

## 基本方針

1. 全銘柄の指標を一括取得してから、プログラムで絞り込む。
2. スクリーナーは「漏らさない」ための道具。投資判断（スコア、目標株価、期待倍率）は出さない。
3. 財務データは一次情報（J-Quants / SEC XBRL）を優先し、二次データ（yfinance / FMP）は補完・照合に使う。
