import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from disclosures import declared_share_codes, extract_scope, other_assets_clear, parse_assets, period_end, permitted_non_stock_trades_clear, portfolio, yearly_quote
from market_data import parse_annual, parse_catalog, parse_metadata, parse_rank, parse_subscription, ranking


class ParserTests(unittest.TestCase):
    def test_catalog_full_scope_and_duplicates(self):
        rows = [["000001", "", "纯债A", "债券型-长债"], ["000002", "", "海外债", "QDII-债券"], ["000003", "", "混合", "混合型"]]
        self.assertEqual(set(parse_catalog("var r = " + json.dumps(rows))), {"000001", "000002"})
        with self.assertRaises(ValueError):
            parse_catalog("var r = " + json.dumps(rows + [rows[0]]))

    def test_catalog_changed_wrapper_fails(self):
        with self.assertRaises(ValueError):
            parse_catalog("<html>blocked</html>")

    def test_rank_layout_and_total(self):
        record = ",".join(["400030", "测试", "CS", "2026-11-06"] + ["0"] * 13)
        rows, total = parse_rank("var rankData={datas:" + json.dumps([record]) + ",allRecords:1};")
        self.assertEqual((rows[0][0], total), ("400030", 1))

    def test_pagination_duplicate_fails_closed(self):
        record = ",".join(["400030", "测试", "CS", "2026-11-06"] + ["0"] * 13)
        class FakeClient:
            def get(self, url, params=None):
                return "var rankData={datas:" + json.dumps([record]) + ",allRecords:2};"
        with self.assertRaisesRegex(ValueError, "重复"):
            ranking(FakeClient(), "zq", page_size=1)

    def test_metadata_normal_and_foreign_class(self):
        text = "基金全称 合成债券基金 基金简称 合成债券美元A 基金代码 999001 基金管理人 测试管理人 基金托管人 测试银行 成立日期/规模 2019年01月02日 / 1亿份"
        metadata = parse_metadata(text)
        self.assertEqual(metadata["currency"], "USD")
        self.assertEqual(metadata["inception_date"], "2019-01-02")

    def test_partial_inception_year_not_full_year(self):
        content = '<table><tr><td>年份</td><td>2021年度</td><td>2020年度</td></tr><tr><td>阶段涨幅</td><td>0.00%</td><td>8.00%</td></tr></table>'
        annual = parse_annual('var apidata={content:"' + content + '"};', "2020-02-01", 2025)
        self.assertEqual(annual, {"2021": "0.00", "2020": None})

    def test_unknown_annual_response_rejected(self):
        with self.assertRaises(ValueError):
            parse_annual("blocked", "2018-01-01", 2025)

    def test_subscription_quota_and_zero(self):
        result = parse_subscription("交易状态：限大额（单日累计购买上限10.00万元） 开放赎回 购买手续费", "2026-11-09")
        self.assertEqual((result["subscription_status"], result["subscription_limit_cny"]), ("limited", "100000.00"))
        result = parse_subscription("交易状态：限大额（单日累计购买上限0元） 购买手续费", "2026-11-09")
        self.assertEqual(result["subscription_status"], "closed")

    def test_unknown_trade_not_assumed_open(self):
        self.assertEqual(parse_subscription("仅开放赎回", "2026-11-09")["subscription_status"], "unverified")

    def test_period_variants(self):
        for title, expected in [("基金2020年度报告", "2020-12-31"), ("基金2026年年度报告", "2026-12-31"), ("基金二〇二六年第三季度报告", "2026-09-30"), ("基金2026年中期报告", "2026-06-30"), ("基金2026年度报告提示性公告", None)]:
            with self.subTest(title=title):
                self.assertEqual(period_end(title), expected)

    def test_report_year_not_fund_name_year_or_revision_year(self):
        self.assertEqual(period_end("合成2020定期开放基金2026年第3季度报告"), "2026-09-30")
        self.assertEqual(period_end("合成2020债券基金2025年度报告（2026年修订）"), "2025-12-31")

    def test_scope_subject_investing_is_not_fund_holding(self):
        text = "投资范围本基金的投资范围包括国债、金融债、可分离交易可转债的纯债部分、银行存款。允许基金投资其他金融工具，但须符合监管规定。投资策略"
        self.assertTrue(extract_scope(text)[1])

    def test_different_asset_negation_does_not_hide_stock_permission(self):
        text = "投资范围本基金的投资范围包括国债及股票，不投资普通可转债和可交换债，固定收益投资比例不低于80%。投资策略"
        self.assertIs(extract_scope(text)[1], False)

    def test_contract_risk_and_denial(self):
        text = "投资范围本基金主要投资国债、金融债和银行存款，不投资股票、权证、普通可转债、可交换债。投资策略"
        self.assertTrue(extract_scope(text)[1])
        self.assertIs(extract_scope(text.replace("不投资股票", "可投资股票"))[1], False)

    def test_annual_quote_matches_correct_class(self):
        text = "合成纯债A 过去一年 5.20% 0.03% 合成纯债C 过去一年 4.80% 0.03%"
        self.assertEqual(str(yearly_quote(text, "合成純债A", False)), "5.20")
        self.assertEqual(str(yearly_quote(text, "合成純债C", False)), "4.80")
        self.assertIsNone(yearly_quote(text, "合成純债D", False))

    def test_total_assets_sum_and_identity(self):
        text = "合成纯债基金 2026年9月30日 单位：人民币元 4.期末基金资产净值 70,000,000.00 40,000,000.00 5.期末基金份额净值 1.10 1.09"
        result = parse_assets(text, "合成纯债基金", "2026-09-30")
        self.assertEqual(result["fund_assets_cny"], "110000000.00")
        with self.assertRaises(ValueError):
            parse_assets(text, "其他基金", "2026-09-30")

    def test_aggregate_is_not_double_counted(self):
        text = "合成纯债基金 2026年9月30日 单位：人民币元 全基金合计 4.期末基金资产净值 110,000,000.00 70,000,000.00 5.期末基金份额净值 1.10"
        with self.assertRaises(ValueError):
            parse_assets(text, "合成纯债基金", "2026-09-30")

    def test_currency_and_all_share_sizes_must_be_confirmed(self):
        text = "合成纯债基金 2026年9月30日 基金主代码999001 下属份额交易代码999002 主要财务指标 单位：人民币元 4.期末基金资产净值 110,000,000.00 5.期末基金份额净值 1.10"
        with self.assertRaisesRegex(ValueError, "全部份额"):
            parse_assets(text, "合成纯债基金", "2026-09-30")
        with self.assertRaisesRegex(ValueError, "人民币"):
            parse_assets(text.replace("人民币元", "美元"), "合成纯债基金", "2026-09-30")

    def test_security_code_not_used_to_verify_share_identity(self):
        text = "基金主代码999001 下属基金份额交易代码999002 主要财务指标 股票代码600001"
        self.assertEqual(declared_share_codes(text), {"999001", "999002"})

    def test_standalone_class_labels(self):
        text = "A 类 过去一年5.20% 0.03% C类 过去一年4.80% 0.03%"
        self.assertEqual(str(yearly_quote(text, "合成纯债A", False)), "5.20")
        self.assertEqual(str(yearly_quote(text, "合成纯债C", False)), "4.80")

    def test_zero_current_convertible_position_does_not_prove_no_investment(self):
        scope = "投资范围包括国债、股票、可转债。"
        self.assertFalse(permitted_non_stock_trades_clear("本报告期末未持有处于转股期的可转换债券。", scope))
        self.assertFalse(permitted_non_stock_trades_clear("本报告期末未持有可转债。", scope))
        self.assertFalse(permitted_non_stock_trades_clear("本报告期未投资股票，但投资可转债。", scope))
        self.assertTrue(permitted_non_stock_trades_clear("本报告期未投资可转债。", scope))
        self.assertTrue(permitted_non_stock_trades_clear("", "投资范围包括国债、股票，不投资可转债。"))

    def test_other_assets_unknown_positive_not_clear(self):
        self.assertTrue(other_assets_clear("期末其他各项资产构成 1 应收申购款 2.00 2 应收清算款 3.00 3 其他 - 4 合计 5.00"))
        self.assertFalse(other_assets_clear("期末其他各项资产构成 1 应收申购款 2.00 2 其他 3.00 3 合计 5.00"))

    def test_top_ten_does_not_prove_full_holdings(self):
        self.assertEqual(portfolio("前十大持仓全为债券")['status'], "unverified")


if __name__ == "__main__":
    unittest.main()
