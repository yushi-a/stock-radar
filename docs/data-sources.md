# データソース調査（2026-09-20時点）

## 採用方針

| 役割 | ソース | 位置づけ |
|---|---|---|
| 日本：株価・財務 | J-Quants API（Standard 推奨） | 一次・主 |
| 米国：財務 | SEC EDGAR XBRL API | 一次・主 |
| 米国：株価・52週高安 | yfinance | 補完（失敗時 FMP） |
| 照合 | yfinance / FMP の財務値 | 一次ソースとの差分チェック |

## J-Quants API（JPX 公式）

- プラン（月額・税込）：Free ¥0 / Light ¥1,650 / Standard ¥3,300 / Premium ¥16,500
- Free は直近12週間を除く2年分のみ（12週遅延）→ 当日スクリーニングには使えない。
- API コール制限：Free 5件/分、Light 60件/分、Standard 120件/分、Premium 500件/分。
- CSV ダウンロードは有料プランのみ。
- Standard 以上で「大株主状況（EDINET）」→ オーナー系判定に使える。
- 財務諸表（BS/PL/CF 詳細）は Premium のみ。Standard は決算短信サマリー。
- `/fins/summary` の主な項目：`Sales` `OP` `NP` `TA` `Eq` `EqAR` `CFO` `CFI` `CashEq` `ShOutFY`、会社予想 `FSales` `FOP` `NxFSales` など。
  - 値はすべて文字列、欠損は空文字列。
  - 四半期レコードでは CF が空のことがある → FCF は通期レコードから取る。
  - IFRS/US-GAAP 採用企業は経常利益が空。
- `/equities/valuation`（バリュエーション指標）あり。
- 規約：取得データの再配布・分析結果の継続的な第三者提供は禁止。

出典：
- https://jpx-jquants.com/ja
- https://jpx-jquants.com/ja/spec/data-spec
- https://jpx-jquants.com/ja/spec/fin-summary

## SEC EDGAR API

- `data.sec.gov`：認証・APIキー不要。10-K/10-Q/20-F/6-K 等の XBRL を JSON で提供。
- `companyfacts.zip`：全社分を毎晩（ET 3:00頃）再生成 → 一括取得に最適。
- `frames` API：1科目×全社をまとめて取得。暦年期間に寄せるため決算期ズレに注意。
- 株価は無い。XBRL は企業ごとにタグ揺れがある（減価償却費など）→ EBITDA 系の正規化が最大の手間になる見込み。
- CORS 非対応。自動アクセスは SEC の Developer FAQ に従う（User-Agent 等）。

出典：https://www.sec.gov/search-filings/edgar-application-programming-interfaces

## yfinance

- 非公式（Yahoo と無関係）、研究・教育目的、Yahoo の API は個人利用限定と README に明記。
- `EquityQuery` / `Screener` でサーバー側の条件検索が可能。
- 429（YFRateLimitError）報告が2025〜2026年に多数。一括取得ではスロットリング・キャッシュ・翌日リトライ必須。
- 財務値の定義・更新タイミングは非公開 → 一次情報扱いしない。

出典：
- https://github.com/ranaroussi/yfinance
- https://github.com/ranaroussi/yfinance/issues/2422
- https://github.com/TauricResearch/TradingAgents/issues/437

## Financial Modeling Prep（FMP）

- Basic 無料（250件/日）、Starter $22/月（年払い、US のみ・5年・300件/分）、Premium $59/月、Ultimate $149/月（グローバル・Bulk エンドポイント）。
- Bulk エンドポイント（Key Metrics TTM Bulk、EOD Bulk 等）は Ultimate のみ。
- yfinance が不安定な場合の代替。

出典：https://site.financialmodelingprep.com/pricing-plans

## 証券会社スクリーナー（参考：手動運用する場合）

### SBI証券 国内株式（Refinitiv データ）

- 詳細条件は最大10項目、My スクリーナー6セット、CSV ダウンロード可。
- あり：時価総額、平均売買代金（5/20日）、PBR、PSR、ROE、ROA（**経常利益ベース**）、自己資本比率、流動比率、売上高営業利益率、売上高変化率（前年度/3年前/5年前/前年同四半期）、過去3年平均売上高成長率（実質2年年率）、52週高値からの下落率、52週安値からの上昇率、値下がり率（3/6ヶ月）
- なし：FCF（PCFR は純利益＋減価償却費で FCF ではない）、粗利率、上場年数、オーナー比率

出典：
- https://search.sbisec.co.jp/v2/popwin/info/trading/pop_domestic_screening_01.html
- https://search.sbisec.co.jp/v2/popwin/info/trading/pop_domestic_screening_02.html

### SBI証券 米国株スクリーナー（Ver2.4, 2024/10）

- 詳細検索は最大8項目（P.6）、My スクリーナー7セット。
- 営業利益率の水準が無い（変化率のみ、P.17–18）、売買代金が無い（P.21）→ 本用途には不向き。

出典：https://graph.sbisec.co.jp/sbisecscr/manual/SBI_Screener_Contents_Help.pdf

### Finviz

- P/FCF、粗利率、営業利益率、ROA/ROE、流動比率、D/E、52週高安からの距離、パフォーマンス、インサイダー保有比率あり。
- 売上成長は「過去5年」「前年同四半期比」。3年は無し。
- 任意数値範囲の可否（無料 vs Elite）はヘルプに記載なし。

出典：https://finviz.com/help/screener
