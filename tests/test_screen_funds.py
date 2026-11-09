import copy
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import screen_funds as engine
from run_market import numeric_screen, representatives


def fund(code="999001", name="合成纯债A", annual=None, ytd="5.00"):
    return {"code": code, "name": name, "data_date": "2026-11-06", "pure_bond": True, "history_basis": "share_class", "inception_date": "2018-01-01", "annual_returns": annual or {str(year): "6.00" for year in range(2020, 2026)}, "ytd": ytd, "recent_6m": "2.30", "recent_3m": "1.20", "sources": ["https://example.test/synthetic"], "annual_anomaly_review": {"status": "clear", "years": list(range(2020, 2026)), "sources": ["https://example.test/synthetic-review"], "findings": []}, "fund_assets_cny": "100000000", "fund_assets_basis": "fund_total", "fund_assets_date": "2026-09-30", "fund_assets_sources": ["https://example.test/synthetic-report"], "subscription_status": "open", "subscription_checked_on": "2026-11-09", "subscription_channel": "合成测试渠道", "subscription_sources": ["https://example.test/synthetic-trade"]}


def dataset(candidates=None):
    target = fund("400030", "合成目标", {"2020": "3", "2021": "6", "2022": "2", "2023": "6", "2024": "6", "2025": "2.2"}, "4.34")
    return {"as_of": "2026-11-06", "screen_date": "2026-11-09", "calendar_end_year": 2025, "history_fill_groups": [], "funds": [target] + (candidates or [fund()])}


def candidate_result(data, code="999001"):
    return next(result for result in engine.screen(data)["results"] if result["code"] == code)


