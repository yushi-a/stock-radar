# stock-radar

米国株・日本株の全銘柄から、「数年で株価が数倍になりうる」候補を機械的に絞り込むスクリーナー。
絞り込んだ候補は、既存の銘柄評価スキル `growth-stock-evaluation` に渡して深掘り評価する。

## ステータス

- 設計完了・実装前（2026-09-20時点）。
- **初回対象は米国株**（SEC EDGAR + yfinance で完全無料）。日本株は第一弾より後。
- システム構成は合意済み：Python + uv / DuckDB 単一ファイル / まずローカル CLI、安定後に pollux の K3s CronJob / 出力は CSV + 通知（notificator）。
- 次は `docs/implementation-plan.md` の Phase 0 から。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けの作業指示・前提・禁止事項 |
| [docs/architecture.md](docs/architecture.md) | システム構成・パイプライン・DuckDB スキーマ・技術選定の根拠 |
| [docs/implementation-plan.md](docs/implementation-plan.md) | フェーズ分けした実装計画と各フェーズの検証項目 |
| [docs/screening-criteria.md](docs/screening-criteria.md) | スクリーニング条件（根拠となる実証研究と閾値、米国版の読み替え） |
| [docs/data-sources.md](docs/data-sources.md) | 証券会社スクリーナー／API の調査結果 |
| [docs/skill-integration.md](docs/skill-integration.md) | 評価スキルとの役割分担・連携方針 |
| [docs/open-questions.md](docs/open-questions.md) | 未決事項と次のステップ |
| [config/criteria.example.yaml](config/criteria.example.yaml) | 条件定義のたたき台 |

## 基本方針

1. 全銘柄の指標を一括取得してから、プログラムで絞り込む。
2. スクリーナーは「漏らさない」ための道具。投資判断（スコア、目標株価、期待倍率）は出さない。
3. 財務データは一次情報（J-Quants / SEC XBRL）を優先し、二次データ（yfinance / FMP）は補完・照合に使う。
