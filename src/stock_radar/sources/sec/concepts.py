"""XBRL タグの優先順位マッピング。

閾値ではないので YAML ではなく Python 定数に置く（docs/architecture.md）。
**順位の根拠は Phase 2a の実測値**で、出現率は `docs/xbrl-findings.md` に載っている。
母集団は Phase 1 の残存ユニバース 3,184社（2026-09-20 時点）。

優先順位は**会計期間ごとに**適用する。1社が複数のタグを併用しているのが普通で
（ASC 606 の移行をまたぐ企業は特に）、「この会社はこのタグ」と固定すると
古い年度が欠ける。
"""

from __future__ import annotations

__all__ = [
    "CAPEX",
    "COST_OF_REVENUE",
    "CURRENT_ASSETS",
    "CURRENT_LIABILITIES",
    "EQUITY",
    "GROSS_PROFIT",
    "NET_INCOME",
    "OPERATING_CASH_FLOW",
    "OPERATING_INCOME",
    "REVENUE",
    "SHARES_OUTSTANDING",
    "TOTAL_ASSETS",
]

# 売上。直近年度で採用された割合（合計 91.5%）：
#   RevenueFromContractWithCustomerExcludingAssessedTax  63.4%
#   Revenues                                             18.4%
#   RevenueFromContractWithCustomerIncludingAssessedTax   9.5%
#   SalesRevenueNet / SalesRevenueGoodsNet                0.1% ずつ（ASC 606 以前）
#
# Excluding を Including より先に置くのは、Including が売上税などを含むため。
# Revenues はそれらより広い概念（例：Walmart は Revenues に会員費を含み、
# RevenueFromContractWithCustomer... より約0.9%大きい）なので、
# ASC 606 のタグがある年度ではそちらを優先する。
REVENUE = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]

# 粗利。**そのまま取れるのは 54.4% しかない。**
# 原価タグから 売上 − 原価 で導出しても 71.2% 止まり（docs/xbrl-findings.md の D）。
GROSS_PROFIT = ["GrossProfit"]

# 粗利を導出するための原価。導出できた 489社の内訳は
#   CostOfGoodsAndServicesSold 313 / CostOfRevenue 174 / その他 2
COST_OF_REVENUE = [
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
]

# EBIT の代用（docs/architecture.md：EBITDA は減価償却費のタグ揺れが激しく扱わない）。
# 出現率 93.5%。設計が前提にしていた「タグが安定している」は実測でも成り立つ。
OPERATING_INCOME = ["OperatingIncomeLoss"]

# 出現率 97.9%。ROA / ROE の分子。
NET_INCOME = ["NetIncomeLoss"]

# 出現率 98.9%。FCF = 営業CF − 設備投資 の第1項。
OPERATING_CASH_FLOW = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]

# 設備投資。合計 92.9%。内訳：
#   PaymentsToAcquirePropertyPlantAndEquipment       79.3%
#   PaymentsToAcquireProductiveAssets                10.8%
#   PaymentsToAcquireOtherPropertyPlantAndEquipment   1.7%
#   PaymentsForCapitalImprovements                    1.0%
CAPEX = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    "PaymentsForCapitalImprovements",
]

# 時点値（BS）。start を持つエントリは実測で 0/2,383 件だった。
TOTAL_ASSETS = ["Assets"]  # 99.4%
CURRENT_ASSETS = ["AssetsCurrent"]  # 96.6%
CURRENT_LIABILITIES = ["LiabilitiesCurrent"]  # 96.5%

# 自己資本。少数株主持分を含まない StockholdersEquity を優先する。
EQUITY = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]

# 発行済株式数。**dei:EntityCommonStockSharesOutstanding を先頭にしない。**
#
# companyfacts は軸（dimension）付きファクトを一切含まないため、株式クラスごとに
# 開示している企業ではこのタグが**丸ごと欠落する**（Alphabet・Reddit・Funko で確認）。
# 出現率も 90.8% にとどまる。
#
# 一方 WeightedAverageNumberOf...Outstanding は軸なしの全クラス合計で報告され、
# 出現率 97.6%。Alphabet で 12,274,000,000 株（全クラス合計）が取れている。
#
# 期中平均なので期末の実数とは一致しないが、時価総額の桁を出す用途には足りる。
# 詳細は docs/xbrl-findings.md の C。
SHARES_OUTSTANDING = [
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),  # 97.4%
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),  # 97.6%
    ("dei", "EntityCommonStockSharesOutstanding"),  # 90.8%
    ("us-gaap", "CommonStockSharesOutstanding"),  # 85.4%
    ("us-gaap", "CommonStockSharesIssued"),  # 89.5%
]
