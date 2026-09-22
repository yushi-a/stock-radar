# stock-radar

米国株・日本株の全銘柄から、「数年で株価が数倍になりうる」候補を機械的に絞り込むスクリーナー。
絞り込んだ候補は、既存の銘柄評価スキル `growth-stock-evaluation` に渡して深掘り評価する。

## ステータス

- **第一弾（米国株）完了**。2026-09-22 からクラスタで週次稼働している。経緯は [issue #1](https://github.com/yushi-a/stock-radar/issues/1)。
- 毎週土曜08:00 JST に CronJob が回り、CSV を出して要約を LINE に通知する。全銘柄で約20分。
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
CLI 側では読み込まない（K8s では ConfigMap と env で渡すため、二重の経路を作らない）。

### 実行

```bash
uv run stock-radar fetch-universe      # SEC からユニバースを取得して universe を作る
uv run stock-radar fetch-facts         # companyfacts を取り込んで縦持ちテーブルを作る
uv run stock-radar fetch-prices        # yfinance で日次株価を差分取得する
```

株価取得は**対象銘柄数を外から絞れる**。全銘柄の実行は時間がかかるので、
デプロイ後に環境上で行う。

```bash
uv run stock-radar fetch-prices --limit 10           # 先頭10銘柄だけ
uv run stock-radar fetch-prices --tickers AAPL,MSFT  # 銘柄を直接指定
```

初回は `submissions.zip`（1.5GB）と `companyfacts.zip`（1.4GB）を落とすので数分かかる。
2回目以降は `sec.submissions_max_age_days`（既定7日）/ `sec.companyfacts_max_age_days`
（既定6日）より新しければ手元のものを使う。

### コンテナ

`run` が全工程を順に回す。CronJob が叩くのもこれ。

```bash
docker build -t stock-radar:dev .
docker run --rm \
  -v "$PWD/data:/app/data" -v "$PWD/output:/app/output" \
  -e SEC_USER_AGENT -e NOTIFICATOR_ADDRESS \
  stock-radar:dev run --limit 10
```

`data/` と `output/` は相対パスで参照しているので、`/app` 配下にマウントする
（クラスタでは PVC）。非 root（uid 10001）で動くが、`--user` で任意の UID を
与えても動く。K8s 側は `fsGroup` で PVC の所有者を合わせる。

main に入ったものは `ghcr.io/yushi-a/stock-radar` に push される。
タグは `sha-<コミットの SHA>` と `latest`。クラスタから参照するのは前者。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けの作業指示・前提・禁止事項 |
| [docs/architecture.md](docs/architecture.md) | システム構成・パイプライン・DuckDB スキーマ・技術選定の根拠 |
| [docs/implementation-plan.md](docs/implementation-plan.md) | フェーズ分けした実装計画と各フェーズの検証項目 |
| [docs/testing.md](docs/testing.md) | テスト方針。何を自動テストにし、何を手動確認に留めるか |
| [docs/xbrl-findings.md](docs/xbrl-findings.md) | `companyfacts` 実地調査の結果（Phase 2a）。正規化ルールの根拠 |
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
