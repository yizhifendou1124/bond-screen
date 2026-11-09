import json
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import disclosures
from market_cache import MarketCache, closed_day, complete_annual, fingerprint
from market_data import SHANGHAI


class CacheTests(unittest.TestCase):
    timestamp = datetime(2026, 11, 9, 12, tzinfo=SHANGHAI)

    def test_reuse_preserves_original_timestamp_and_detects_hash_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                cache.store("annual", "400030", {"2025": "0.00"}, "v1")
                cache.timestamp += timedelta(hours=1)
                self.assertEqual(cache.load("annual", "400030", "v1", timedelta(days=7)), {"2025": "0.00"})
                self.assertEqual(cache.events[-1]["original_fetched_at"], self.timestamp.isoformat())
                cache.connection.execute("UPDATE records SET payload='{}'")
                self.assertIsNone(cache.load("annual", "400030", "v1", timedelta(days=7)))
            finally:
                cache.close()

    def test_version_expiry_future_dates_and_force_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                cache.store("identity", "400030", {"name": "test"}, "v1")
                self.assertIsNone(cache.load("identity", "400030", "v2", timedelta(days=7)))
                cache.timestamp += timedelta(days=8)
                self.assertIsNone(cache.load("identity", "400030", "v1", timedelta(days=7)))
                cache.timestamp = self.timestamp - timedelta(hours=1)
                self.assertIsNone(cache.load("identity", "400030", "v1", timedelta(days=7)))
                cache.timestamp = self.timestamp
                cache.force = True
                self.assertIsNone(cache.load("identity", "400030", "v1", timedelta(days=7)))
            finally:
                cache.close()

    def test_wrong_nav_date_and_pending_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                value = {"data_date": "2026-11-06", "ytd": "4.34"}
                cache.store("phase", "400030", value, "v1", data_date=value["data_date"])
                self.assertIsNone(cache.load("phase", "400030", "v1", timedelta(days=7), "2026-11-09"))
                cache.invalidate("phase", "400030")
                self.assertIsNone(cache.load("phase", "400030", "v1", timedelta(days=7), "2026-11-06"))
            finally:
                cache.close()

    def test_same_date_correction_and_no_date_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                cache.store("phase", "400030", {"ytd": "4.34"}, "v1", data_date="2026-11-09")
                cache.store("phase", "400030", {"ytd": "4.35"}, "v1", data_date="2026-11-09")
                cache.store("phase", "400030", {"ytd": "4.30"}, "v1", data_date="2026-11-06")
                self.assertEqual(cache.load("phase", "400030", "v1", timedelta(days=7), "2026-11-09"), {"ytd": "4.35"})
                self.assertEqual(cache.audit()["counts"]["phase"]["updated"], 2)
            finally:
                cache.close()

    def test_incomplete_rank_does_not_erase_complete_matching_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                detail = {"data_date": "2026-11-09", "ytd": "4.34", "recent_6m": "2.3", "recent_3m": "1.2"}
                cache.store("phase", "400030", detail, "v1", data_date=detail["data_date"])
                cache.store("phase", "400030", {**detail, "recent_3m": None}, "v1", complete=False, data_date=detail["data_date"])
                self.assertEqual(cache.load("phase", "400030", "v1", timedelta(days=7), detail["data_date"]), detail)
                cache.store("phase", "400030", {**detail, "ytd": "4.35", "recent_3m": None}, "v1", complete=False, data_date=detail["data_date"])
                self.assertIsNone(cache.load("phase", "400030", "v1", timedelta(days=7), detail["data_date"]))
            finally:
                cache.close()

    def test_failed_fetch_does_not_fall_back_to_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                cache.store("annual", "400030", {"2025": "5"}, "v1")
                cache.timestamp += timedelta(days=8)
                loader = Mock(side_effect=RuntimeError("source unavailable"))
                with self.assertRaises(RuntimeError):
                    cache.fetch("annual", "400030", "v1", loader)
                loader.side_effect = None
                loader.return_value = {"2025": "6"}
                self.assertEqual(cache.fetch("annual", "400030", "v1", loader), {"2025": "6"})
                self.assertEqual(loader.call_count, 2)
            finally:
                cache.close()

    def test_incomplete_success_retried_not_cached_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            try:
                loader = Mock(return_value={"annual_returns": {"2025": None}})
                validator = lambda value: complete_annual(value, "2025-01-01", 2025)
                cache.fetch("annual", "400030", "v1", loader, validator)
                loader.return_value = {"annual_returns": {"2025": "0"}}
                cache.fetch("annual", "400030", "v1", loader, validator)
                cache.fetch("annual", "400030", "v1", loader, validator)
                self.assertEqual(loader.call_count, 2)
            finally:
                cache.close()

    def test_annual_completeness_respects_own_full_years_and_real_zero(self):
        self.assertTrue(complete_annual({"annual_returns": {"2024": "0", "2025": "-0.00"}}, "2023-05-01", 2025))
        self.assertFalse(complete_annual({"annual_returns": {"2025": "0"}}, "2023-05-01", 2025))
        self.assertTrue(complete_annual({"annual_returns": {}}, "2026-01-01", 2025))

    def test_calendar_weekends_exchange_holidays_and_unknown_year(self):
        self.assertTrue(closed_day(date(2026, 11, 8)))
        self.assertFalse(closed_day(date(2026, 11, 9)))
        self.assertTrue(closed_day(date(2026, 10, 5)))
        self.assertTrue(closed_day(date(2026, 10, 10)))
        self.assertFalse(closed_day(date(2027, 1, 1)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            path.write_text(json.dumps({"source": "https://example.test/calendar", "holidays": ["2027-01-01"]}))
            self.assertTrue(closed_day(date(2027, 1, 1), path))
            path.write_text("{}")
            with self.assertRaises(ValueError):
                closed_day(date(2027, 1, 1), path)

    def test_persistent_cache_survives_process_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketCache(directory, self.timestamp)
            cache.store("identity", "400030", {"source": "https://example.test"}, fingerprint("parser"))
            cache.close()
            reopened = MarketCache(directory, self.timestamp + timedelta(hours=1))
            try:
                self.assertEqual(reopened.load("identity", "400030", fingerprint("parser"), timedelta(days=7)), {"source": "https://example.test"})
            finally:
                reopened.close()


class DocumentCacheTests(unittest.TestCase):
    def test_pdf_text_reuse_corrections_corruption_parser_changes_and_force(self):
        timestamp = datetime(2026, 11, 9, 12, tzinfo=SHANGHAI)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            cache = root / "cache"
            client = SimpleNamespace(evidence=evidence, manifest=[], lock=threading.Lock(), get=Mock(return_value=b"%PDF synthetic"), force_refresh=False)
            entry = {"ID": "AN12345", "TITLE": "合成基金2025年度报告", "PUBLISHDATEDesc": "2026-03-31"}
            page = Mock()
            page.extract_text.return_value = "合成全文" * 100
            reader = Mock(return_value=SimpleNamespace(pages=[page]))
            with patch.object(disclosures, "PdfReader", reader), patch.object(disclosures, "now", return_value=timestamp):
                first, _ = disclosures.document(client, entry, "400030", cache, refresh=True)
                second, _ = disclosures.document(client, entry, "400030", cache, refresh=True)
                self.assertEqual(first, second)
                self.assertEqual(client.get.call_count, 1)
                self.assertEqual(reader.call_count, 1)
                self.assertTrue(client.manifest[-1]["extracted_text_reused"])
                (cache / "AN12345.txt").write_bytes(b"\xff")
                disclosures.document(client, entry, "400030", cache)
                self.assertEqual(reader.call_count, 2)
                metadata_path = cache / "AN12345.json"
                metadata = json.loads(metadata_path.read_text())
                metadata["parser_version"] = "old parser"
                metadata_path.write_text(json.dumps(metadata))
                disclosures.document(client, entry, "400030", cache)
                self.assertEqual(reader.call_count, 3)
                self.assertEqual(client.get.call_count, 1)
                (cache / "AN12345.pdf").write_bytes(b"tampered")
                disclosures.document(client, entry, "400030", cache)
                self.assertEqual(client.get.call_count, 2)
                client.force_refresh = True
                client.get.return_value = b"%PDF corrected"
                disclosures.document(client, entry, "400030", cache)
                self.assertEqual(client.get.call_count, 3)
                self.assertEqual((cache / "AN12345.pdf").read_bytes(), b"%PDF corrected")
                self.assertFalse(client.manifest[-1]["immutable_pdf_reused_after_live_announcement_check"])
            client.force_refresh = False
            with patch.object(disclosures, "PdfReader", reader), patch.object(disclosures, "now", return_value=timestamp + timedelta(days=8)):
                disclosures.document(client, entry, "400030", cache)
                self.assertEqual(client.get.call_count, 4)

    def test_bad_pdf_never_creates_a_complete_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = SimpleNamespace(get=Mock(return_value=b"not a pdf"))
            entry = {"ID": "AN12345", "TITLE": "合成基金2025年度报告", "PUBLISHDATEDesc": "2026-03-31"}
            for attempt in range(2):
                with self.assertRaises(ValueError):
                    disclosures.document(client, entry, "400030", root / "cache")
                self.assertEqual(client.get.call_count, attempt + 1)
            self.assertFalse((root / "cache/AN12345.json").exists())


if __name__ == "__main__":
    unittest.main()
