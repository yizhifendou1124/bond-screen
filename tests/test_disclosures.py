import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import disclosures


class DisclosureTests(unittest.TestCase):
    def review(self, codes, expected="5.00", quote=Decimal("5.00"), broken_year=None, scope=None, report=None):
        entries = [{"ID": "AN20261027", "TITLE": "合成纯债基金2026年第3季度报告", "PUBLISHDATEDesc": "2026-10-27"}, {"ID": "AN20260729", "TITLE": "合成纯债基金招募说明书", "PUBLISHDATEDesc": "2026-07-29"}]
        entries.extend({"ID": "AN" + str(year), "TITLE": f"合成纯债基金{year}年度报告", "PUBLISHDATEDesc": f"{year+1}-03-31"} for year in range(2020, 2026))
        funds = [{"code": code, "name": "合成纯债" + ("A" if code == "999001" else "C"), "full_name": "合成纯债基金", "inception_date": "2018-01-01", "annual_returns": {str(year): expected for year in range(2020, 2026)}} for code in codes]

        def document(client, entry, code, cache, refresh=False):
            if entry["ID"] == "AN" + str(broken_year):
                raise RuntimeError("数据请求失败: https://example.test/report")
            return "合成纯债基金 基金主代码999001 下属份额交易代码999002 主要财务指标 " + (report or ""), {"source": "https://example.test/report/" + entry["ID"], "pdf_source": "https://example.test/report.pdf", "report_date": disclosures.period_end(entry["TITLE"]), "publication_date": entry["PUBLISHDATEDesc"]}

        with patch.object(disclosures, "announcements", return_value=entries), patch.object(disclosures, "document", side_effect=document), patch.object(disclosures, "portfolio", return_value={"status": "clear"}), patch.object(disclosures, "parse_assets", return_value={"fund_assets_cny": "110000000.00"}), patch.object(disclosures, "extract_scope", return_value=scope or ("仅投资国债", True)), patch.object(disclosures, "yearly_quote", return_value=quote), patch.object(disclosures, "event_findings", return_value=[]):
            return disclosures.review_family(None, funds, None, "2026-11-09", "2026-11-06")

    def test_unresolved_other_share_cannot_hide_family_anomaly(self):
        partial = self.review(["999001"])
        self.assertEqual(partial["unverified_share_codes"], ["999002"])
        self.assertEqual(partial["annual_anomaly_review"]["status"], "unverified")
        complete = self.review(["999001", "999002"])
        self.assertEqual(complete["unverified_share_codes"], [])
        self.assertEqual(complete["annual_anomaly_review"]["status"], "clear")

    def test_negative_official_quote_not_treated_as_real_zero(self):
        conflict = self.review(["999001", "999002"], expected="0.00", quote=Decimal("-0.01"))
        self.assertEqual(conflict["annual_anomaly_review"]["status"], "unverified")
        self.assertFalse(conflict["annual_quote_checks"][0]["checks"][0]["matched"])
        real_zero = self.review(["999001", "999002"], expected="0.00", quote=Decimal("-0.00"))
        self.assertEqual(real_zero["annual_anomaly_review"]["status"], "clear")

    def test_positive_zero_boundary_is_not_waived_by_quote_tolerance(self):
        for expected, quote in [("0.01", "0.00"), ("0.00", "0.01"), ("0.01", "-0.00")]:
            with self.subTest(expected=expected, quote=quote):
                conflict = self.review(["999001", "999002"], expected=expected, quote=Decimal(quote))
                check = conflict["annual_quote_checks"][0]["checks"][0]
                self.assertFalse(check["matched"])
                self.assertTrue(check["conflict_disclosed"])
                self.assertEqual(conflict["annual_return_conflicts"]["999001"]["2020"], [expected, quote])

    def test_real_quote_conflict_is_disclosed_separately_from_anomaly_review(self):
        conflict = self.review(["999001", "999002"], expected="6.79", quote=Decimal("6.82"))
        self.assertEqual(conflict["annual_anomaly_review"]["status"], "clear")
        self.assertEqual(conflict["annual_anomaly_review"]["findings"], [])
        self.assertTrue(any(note["kind"] == "annual_quote_conflict" for note in conflict["disclosure_notes"]))
        self.assertEqual(conflict["annual_return_conflicts"]["999001"]["2024"], ["6.79", "6.82"])
        self.assertFalse(conflict["annual_quote_checks"][0]["checks"][0]["matched"])
        within_tolerance = self.review(["999001", "999002"], expected="6.79", quote=Decimal("6.81"))
        self.assertEqual(within_tolerance["annual_anomaly_review"]["status"], "clear")

    def test_actual_non_pure_period_trades_not_absence_of_evidence(self):
        self.assertTrue(disclosures.non_pure_period_trades("本报告期，本基金买入并卖出可交换债券。"))
        self.assertEqual(disclosures.non_pure_period_trades("本报告期未投资可交换债券。"), [])
        self.assertEqual(disclosures.non_pure_period_trades("如果本报告期投资可交换债券，可能影响净值。"), [])
        self.assertEqual(disclosures.non_pure_period_trades("本报告期投资可分离交易可转债的纯债部分。"), [])
        self.assertEqual(disclosures.non_pure_period_trades("7.4报告期内股票投资组合的重大变动7.4.1累计买入金额前20名的股票明细本基金本报告期未进行股票买入。"), [])
        self.assertEqual(disclosures.non_pure_period_trades("本期2026年1月1日至6月30日交易性金融资产——股票投资-——权证投资-"), [])
        self.assertEqual(disclosures.non_pure_period_trades("券商权证交易成交金额占当期权证交易总额----本期无租用交易单元变更。"), [])
        self.assertTrue(disclosures.non_pure_period_trades("未发生净值差错同时本基金本报告期买入可交换债券。"))

    def test_only_missing_full_period_denial_can_be_retained_with_notes(self):
        scope = ("投资范围包括国债、普通可转债、可交换债券。", False)
        report = "7.4.3 买入股票的成本总额及卖出股票的收入总额 注：无。7.5 期末按债券品种分类 7.6 其他资产构成 本基金本报告期末无其他资产。"
        with patch.object(disclosures, "other_assets_clear", return_value=True):
            reviewed = self.review(["999001", "999002"], scope=scope, report=report)
            self.assertTrue(reviewed["pure_bond"])
            self.assertFalse(reviewed["pure_bond_review"]["component_checks"]["permitted_non_stock_transactions"])
            self.assertTrue(any(note["kind"] == "period_non_pure_absence_not_proven" for note in reviewed["disclosure_notes"]))
            traded = self.review(["999001", "999002"], scope=scope, report=report + "本报告期买入可交换债券。")
            self.assertFalse(traded["pure_bond"])
            incomplete = self.review(["999001", "999002"], scope=scope, report="本报告期末未持有股票。")
            self.assertIsNone(incomplete["pure_bond"])

    def test_spaced_share_headers_do_not_reuse_old_financial_table_class(self):
        text = "合成纯债A 合成纯债C 主要财务指标。合成纯债债券 A 阶段 过去一年 1.90% 合成纯债债券 C 阶段 过去一年 1.79%"
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债A", False), Decimal("1.90"))
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债C", False), Decimal("1.79"))

    def test_share_letter_line_breaks_and_full_width_percent(self):
        text = "合成纯债债券\nA\n类 阶段 过去一年 −0.01％ 合成纯债债券 C 类 阶段 过去一年 0.00％"
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债A", False), Decimal("-0.01"))
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债C", False), Decimal("0.00"))

    def test_identical_repeated_tables_are_not_ambiguous(self):
        table = "合成纯债 A 阶段 过去一年 1.90% 合成纯债 C 阶段 过去一年 1.79% "
        self.assertEqual(disclosures.yearly_quote(table * 2, "合成纯债A", False), Decimal("1.90"))
        self.assertIsNone(disclosures.yearly_quote(table + table.replace("1.90", "1.80"), "合成纯债A", False))

    def test_financial_metrics_fallback_requires_current_year_and_class_order(self):
        text = "主要财务指标 2025年 2024年 合成纯债 A 合成纯债 C 合成纯债 A 合成纯债 C 本期基金份额净值增长率 1.90% 1.79% 6.82% 6.73% 期末数据"
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债A", False, year=2025), Decimal("1.90"))
        self.assertEqual(disclosures.yearly_quote(text, "合成纯债C", False, year=2025), Decimal("1.79"))
        self.assertIsNone(disclosures.yearly_quote(text, "合成纯债D", False, year=2025))
        self.assertIsNone(disclosures.yearly_quote(text, "合成纯债A", False, year=2024))

    def test_unknown_class_cannot_use_single_unlabelled_return(self):
        self.assertIsNone(disclosures.yearly_quote("阶段 过去一年 1.90%", "合成纯债A", False))
        self.assertEqual(disclosures.yearly_quote("阶段 过去一年 1.90%", "合成纯债", True), Decimal("1.90"))

    def test_scope_prefers_investment_chapter_not_front_matter_or_contents(self):
        summary = "本基金的投资范围主要为具有流动性的国债、金融债和银行存款，不投资于股票、权证及普通可转债。"
        contents = "目录第一部分释义基金份额持有人，其他基金的业绩不作保证。"
        chapter = "二、投资范围本基金主要投资具有流动性的国债、金融债和银行存款，不投资于股票、权证、普通可转债及可交换债。三、投资策略"
        scope, pure = disclosures.extract_scope(summary + contents + chapter)
        self.assertTrue(pure)
        self.assertNotIn("目录", scope)
        self.assertNotIn("基金份额持有人", scope)
        scope, pure = disclosures.extract_scope(summary + contents + chapter.replace("不投资于股票", "可以投资股票"))
        self.assertIs(pure, False)

    def test_other_assets_alternate_heading_and_following_tables(self):
        text = "其他资产构成 1 应收申购款 2.00 2 应收清算款 3.00 3 合计 5.00 5.11.4 其他明细 1 未知证券 200.00 2 合计 200.00"
        self.assertTrue(disclosures.other_assets_clear(text))
        self.assertFalse(disclosures.other_assets_clear(text.replace("3.00", "4.00", 1)))
        self.assertFalse(disclosures.other_assets_clear("其他资产构成 1 应收申购款 2.00 2 其他 3.00 3 合计 5.00"))

    def test_full_period_no_stock_holdings_in_transaction_section(self):
        heading = "买入股票的成本总额及卖出股票的收入总额 "
        self.assertTrue(disclosures.stock_trades_clear(heading + "本基金本报告期未持有股票。"))
        self.assertTrue(disclosures.stock_trades_clear(heading + "本报告期无股票投资。"))
        self.assertFalse(disclosures.stock_trades_clear(heading + "本基金本报告期末未持有股票。"))
        self.assertFalse(disclosures.stock_trades_clear("前十大持仓 本基金本报告期末未持有股票。"))

    def test_explicit_empty_total_stock_transactions(self):
        heading = "7.4.3 买入股票的成本总额及卖出股票的收入总额 "
        self.assertTrue(disclosures.stock_trades_clear(heading + "注：无。\n7.5 期末按债券品种分类"))
        self.assertTrue(disclosures.stock_trades_clear(heading + "注: 无"))
        self.assertFalse(disclosures.stock_trades_clear(heading + "注：无异常。"))
        self.assertFalse(disclosures.stock_trades_clear(heading + "注：无重大交易。"))
        self.assertFalse(disclosures.stock_trades_clear(heading + "注：无。买入股票成本总额100.00"))
        self.assertFalse(disclosures.stock_trades_clear("7.4.1 累计买入前20名 注：无。7.4.2 累计卖出前20名 注：无。" + heading + "买入股票成本总额100.00"))

    def test_treasury_futures_are_separate_from_forbidden_non_stock_trades(self):
        scope = "投资范围包括国债、国债期货。"
        self.assertTrue(disclosures.permitted_non_stock_trades_clear("本基金本报告期无国债期货投资。", scope))
        self.assertTrue(disclosures.permitted_non_stock_trades_clear("本期未进行国债期货投资。", scope))
        self.assertTrue(disclosures.permitted_non_stock_trades_clear("本报告期末未持有国债期货。", scope))
        self.assertEqual(disclosures.treasury_futures_review("本报告期末未持有国债期货。", scope)["status"], "unverified")
        self.assertTrue(disclosures.permitted_non_stock_trades_clear("本报告期无国债期货投资政策变化，但期内投资国债期货。", scope))
        self.assertEqual(disclosures.treasury_futures_review("本报告期无国债期货投资政策变化，但期内投资国债期货。", scope)["status"], "unverified")

    def futures_report(self):
        return "5.9 报告期末本基金投资的国债期货交易情况说明 5.9.1 本期国债期货投资政策 投资以套期保值为主要目的。5.9.2 报告期末本基金投资的国债期货持仓和损益明细 代码 名称 持仓量（买/卖） 合约市值(元) 公允价值变动（元） 风险指标说明 T2612 10 年期国债期货 2612 合约 -800 -875,280,000.00 909,150.00 本基金投资国债期货以套期保值为主要目的。TL2612 30 年期国债期货 2612 合约 1,000 1,168,600,000.00 2,446,555.56 本基金投资国债期货以套期保值为主要目的。公允价值变动总额合计（元） 3,355,705.56 5.9.3 本期国债期货投资评价 报告期内，本基金运用国债期货工具调节组合的期限结构以及久期水平。5.10 投资组合报告附注"

    def test_treasury_contract_not_equated_with_equity_derivatives(self):
        base = "一、投资范围本基金投资国债、金融债、企业债、公司债、同业存单、银行存款、国债期货，不投资股票、权证。二、投资策略"
        self.assertTrue(disclosures.extract_scope(base)[1])
        self.assertFalse(disclosures.extract_scope(base.replace("国债期货", "股指期货"))[1])
        self.assertFalse(disclosures.extract_scope(base.replace("国债期货", "期货"))[1])
        self.assertFalse(disclosures.permitted_non_stock_trades_clear("本期投资国债期货。", "投资范围包括国债期货、股指期货。"))

    def test_treasury_purpose_positions_and_long_short_notional_are_audited(self):
        review = disclosures.treasury_futures_review(self.futures_report(), "投资范围包括国债、国债期货。")
        self.assertEqual(review["status"], "clear")
        self.assertTrue(all(review["checks"].values()))
        self.assertEqual([row["code"] for row in review["positions"]], ["T2612", "TL2612"])
        self.assertEqual(review["gross_contract_notional_cny"], "2043880000.00")
        self.assertEqual(review["net_contract_notional_cny"], "293320000.00")

    def test_treasury_unknown_purpose_positions_or_permission_remain_pending(self):
        for report, scope in [(self.futures_report(), "投资范围包括国债。"), (self.futures_report(), "本基金不投资国债期货。"), (self.futures_report().replace("以套期保值为主要目的", "以投机为主要目的"), "国债期货"), (self.futures_report().replace("-800 -875,280,000.00 909,150.00", "缺失持仓"), "国债期货")]:
            self.assertEqual(disclosures.treasury_futures_review(report, scope)["status"], "unverified")

    def test_treasury_hedge_negation_and_conditional_risk_are_not_proof(self):
        report = self.futures_report().replace("以套期保值为主要目的", "不以套期保值为主要目的")
        self.assertEqual(disclosures.treasury_futures_review(report, "国债期货")["status"], "unverified")
        report = self.futures_report().replace("报告期内，本基金运用", "如果本基金运用")
        self.assertEqual(disclosures.treasury_futures_review(report, "国债期货")["status"], "unverified")

    def test_equity_future_cannot_hide_in_treasury_position_table(self):
        report = self.futures_report().replace("TL2612 30 年期国债期货", "IF2612 股指期货")
        self.assertEqual(disclosures.treasury_futures_review(report, "国债期货")["status"], "non_pure")

    def test_report_contents_do_not_replace_actual_futures_section(self):
        text = "目录 5.9 报告期末本基金投资的国债期货交易情况说明 5.10 投资组合报告附注 " + self.futures_report()
        self.assertEqual(disclosures.treasury_futures_review(text, "国债期货")["status"], "clear")

    def test_treasury_absence_cannot_override_actual_positions_or_hypotheticals(self):
        text = self.futures_report().replace("5.9.1", "本报告期未投资国债期货。5.9.1")
        self.assertEqual(disclosures.treasury_futures_review(text, "国债期货")["status"], "unverified")
        self.assertEqual(disclosures.treasury_futures_review("如果本报告期无国债期货投资，则收益可能下降。", "国债期货")["status"], "unverified")

    def test_unused_report_template_does_not_invent_treasury_activity(self):
        report = "5.9 报告期末本基金投资的国债期货交易情况说明 5.9.1 本期国债期货投资政策 本基金本报告期末未持有国债期货。5.9.2 报告期末本基金投资的国债期货持仓和损益明细 本基金本报告期末未持有国债期货。5.9.3 本期国债期货投资评价 本基金本报告期末未持有国债期货。5.10 投资组合报告附注"
        self.assertEqual(disclosures.treasury_futures_review(report, "投资范围为国债、银行存款。")["status"], "not_applicable")
        self.assertEqual(disclosures.treasury_futures_review(report, "投资范围为国债、国债期货。")["status"], "not_applicable")
        review = disclosures.treasury_futures_review(report.replace("未持有国债期货", "无国债期货投资"), "国债期货")
        self.assertEqual(review["status"], "not_applicable")
        self.assertNotIn("no_period_investment", review["checks"])
        unused = report.replace("未持有国债期货", "未投资国债期货")
        self.assertEqual(disclosures.treasury_futures_review(unused, "国债期货")["status"], "not_applicable")
        activity = report.replace("5.9.3", "国债期货投资本期收益（元）100.00 5.9.3")
        self.assertEqual(disclosures.treasury_futures_review(activity, "国债期货")["status"], "unverified")
        empty = "5.9 报告期末本基金投资的国债期货交易情况说明 5.9.1 本期国债期货投资政策 无。5.9.2 报告期末本基金投资的国债期货持仓和损益明细 无。5.9.3 本期国债期货投资评价 无。5.10 投资组合报告附注"
        self.assertEqual(disclosures.treasury_futures_review(empty, "国债、存款")["status"], "not_applicable")

    def test_share_holder_payment_is_not_fund_investment_permission(self):
        text = "一、投资范围本基金投资国债、金融债、企业债、银行存款等固定收益资产，不投资股票、权证、普通可转债、可交换债券。保持现金以备支付基金份额持有人的赎回款项。二、投资策略"
        self.assertTrue(disclosures.extract_scope(text)[1])
        self.assertFalse(disclosures.extract_scope(text.replace("支付基金份额持有人的赎回款项", "投资其他基金份额"))[1])

    def test_historical_name_requires_official_original_contract_and_matching_codes(self):
        report = "旧名纯债基金 基金主代码 999001 下属份额交易代码 999002 主要财务指标"
        prospectus = "新名纯债基金 《旧名纯债基金基金合同》已于2017年3月2日正式生效。"
        verified = {"999001", "999002"}
        self.assertTrue(disclosures.historical_identity(report, "新名纯债基金", prospectus, verified)["verified"])
        self.assertFalse(disclosures.historical_identity(report, "新名纯债基金", "管理人还管理旧名纯债基金", verified)["verified"])
        self.assertFalse(disclosures.historical_identity(report.replace("999001", "888001"), "新名纯债基金", prospectus, verified)["verified"])

    def test_share_codes_are_bound_to_code_fields_not_postal_addresses_or_balances(self):
        text = "基金主代码 004104 基金合同生效日 2017年3月2日 下属分级基金的交易代码 004104 004105 报告期末份额 100020 管理人地址 邮编200120"
        self.assertEqual(disclosures.declared_share_codes(text), {"004104", "004105"})
        self.assertEqual(disclosures.declared_share_codes("基金主代码 0 0 4 1 0 4 基金运作方式开放式"), {"004104"})

    def test_future_benchmark_change_and_pure_contract_are_checked_not_ignored(self):
        entries = [{"ID": "NOTICE", "TITLE": "调整业绩比较基准并修订基金合同的公告", "PUBLISHDATEDesc": "2026-10-31"}, {"ID": "CONTRACT", "TITLE": "合成纯债基金基金合同更新", "PUBLISHDATEDesc": "2026-10-31"}]
        texts = {"NOTICE": "合成纯债基金 修订后的基金合同自2026年11月30日起生效。", "CONTRACT": "合成纯债基金 一、投资范围本基金投资国债、金融债、企业债、银行存款等固定收益资产，不投资股票、权证、普通可转债、可交换债券。二、投资策略"}

        def document(client, entry, code, cache, refresh=False):
            return texts[entry["ID"]], {"source": "https://example.test/" + entry["ID"]}

        with patch.object(disclosures, "document", side_effect=document):
            reviews = disclosures.review_later_announcements(None, entries, "999001", None, "合成纯债基金", "2026-11-09", True)
            self.assertTrue(all(review["status"] != "unverified" for review in reviews))
            self.assertTrue(all(review["effective_date"] == "2026-11-30" for review in reviews))
            reviews = disclosures.review_later_announcements(None, entries, "999001", None, "合成纯债基金", "2026-11-30", True)
            self.assertTrue(all(review["status"] == "unverified" for review in reviews))
            reviews = disclosures.review_later_announcements(None, entries, "999001", None, "其他基金", "2026-11-09", True)
            self.assertTrue(all(review["status"] == "unverified" for review in reviews))
            texts["CONTRACT"] = texts["CONTRACT"].replace("不投资股票", "投资股票")
            reviews = disclosures.review_later_announcements(None, entries, "999001", None, "合成纯债基金", "2026-11-09", True)
            self.assertEqual(next(review for review in reviews if review["announcement_id"] == "CONTRACT")["status"], "unverified")

    def test_generic_derivative_amount_not_automatically_equity_exposure(self):
        text = "报告期末基金资产组合 1 权益投资 - 其中股票 - 2 基金投资 - 3 贵金属投资 - 4 金融衍生品投资 2.00 按债券品种分类的债券投资组合 可转债（可交换债） -"
        self.assertEqual(disclosures.portfolio(text)["status"], "unverified")
        self.assertIn("derivatives_instrument_and_amount_unconfirmed", disclosures.portfolio(text)["missing"])
        self.assertEqual(disclosures.portfolio(text.replace("权益投资 -", "权益投资 1.00"))["status"], "non_pure")

    def test_family_treasury_trading_requires_separate_verified_proof(self):
        scope = ("投资范围包括国债、国债期货。", True)
        clear = self.review(["999001", "999002"], scope=scope, report=self.futures_report())
        self.assertTrue(clear["pure_bond"])
        self.assertEqual(clear["treasury_futures_review"]["status"], "clear")
        pending = self.review(["999001", "999002"], scope=scope, report="本报告期末未持有国债期货。")
        self.assertIsNone(pending["pure_bond"])
        self.assertTrue(any(issue["kind"] == "treasury_futures_unverified" for issue in pending["verification_issues"]))

    def test_conditional_redemption_risk_is_not_an_actual_event(self):
        text = "本基金本报告期存在单一投资者持有份额超过20%的情况。当该持有人大量赎回本基金时，可能导致的风险包括巨额赎回风险、流动性风险、基金份额净值大幅波动风险。"
        self.assertEqual(disclosures.event_findings(text, 2020, "https://example.test/report"), [])
        text = "本期未发生净值异常。若发生巨额赎回，可能导致基金净值大幅上涨。"
        self.assertEqual(disclosures.event_findings(text, 2020, "https://example.test/report"), [])

    def test_actual_event_is_not_hidden_by_unrelated_negation(self):
        text = "本期未发生净值差错。本报告期发生巨额赎回，赎回费计入资产导致基金净值增长率上涨。"
        findings = disclosures.event_findings(text, 2023, "https://example.test/report")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["year"], 2023)
        text = "本期未发生净值差错，本报告期发生巨额赎回，导致基金净值增长率上涨，未来可能还有流动性风险。"
        self.assertEqual(len(disclosures.event_findings(text, 2023, "https://example.test/report")), 1)

    def test_missing_source_and_unparsed_quote_have_different_diagnostics(self):
        missing = self.review(["999001", "999002"], broken_year=2022)
        self.assertEqual(missing["annual_anomaly_review"]["status"], "unverified")
        self.assertEqual(missing["annual_anomaly_review"]["years"], [2020, 2021, 2023, 2024, 2025])
        self.assertTrue(any(issue["kind"] == "source_unavailable" and issue["year"] == 2022 for issue in missing["verification_issues"]))
        unparsed = self.review(["999001", "999002"], quote=None)
        self.assertTrue(any(issue["kind"] == "annual_quote_unparsed" for issue in unparsed["verification_issues"]))
        self.assertFalse(any(issue["kind"] == "source_unavailable" for issue in unparsed["verification_issues"]))


if __name__ == "__main__":
    unittest.main()
