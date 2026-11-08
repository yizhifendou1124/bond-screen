import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_market
from market_data import SHANGHAI
from test_screen_funds import dataset


class FakeClient:
    def __init__(self, catalog):
        self.catalog = catalog
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        if url.endswith("fundcode_search.js"):
            return "var r = " + json.dumps(self.catalog) + ";"
        if url.startswith("https://fund.eastmoney.com/"):
            return "交易状态：开放申购 开放赎回 购买手续费"
        raise AssertionError(f"未预期请求: {url}")


class PipelineTests(unittest.TestCase):
    def execute(self, directory, target=None, failed_identity=None):
        data = dataset()
        funds = {fund["code"]: fund for fund in data["funds"]}
        catalog = [[code, "", fund["name"], "债券型-长债"] for code, fund in funds.items()]
        client = FakeClient(catalog)
        defaults = json.loads((ROOT / "references/defaults.json").read_text())
        timestamp = datetime(2026, 11, 9, 12, 0, tzinfo=SHANGHAI)
        args = argparse.Namespace(target_code=target, workers=2, output_dir=directory, top_lists=True, started_at=timestamp.isoformat())
        destination = directory / "run"
        destination.mkdir(exist_ok=True)
        rank_calls = []

        def ranking(client, kind):
            rank_calls.append(kind)
            if kind == "qdii":
                return {}
            result = {}
            for code, fund in funds.items():
                row = [code, fund["name"], "", data["as_of"]] + ["0"] * 13
                row[9], row[10], row[14] = fund["recent_3m"], fund["recent_6m"], fund["ytd"]
                result[code] = row
            return result

        def metadata(client, code):
            if code == failed_identity:
                raise ValueError("模拟身份获取失败")
            return {"full_name": "合成完整基金" + code, "manager": "合成管理人", "currency": "CNY", "inception_date": "2018-01-01", "identity_source": "https://example.test/identity/" + code}

        def annual(client, code, inception, end):
            return {"annual_returns": funds[code]["annual_returns"], "annual_source": "https://example.test/annual/" + code}

        def review(client, members, cache, today, as_of, earliest):
            fund = funds[members[0]["code"]]
            return {"verified_codes": [fund["code"]], "latest_report": {"source": "https://example.test/report"}, **{key: fund[key] for key in ("pure_bond", "fund_assets_cny", "fund_assets_date", "fund_assets_basis", "fund_assets_sources", "annual_anomaly_review")}}

        with patch.object(run_market, "now", return_value=timestamp), patch.object(run_market, "ranking", side_effect=ranking), patch.object(run_market, "metadata", side_effect=metadata), patch.object(run_market, "annual", side_effect=annual), patch.object(run_market, "review_family", side_effect=review):
            status = run_market.run(args, destination, client, defaults)
        return status, destination, client, rank_calls

    def test_end_to_end_exports_and_fresh_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            status, destination, client, ranks = self.execute(directory)
            self.assertEqual(status, 0)
            self.assertTrue(client.requests[0][0].endswith("fundcode_search.js"))
            self.assertEqual(ranks, ["zq", "qdii"])
            output = json.loads((destination / "screen_output.json").read_text())
            retained = json.loads((destination / "retained_funds.json").read_text())
            self.assertEqual(output["target_code"], "400030")
            self.assertEqual([fund["code"] for fund in retained], ["999001"])
            self.assertEqual(json.loads((directory / "latest.json").read_text())["qualified_count"], 1)
            self.assertTrue((destination / "report.md").exists())
            self.assertTrue((destination / "retained_funds.csv").exists())
            self.assertTrue((destination / "top_lists.json").exists())

    def test_manual_target_online(self):
        with tempfile.TemporaryDirectory() as temporary:
            status, destination, _, _ = self.execute(Path(temporary), target="999001")
            self.assertEqual(status, 0)
            self.assertEqual(json.loads((destination / "screen_output.json").read_text())["target_code"], "999001")
            self.assertEqual(json.loads((ROOT / "references/defaults.json").read_text())["target_code"], "400030")

    def test_partial_coverage_not_false_full_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            status, destination, _, _ = self.execute(Path(temporary), failed_identity="999001")
            self.assertEqual(status, 3)
            coverage = json.loads((destination / "coverage.json").read_text())
            self.assertEqual(coverage["status"], "completed_with_gaps")
            self.assertEqual(coverage["unresolved_count"], 1)

    def test_target_failure_does_not_publish_or_load_old_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = {"run_directory": "old-run", "qualified_count": 99}
            (directory / "latest.json").write_text(json.dumps(previous))
            with self.assertRaises(ValueError):
                self.execute(directory, failed_identity="400030")
            self.assertEqual(json.loads((directory / "latest.json").read_text()), previous)
            self.assertFalse((directory / "run/retained_funds.json").exists())

    def test_recent_lists_sort_using_exact_completed_returns(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _, destination, _, _ = self.execute(directory)
            output = json.loads((destination / "screen_output.json").read_text())
            retained = json.loads((destination / "retained_funds.json").read_text())
            first = {**retained[0], "code": "999001", "recent_3m": "1.23", "data_completion": {"recent_3m": {"return_pct": "1.2344"}}}
            second = {**first, "code": "999002", "data_completion": {"recent_3m": {"return_pct": "1.2345"}}}
            raw = {fund["code"]: {**fund, "recent_3m": None} for fund in [first, second]}
            coverage = json.loads((destination / "coverage.json").read_text())
            with patch.object(run_market, "representatives", return_value=[first, second]):
                run_market.export_report(destination, output, raw, [], coverage, True)
            lists = json.loads((destination / "top_lists.json").read_text())
            self.assertEqual([fund["code"] for fund in lists["recent_3m"]], ["999002", "999001"])


if __name__ == "__main__":
    unittest.main()
