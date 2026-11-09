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
    def __init__(self, catalog, phases):
        self.catalog = catalog
        self.phases = phases
        self.requests = []
        self.metadata_calls = []
        self.annual_calls = []
        self.review_calls = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        if url.endswith("fundcode_search.js"):
            return "var r = " + json.dumps(self.catalog) + ";"
        if url.startswith("https://fund.eastmoney.com/"):
            code = url.rsplit("/", 1)[1].removesuffix(".html")
            phase = self.phases[code]
            return f"截止至 {phase['data_date']} 近1周 近1月 近3月 近6月 今年来 阶段涨幅 0% 0% {phase['recent_3m']}% {phase['recent_6m']}% {phase['ytd']}% 交易状态：开放申购 开放赎回 购买手续费"
        raise AssertionError(f"未预期请求: {url}")


class PipelineTests(unittest.TestCase):
    def execute(self, directory, target=None, failed_identity=None, pending_review=None, timestamp=None, row_dates=None, page_dates=None, ytd_values=None, force=False, extra_fund=False, missing_year=None, qdii_fund=False, disclosed_review=False):
        data = dataset()
        funds = {fund["code"]: fund for fund in data["funds"]}
        if extra_fund or qdii_fund:
            funds["999002"] = {**funds["999001"], "code": "999002", "name": "合成新增纯债A"}
        for code, value in (ytd_values or {}).items():
            funds[code]["ytd"] = value
        catalog = [[code, "", fund["name"], "QDII-债券" if qdii_fund and code == "999002" else "债券型-长债"] for code, fund in funds.items()]
        phases = {code: {"data_date": (page_dates or {}).get(code, (row_dates or {}).get(code, data["as_of"])), **{field: fund[field] for field in ("recent_3m", "recent_6m", "ytd")}} for code, fund in funds.items()}
        client = FakeClient(catalog, phases)
        defaults = json.loads((ROOT / "references/defaults.json").read_text())
        timestamp = timestamp or datetime(2026, 11, 9, 12, 0, tzinfo=SHANGHAI)
        args = argparse.Namespace(target_code=target, workers=2, output_dir=directory, top_lists=True, started_at=timestamp.isoformat(), force_refresh=force)
        destination = directory / "run"
        destination.mkdir(exist_ok=True)
        rank_calls = []

        def ranking(client, kind):
            rank_calls.append(kind)
            result = {}
            for code, fund in funds.items():
                if (kind == "qdii") != (qdii_fund and code == "999002"):
                    continue
                row = [code, fund["name"], "", (row_dates or {}).get(code, data["as_of"])] + ["0"] * 13
                row[9], row[10], row[14] = fund["recent_3m"], fund["recent_6m"], fund["ytd"]
                result[code] = row
            return result

        def metadata(client, code):
            client.metadata_calls.append(code)
            if code == failed_identity:
                raise ValueError("模拟身份获取失败")
            return {"full_name": "合成完整基金" + code, "manager": "合成管理人", "currency": "CNY", "inception_date": "2018-01-01", "identity_source": "https://example.test/identity/" + code}

        def annual(client, code, inception, end):
            client.annual_calls.append(code)
            returns = dict(funds[code]["annual_returns"])
            if code == missing_year:
                returns.pop("2025")
            return {"annual_returns": returns, "annual_source": "https://example.test/annual/" + code}

        def review(client, members, cache, today, as_of, earliest):
            client.review_calls.append(members[0]["code"])
            fund = funds[members[0]["code"]]
            result = {"verified_codes": [fund["code"]], "latest_report": {"source": "https://example.test/report"}, **{key: fund[key] for key in ("pure_bond", "fund_assets_cny", "fund_assets_date", "fund_assets_basis", "fund_assets_sources", "annual_anomaly_review")}}
            if fund["code"] == pending_review:
                result["pure_bond"] = None
                result["verification_issues"] = [{"kind": "annual_quote_unparsed", "year": 2025, "message": "年报已取得但未识别出对应份额的全年收益", "source": "https://example.test/annual-report"}]
            if disclosed_review and fund["code"] == "999001":
                result["annual_return_conflicts"] = {"999001": {"2024": ["6.00", "6.03"]}}
                result["disclosure_notes"] = [{"code": "999001", "kind": "annual_quote_conflict", "year": 2024, "message": "平台6.00%，年报6.03%", "sources": ["https://example.test/annual-report"]}]
            return result

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
            self.assertTrue((destination / "decision_audit.json").exists())

    def test_disclosure_notes_reach_market_report_csv_json_and_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            status, destination, _, _ = self.execute(Path(temporary), disclosed_review=True)
            retained = json.loads((destination / "retained_funds.json").read_text())
            self.assertTrue(retained[0]["retained_with_notes"])
            self.assertTrue(retained[0]["return_source_sensitivity"]["decision_stable"])
            self.assertIn("6.03", retained[0]["note_text"])
            self.assertIn("※", (destination / "report.md").read_text())
            self.assertIn("note_text", (destination / "retained_funds.csv").read_text(encoding="utf-8-sig"))
            self.assertTrue((destination / "disclosure_notes.json").exists())
            audit = json.loads((destination / "decision_audit.json").read_text())
            self.assertTrue(audit["fund_decisions"]["999001"]["retained_with_notes"])

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

    def test_downloaded_but_unverified_report_is_a_coverage_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            status, destination, _, _ = self.execute(Path(temporary), pending_review="999001")
            coverage = json.loads((destination / "coverage.json").read_text())
            self.assertEqual(status, 3)
            self.assertEqual(coverage["unresolved_count"], 0)
            self.assertEqual(coverage["verification_issue_count"], 1)
            self.assertEqual(coverage["pending_share_class_count"], 1)
            self.assertEqual(coverage["status"], "completed_with_gaps")
            pending = json.loads((destination / "pending_funds.json").read_text())
            self.assertEqual(pending[0]["code"], "999001")
            self.assertEqual(pending[0]["verification_issues"][0]["kind"], "annual_quote_unparsed")
            self.assertTrue((destination / "verification_issues.json").exists())
            report = (destination / "report.md").read_text()
            self.assertIn("年报已取得但未识别出对应份额的全年收益", report)
            self.assertEqual(json.loads((destination / "retained_funds.json").read_text()), [])

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

    def test_sunday_reuses_friday_data_but_checks_directory_and_trade(self):
        sunday = datetime(2026, 11, 8, 12, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, timestamp=sunday)
            status, destination, client, ranks = self.execute(directory, timestamp=sunday.replace(hour=13))
            self.assertEqual(status, 0)
            self.assertEqual(ranks, ["qdii"])
            self.assertEqual(client.metadata_calls, [])
            self.assertEqual(client.annual_calls, [])
            self.assertEqual(len(client.review_calls), 2)
            self.assertEqual(len(client.requests), 4)
            coverage = json.loads((destination / "coverage.json").read_text())
            self.assertEqual(coverage["as_of"], "2026-11-06")
            self.assertEqual(coverage["screen_date"], "2026-11-08")
            self.assertEqual(coverage["annual_fresh_checks"], 0)
            self.assertEqual(coverage["annual_cache_reused"], 2)

    def test_repeated_same_snapshot_has_same_decision_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory)
            _, destination, _, _ = self.execute(directory)
            audit = json.loads((destination / "decision_audit.json").read_text())
            self.assertTrue(audit["previous_comparison"]["same_policy_and_code"])
            self.assertTrue(audit["previous_comparison"]["same_input"])
            self.assertTrue(audit["previous_comparison"]["same_result"])

    def test_monday_batches_detect_late_updates_and_same_date_corrections(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first_dates = {"400030": "2026-11-09", "999001": "2026-11-06"}
            status, destination, _, _ = self.execute(directory, row_dates=first_dates)
            self.assertEqual(status, 3)
            audit = json.loads((destination / "cache_audit.json").read_text())
            self.assertEqual(audit["phase_dates"], {"2026-11-06": 1, "2026-11-09": 1})
            _, _, client, ranks = self.execute(directory, row_dates=first_dates)
            self.assertEqual(ranks, ["zq", "qdii"])
            self.assertTrue(any(url.endswith("999001.html") for url, _ in client.requests))
            self.assertEqual(client.metadata_calls, [])
            next_dates = {"400030": "2026-11-09", "999001": "2026-11-09"}
            status, destination, client, _ = self.execute(directory, row_dates=next_dates)
            self.assertEqual(status, 0)
            self.assertEqual(client.annual_calls, [])
            status, destination, _, ranks = self.execute(directory, row_dates=next_dates, ytd_values={"999001": "1.00"})
            self.assertEqual(status, 0)
            self.assertEqual(ranks, ["zq", "qdii"])
            self.assertEqual(json.loads((destination / "retained_funds.json").read_text()), [])
            audit = json.loads((destination / "cache_audit.json").read_text())
            self.assertTrue(any(event["layer"] == "phase" and event["key"] == "999001" and event["action"] == "updated" for event in audit["events"]))

    def test_lagging_target_stops_but_records_partial_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ValueError, "目标净值落后"):
                self.execute(directory, row_dates={"999001": "2026-11-09"})
            audit = json.loads((directory / "run/cache_audit.json").read_text())
            self.assertEqual(audit["phase_dates"], {"2026-11-06": 1, "2026-11-09": 1})
            self.assertFalse((directory / "latest.json").exists())

    def test_force_refresh_bypasses_all_data_cache(self):
        sunday = datetime(2026, 11, 8, 12, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, timestamp=sunday)
            _, destination, client, ranks = self.execute(directory, timestamp=sunday, force=True)
            self.assertEqual(ranks, ["zq", "qdii"])
            self.assertEqual(len(client.metadata_calls), 2)
            self.assertEqual(len(client.annual_calls), 2)
            self.assertTrue(json.loads((destination / "cache_audit.json").read_text())["force_refresh"])

    def test_target_change_recomputes_using_cached_whole_market(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory)
            _, destination, client, _ = self.execute(directory, target="999001")
            self.assertEqual(client.metadata_calls, [])
            self.assertEqual(client.annual_calls, [])
            self.assertEqual(json.loads((destination / "screen_output.json").read_text())["target_code"], "999001")

    def test_new_fund_is_discovered_despite_cached_universe(self):
        sunday = datetime(2026, 11, 8, 12, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, timestamp=sunday)
            _, destination, client, ranks = self.execute(directory, timestamp=sunday, extra_fund=True)
            self.assertEqual(ranks, ["zq", "qdii"])
            self.assertEqual(client.metadata_calls, ["999002"])
            self.assertEqual(client.annual_calls, ["999002"])
            self.assertEqual(json.loads((destination / "coverage.json").read_text())["catalog_bond_share_classes"], 3)

    def test_failed_identity_and_missing_annual_year_are_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, failed_identity="999001")
            status, destination, client, _ = self.execute(directory, missing_year="999001")
            self.assertEqual(status, 3)
            audit = json.loads((destination / "market_audit.json").read_text())
            self.assertTrue(any(failure["stage"] == "numeric_history" for failure in audit["failures"]))
            self.assertEqual(client.metadata_calls, ["999001"])
            _, _, client, _ = self.execute(directory)
            self.assertEqual(client.annual_calls, ["999001"])

    def test_weekend_qdii_updates_do_not_freeze_with_domestic_calendar(self):
        sunday = datetime(2026, 11, 8, 12, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, timestamp=sunday, qdii_fund=True)
            status, destination, client, ranks = self.execute(directory, timestamp=sunday, qdii_fund=True, ytd_values={"999002": "1"})
            self.assertEqual(status, 0)
            self.assertEqual(ranks, ["qdii"])
            self.assertEqual(client.metadata_calls, [])
            self.assertEqual(client.annual_calls, [])
            retained = json.loads((destination / "retained_funds.json").read_text())
            self.assertEqual([fund["code"] for fund in retained], ["999001"])

    def test_weekend_probe_change_and_expired_snapshot_trigger_ranking_refresh(self):
        saturday = datetime(2026, 11, 7, 10, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, timestamp=saturday)
            _, _, client, ranks = self.execute(directory, timestamp=saturday.replace(hour=11), ytd_values={"400030": "4.35"})
            self.assertEqual(ranks, ["zq", "qdii"])
            self.assertEqual(client.metadata_calls, [])
            sunday = datetime(2026, 11, 8, 13, tzinfo=SHANGHAI)
            _, _, _, ranks = self.execute(directory, timestamp=sunday, ytd_values={"400030": "4.35"})
            self.assertEqual(ranks, ["zq", "qdii"])

    def test_incomplete_annual_review_invalidates_only_affected_share(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.execute(directory, pending_review="999001")
            _, _, client, _ = self.execute(directory)
            self.assertEqual(client.annual_calls, ["999001"])


if __name__ == "__main__":
    unittest.main()
