import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import disclosures


class DisclosureTests(unittest.TestCase):
    def review(self, codes, expected="5.00", quote=Decimal("5.00")):
        entries = [{"ID": "AN20261027", "TITLE": "合成纯债基金2026年第3季度报告", "PUBLISHDATEDesc": "2026-10-27"}, {"ID": "AN20260729", "TITLE": "合成纯债基金招募说明书", "PUBLISHDATEDesc": "2026-07-29"}]
        entries.extend({"ID": "AN" + str(year), "TITLE": f"合成纯债基金{year}年度报告", "PUBLISHDATEDesc": f"{year+1}-03-31"} for year in range(2020, 2026))
        funds = [{"code": code, "name": "合成纯债" + ("A" if code == "999001" else "C"), "full_name": "合成纯债基金", "inception_date": "2018-01-01", "annual_returns": {str(year): expected for year in range(2020, 2026)}} for code in codes]

        def document(client, entry, code, cache, refresh=False):
            return "合成纯债基金 基金主代码999001 下属份额交易代码999002 主要财务指标", {"source": "https://example.test/report/" + entry["ID"], "pdf_source": "https://example.test/report.pdf", "report_date": disclosures.period_end(entry["TITLE"]), "publication_date": entry["PUBLISHDATEDesc"]}

        with patch.object(disclosures, "announcements", return_value=entries), patch.object(disclosures, "document", side_effect=document), patch.object(disclosures, "portfolio", return_value={"status": "clear"}), patch.object(disclosures, "parse_assets", return_value={"fund_assets_cny": "110000000.00"}), patch.object(disclosures, "extract_scope", return_value=("仅投资国债", True)), patch.object(disclosures, "yearly_quote", return_value=quote), patch.object(disclosures, "event_findings", return_value=[]):
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


if __name__ == "__main__":
    unittest.main()
