# multibagger-screener

米国株・日本株の全銘柄から、「数年で株価が数倍になりうる」候補を機械的に絞り込むスクリーナー。
絞り込んだ候補は、既存の銘柄評価スキル `growth-stock-evaluation` に渡して深掘り評価する。

## ステータス

- 設計前（2026-09-20時点）。スクリーニング条件・データソース候補・評価スキルとの役割分担まで合意済み。
- **システム構成（ストレージ、実行基盤、取得ジョブの分割など）は未決定で、別途再検討する。**

## ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けの作業指示・前提・禁止事項 |
| [docs/screening-criteria.md](docs/screening-criteria.md) | スクリーニング条件（根拠となる実証研究と閾値） |
| [docs/data-sources.md](docs/data-sources.md) | 証券会社スクリーナー／API の調査結果 |
| [docs/skill-integration.md](docs/skill-integration.md) | 評価スキルとの役割分担・連携方針 |
| [docs/open-questions.md](docs/open-questions.md) | 未決事項と次のステップ |
| [config/criteria.example.yaml](config/criteria.example.yaml) | 条件定義のたたき台 |

## 基本方針

1. 全銘柄の指標を一括取得してから、プログラムで絞り込む。
2. スクリーナーは「漏らさない」ための道具。投資判断（スコア、目標株価、期待倍率）は出さない。
3. 財務データは一次情報（J-Quants / SEC XBRL）を優先し、二次データ（yfinance / FMP）は補完・照合に使う。