class ScreenTests(unittest.TestCase):
    def test_safe_quote_conflict_is_retained_with_notes_without_overwriting_returns(self):
        data = dataset()
        candidate = data["funds"][1]
        candidate["annual_returns"]["2024"] = "6.79"
        candidate["annual_return_conflicts"] = {"2024": ["6.79", "6.82"]}
        candidate["disclosure_notes"] = [{"kind": "annual_quote_conflict", "message": "平台6.79%，年报6.82%"}]
        original = copy.deepcopy(data)
        result = candidate_result(data)
        self.assertEqual(result["status"], "qualified")
        self.assertTrue(result["retained_with_notes"])
        self.assertTrue(result["return_source_sensitivity"]["decision_stable"])
        self.assertEqual(data, original)

    def test_decision_sensitive_quote_conflict_remains_pending(self):
        data = dataset()
        candidate = data["funds"][1]
        candidate["annual_returns"] = dict(data["funds"][0]["annual_returns"])
        candidate["annual_returns"]["2025"] = "2.25"
        candidate["annual_return_conflicts"] = {"2025": ["2.25", "2.00"]}
        candidate["ytd"] = "4.00"
        result = candidate_result(data)
        self.assertEqual(result["status"], "observation")
        self.assertEqual(result["reason"], "RETURN_SOURCE_CONFLICT_DECISION_SENSITIVE")

    def test_mixed_source_worst_case_anomaly_not_only_two_endpoint_probes(self):
        data = dataset()
        data["funds"][1]["annual_return_conflicts"] = {"2024": ["6.00", "20.90"], "2025": ["6.00", "0.00"]}
        self.assertEqual(candidate_result(data)["status"], "observation")

    def test_quote_conflict_with_possible_negative_year_is_not_waived(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2024"] = "0.00"
        data["funds"][1]["annual_return_conflicts"] = {"2024": ["0.00", "-0.01"]}
        self.assertEqual(candidate_result(data)["reason"], "NONPOSITIVE_ANNUAL_RETURN")
        self.assertEqual(data["funds"][1]["annual_returns"]["2024"], "0.00")

    def test_quote_conflict_with_possible_zero_year_is_not_waived(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2024"] = "0.01"
        data["funds"][1]["annual_return_conflicts"] = {"2024": ["0.01", "0.00"]}
        result = candidate_result(data)
        self.assertEqual(result["reason"], "RETURN_SOURCE_CONFLICT_DECISION_SENSITIVE")
        self.assertFalse(result["return_source_sensitivity"]["decision_stable"])
        self.assertEqual(data["funds"][1]["annual_returns"]["2024"], "0.01")

    def test_source_conflict_is_propagated_from_history_fill_donor(self):
        candidate = fund()
        donor = fund("999002", "合成纯债C")
        candidate["annual_returns"]["2024"] = None
        donor["annual_returns"]["2024"] = "6.79"
        donor["annual_return_conflicts"] = {"2024": ["6.79", "6.82"]}
        data = dataset([candidate, donor])
        data["history_fill_groups"] = [{"codes": ["999001", "999002"], "preferred_codes": ["999002"], "sources": ["https://example.test/identity"]}]
        result = candidate_result(data)
        self.assertEqual(result["status"], "qualified")
        self.assertTrue(result["return_source_sensitivity"]["decision_stable"])
        self.assertTrue(any(note["kind"] == "inherited_annual_quote_conflict" for note in result["disclosure_notes"]))

    def test_target_quote_conflict_is_not_silently_waived(self):
        data = dataset()
        data["funds"][0]["annual_return_conflicts"] = {"2024": ["6", "6.03"]}
        output = engine.screen(data)
        self.assertIn("TARGET_RETURN_SOURCE_CONFLICT_UNCONFIRMED", output["target_data_unavailable"])
        self.assertEqual(candidate_result(data)["status"], "observation")

    def test_other_share_conflict_cannot_hide_whole_fund_anomaly(self):
        candidate = fund()
        other = fund("999002", "合成纯债C")
        other["annual_return_conflicts"] = {"2024": ["6.00", "22.00"]}
        data = dataset([candidate, other])
        data["history_fill_enabled"] = False
        data["history_fill_groups"] = [{"codes": ["999001", "999002"], "sources": ["https://example.test/identity"]}]
        result = candidate_result(data)
        self.assertEqual(result["status"], "observation")

    def test_source_difference_may_change_category_but_not_pass_status(self):
        data = dataset()
        candidate = data["funds"][1]
        candidate["annual_returns"] = dict(data["funds"][0]["annual_returns"])
        candidate["annual_returns"]["2025"] = "2.25"
        candidate["annual_return_conflicts"] = {"2025": ["2.25", "2.00"]}
        candidate["ytd"] = "4.04"
        result = candidate_result(data)
        self.assertEqual(result["status"], "qualified")
        self.assertTrue(result["retained_with_notes"])
        self.assertGreater(len(set(result["return_source_sensitivity"]["probe_reasons"])), 1)

    def test_default_target(self):
        self.assertEqual(engine.screen(dataset())["target_code"], "400030")
        self.assertEqual(candidate_result(dataset())["status"], "qualified")

    def test_manual_target_is_not_persisted(self):
        output = engine.screen(dataset(), target_override="999001")
        self.assertEqual(output["target_code"], "999001")
        self.assertEqual(json.loads((ROOT / "references/defaults.json").read_text())["target_code"], "400030")

    def test_unknown_target_rejected(self):
        with self.assertRaises(ValueError):
            engine.screen(dataset(), target_override="999999")

    def test_exact_size_and_one_cent_below(self):
        data = dataset()
        self.assertEqual(candidate_result(data)["status"], "qualified")
        data["funds"][1]["fund_assets_cny"] = "99999999.99"
        self.assertEqual(candidate_result(data)["reason"], "FUND_ASSETS_BELOW_MINIMUM")

    def test_missing_size_is_observation(self):
        data = dataset()
        data["funds"][1].pop("fund_assets_sources")
        self.assertEqual(candidate_result(data)["status"], "observation")

    def test_closed_and_zero_quota(self):
        for status, limit in [("closed", None), ("limited", "0")]:
            with self.subTest(status=status):
                data = dataset()
                data["funds"][1].update(subscription_status=status, subscription_limit_cny=limit)
                self.assertEqual(candidate_result(data)["reason"], "CURRENTLY_NOT_SUBSCRIBABLE")

    def test_positive_quota(self):
        data = dataset()
        data["funds"][1].update(subscription_status="limited", subscription_limit_cny="10")
        self.assertEqual(candidate_result(data)["status"], "qualified")

    def test_stale_trade_is_observation(self):
        data = dataset()
        data["funds"][1]["subscription_checked_on"] = "2026-11-08"
        self.assertEqual(candidate_result(data)["status"], "observation")

    def test_negative_every_complete_year(self):
        for year in range(2020, 2026):
            with self.subTest(year=year):
                data = dataset()
                data["funds"][1]["annual_returns"][str(year)] = "-0.000000000001"
                self.assertEqual(candidate_result(data)["reason"], "NONPOSITIVE_ANNUAL_RETURN")

    def test_pre2020_negative_and_anomaly_ignored(self):
        data = dataset()
        data["funds"][1]["annual_returns"].update({"2018": "-20", "2019": "90"})
        self.assertEqual(candidate_result(data)["status"], "qualified")

    def test_zero_and_negative_zero_every_complete_year(self):
        for year in range(2020, 2026):
            for value in ["0", "0.00", "-0", "-0.00"]:
                with self.subTest(year=year, value=value):
                    data = dataset()
                    data["funds"][1]["annual_returns"][str(year)] = value
                    output = engine.screen(data)
                    result = next(item for item in output["results"] if item["code"] == "999001")
                    self.assertEqual(result["status"], "excluded")
                    self.assertEqual(result["reason"], "NONPOSITIVE_ANNUAL_RETURN")
                    self.assertEqual(result["nonpositive_annual_returns_pct"], {str(year): value})
                    self.assertEqual(output["positive_annual_years"], list(range(2020, 2026)))
                    self.assertEqual(result["witnesses"][-1]["rule"], "R4")

    def test_tiny_positive_return_is_not_rounded_to_zero(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2020"] = "0.000000000001"
        result = candidate_result(data)
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["nonpositive_annual_returns_pct"], {})

    def test_pre2020_zero_is_out_of_scope(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2019"] = "0"
        self.assertEqual(candidate_result(data)["status"], "qualified")

    def test_zero_target_remains_benchmark(self):
        data = dataset()
        data["funds"][0]["annual_returns"]["2020"] = "0"
        result = next(item for item in engine.screen(data)["results"] if item["code"] == "400030")
        self.assertEqual(result["status"], "benchmark")

    def test_zero_year_after_frozen_long_term_window_is_excluded(self):
        data = dataset()
        data.update(as_of="2027-11-05", screen_date="2027-11-08")
        for entry in data["funds"]:
            entry.update(data_date="2027-11-05", subscription_checked_on="2027-11-08")
            entry["annual_returns"]["2026"] = "6"
            entry["annual_anomaly_review"]["years"].append(2026)
        data["funds"][1]["annual_returns"]["2026"] = "0"
        self.assertEqual(candidate_result(data)["reason"], "NONPOSITIVE_ANNUAL_RETURN")

    def test_missing_year_is_observation(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2020"] = None
        result = candidate_result(data)
        self.assertEqual(result["status"], "observation")
        self.assertIn("POSITIVE_ANNUAL_HISTORY_INCOMPLETE", result["missing"])
        self.assertEqual(result["nonpositive_annual_returns_pct"], {})

    def test_anomaly_exact_15(self):
        data = dataset()
        data["funds"][1]["annual_returns"]["2023"] = "21"
        self.assertEqual(candidate_result(data)["reason"], "ABNORMAL_ANNUAL_RETURN")
        data["funds"][1]["annual_returns"]["2023"] = "20.9999999999"
        self.assertEqual(candidate_result(data)["status"], "qualified")

    def test_ytd_gap_two_exact(self):
        for ytd, expected in [("2.34", "excluded"), ("2.340000001", "qualified")]:
            data = dataset([fund(ytd=ytd)])
            self.assertEqual(candidate_result(data)["status"], expected)

    def test_r1_strict_and_historical_tie(self):
        data = dataset()
        data["funds"][1]["annual_returns"] = {year: str(Decimal(value) - Decimal("0.1")) for year, value in data["funds"][0]["annual_returns"].items()}
        self.assertIn("R1", [entry["rule"] for entry in candidate_result(data)["witnesses"]])
        data["funds"][1]["annual_returns"]["2020"] = "3"
        self.assertNotIn("R1", [entry["rule"] for entry in candidate_result(data)["witnesses"]])

    def test_two_lower_recent_floor(self):
        annual = {"2020": "3.2", "2021": "3", "2022": "2", "2023": "4", "2024": "4", "2025": "2"}
        data = dataset([fund(annual=annual, ytd="4.04")])
        self.assertEqual(candidate_result(data)["reason"], "BOTH_LOWER_RECENT_EXCEPTION")
        data["funds"][1]["ytd"] = "4.0399999"
        self.assertEqual(candidate_result(data)["status"], "excluded")

    def test_actual_holdings_requires_evidence(self):
        data = dataset()
        data["funds"][1]["pure_bond_basis"] = "actual_holdings"
        self.assertEqual(candidate_result(data)["status"], "observation")
        data["funds"][1]["pure_bond_review"] = {"report_date": "2026-09-30", "checked_on": "2026-11-09", "coverage_complete": True, "non_pure_assets_held": False, "sources": ["https://example.test/complete-holdings"], "findings": ["合成完整组合和交易核查"]}
        self.assertEqual(candidate_result(data)["status"], "qualified")

    def test_no_chain_or_overwrite(self):
        data = dataset([fund("999001"), fund("999002"), fund("999003")])
        data["history_fill_groups"] = [{"codes": ["999001", "999002", "999003"], "sources": ["https://example.test/identity"]}]
        data["funds"][1]["annual_returns"]["2020"] = "0"
        data["funds"][2]["annual_returns"]["2020"] = None
        data["funds"][3]["annual_returns"]["2020"] = None
        output = engine.screen(data)
        self.assertEqual(output["same_fund_data_completion"]["999003"]["annual_returns"]["2020"]["source_code"], "999001")
        self.assertNotIn("999001", output["same_fund_data_completion"])
        for code in ["999001", "999002", "999003"]:
            result = next(item for item in output["results"] if item["code"] == code)
            self.assertEqual(result["reason"], "NONPOSITIVE_ANNUAL_RETURN")
            self.assertEqual(result["nonpositive_annual_returns_pct"], {"2020": "0"})
        self.assertEqual(data["funds"][1]["annual_returns"]["2020"], "0")

    def test_sort_uses_raw_values_and_not_cagr(self):
        candidates = [fund("999002", ytd="4.343"), fund("999003", ytd="4.344"), fund("999001", ytd="4.343")]
        output = engine.screen(dataset(candidates))
        qualified = [result["code"] for result in output["results"] if result["status"] == "qualified"]
        self.assertEqual(qualified, ["999003", "999001", "999002"])

    def test_group_order_before_ytd(self):
        annual = {"2020": "3.2", "2021": "3", "2022": "2", "2023": "4", "2024": "4", "2025": "2"}
        output = engine.screen(dataset([fund("999001", ytd="4.35"), fund("999002", annual=annual, ytd="10")]))
        self.assertEqual([result["code"] for result in output["results"] if result["status"] == "qualified"], ["999001", "999002"])

    def test_inputs_unchanged(self):
        data = dataset()
        before = copy.deepcopy(data)
        engine.screen(data)
        self.assertEqual(data, before)

    def test_numeric_prefilter_matches_engine(self):
        defaults = json.loads((ROOT / "references/defaults.json").read_text())
        for ytd in ["2.34", "2.35", "3.8", "4.04", "5.0"]:
            for annual_value in ["-0.01", "0", "-0.00", "0.000000000001", "3", "6", "30"]:
                with self.subTest(ytd=ytd, annual=annual_value):
                    candidate = fund(ytd=ytd)
                    candidate["annual_returns"]["2020"] = annual_value
                    data = dataset([candidate])
                    raw = {entry["code"]: entry for entry in data["funds"]}
                    numeric = numeric_screen(raw, [], "400030", defaults, data["as_of"])
                    self.assertEqual(numeric["999001"] == "numeric_candidate", candidate_result(data)["status"] == "qualified")


if __name__ == "__main__":
    unittest.main()
