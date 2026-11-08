import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from market_data import Client


class ClientTests(unittest.TestCase):
    def test_changed_response_same_url_preserves_original_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Client(temporary, interval=0)
            responses = []
            for content in [b"first response", b"second response"]:
                response = MagicMock(url="https://source.test/data")
                response.__enter__.return_value = response
                response.iter_content.return_value = [content]
                responses.append(response)
            client.local.session = MagicMock()
            client.local.session.get.side_effect = responses
            client.get("https://source.test/data")
            client.get("https://source.test/data")
            self.assertEqual(len(list(Path(temporary).glob("*.txt"))), 2)
            self.assertNotEqual(client.manifest[0]["path"], client.manifest[1]["path"])

    def test_same_host_pacing_and_independent_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Client(temporary, interval=0.5)
            clock = [0.0]

            def sleep(seconds):
                clock[0] += seconds

            with patch("market_data.time.monotonic", side_effect=lambda: clock[0]), patch("market_data.time.sleep", side_effect=sleep):
                client.pace("https://first.test/a")
                client.pace("https://second.test/a")
                self.assertEqual(clock[0], 0)
                client.pace("https://first.test/b")
                self.assertEqual(clock[0], 0.5)

    def test_frequency_cap_cools_all_workers_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Client(temporary, retries=2)
            blocked = MagicMock(status_code=514, headers={"Retry-After": "45"})
            blocked.__enter__.return_value = blocked
            blocked.raise_for_status.side_effect = requests.HTTPError("Frequency Capped", response=blocked)
            success = MagicMock(url="https://source.test/data")
            success.__enter__.return_value = success
            success.iter_content.return_value = [b"fresh data"]
            session = MagicMock()
            session.get.side_effect = [blocked, success]
            client.local.session = session
            clock = [0.0]

            def sleep(seconds):
                clock[0] += seconds

            with patch("market_data.time.monotonic", side_effect=lambda: clock[0]), patch("market_data.time.sleep", side_effect=sleep):
                self.assertEqual(client.get("https://source.test/data"), "fresh data")
            self.assertGreaterEqual(clock[0], 45)
            self.assertEqual(session.get.call_count, 2)
            self.assertTrue(blocked.__exit__.called)
            self.assertEqual(client.manifest[0]["status"], "source_throttled")
            self.assertEqual(client.manifest[0]["cooldown_seconds"], 45)
            self.assertTrue(list(Path(temporary).glob("*.txt")))


if __name__ == "__main__":
    unittest.main()
