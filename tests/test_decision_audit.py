import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import screen_funds
from decision_audit import build_audit, write_audit
from test_screen_funds import dataset


class DecisionAuditTests(unittest.TestCase):
    def test_reordered_inputs_and_results_have_identical_fingerprints(self):
        first = dataset()
        output = screen_funds.screen(first)
        original = copy.deepcopy(first)
        audit = build_audit(ROOT, first, output)
        first["funds"].reverse()
        reordered = screen_funds.screen(first)
        reordered["results"].reverse()
        same = build_audit(ROOT, first, reordered)
        self.assertEqual(audit["input_sha256"], same["input_sha256"])
        self.assertEqual(audit["result_sha256"], same["result_sha256"])
        self.assertEqual(original["funds"][0]["annual_returns"], first["funds"][1]["annual_returns"])

    def test_same_version_and_input_changed_result_stops_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            data = dataset()
            output = screen_funds.screen(data)
            write_audit(ROOT, first, root, data, output)
            pointer = {"run_directory": str(first)}
            (root / "latest.json").write_text(json.dumps(pointer))
            output["results"][1]["reason"] = "UNAUTHORIZED_FUTURES_FILTER"
            with self.assertRaisesRegex(ValueError, "同一规则版本"):
                write_audit(ROOT, second, root, data, output)
            self.assertEqual(json.loads((root / "latest.json").read_text()), pointer)
            self.assertTrue(json.loads((second / "decision_audit.json").read_text())["determinism_violation"])

    def test_changed_verification_facts_are_visible_not_called_price_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            data = dataset()
            write_audit(ROOT, first, root, data, screen_funds.screen(data))
            (root / "latest.json").write_text(json.dumps({"run_directory": str(first)}))
            data["funds"][1]["pure_bond"] = None
            audit = write_audit(ROOT, second, root, data, screen_funds.screen(data))
            self.assertTrue(audit["previous_comparison"]["same_policy_and_code"])
            self.assertFalse(audit["previous_comparison"]["same_input"])
            self.assertFalse(audit["previous_results_used_for_selection"])
            changed = next(item for item in audit["fund_decision_changes"] if item["code"] == data["funds"][1]["code"])
            self.assertTrue(changed["fund_input_changed"])
            self.assertFalse(changed["fund_performance_changed"])
            self.assertEqual(changed["after"]["status"], "observation")

    def test_old_latest_without_audit_does_not_supply_candidate_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "current"
            destination.mkdir()
            (root / "latest.json").write_text(json.dumps({"run_directory": str(root / "legacy")}))
            data = dataset()
            audit = write_audit(ROOT, destination, root, data, screen_funds.screen(data))
            self.assertTrue(audit["previous_comparison_unavailable"])
            self.assertFalse(audit["previous_results_used_for_selection"])
