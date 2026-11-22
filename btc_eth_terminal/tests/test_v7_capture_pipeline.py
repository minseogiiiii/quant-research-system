"""Offline engineering fixtures; these are NOT authentic market observations."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import acquire_validate
import bridge
import fetch_btc_eth
from independent_audit import IndependentAuditError, audit_bundle_independently

START = int(datetime(2026, 11, 8, tzinfo=UTC).timestamp())
END = START + 2 * 300


def fixture(product, a, b):
    p = "69000.1200" if product == "BTC-USD" else "3150.0500"
    candles = [{"start": str(t), "open": p, "high": p, "low": p,
                "close": p, "volume": "0.1"} for t in range(a, b, 300)]
    raw = json.dumps({"candles": candles}).encode("utf-8")
    return raw, json.loads(raw)


class TestV7CaptureGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "data"
        self.root.mkdir()
        self.manifest = self.work / "manifest.json"
        self.manifest.write_text(json.dumps({"scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY",
                                             "datasets": [], "research": [], "experiments": []}))
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        self.addCleanup(lambda: setattr(bridge, "DATA_ROOT", self.old_root))

    def capture(self, **kwargs):
        return acquire_validate.capture(START * 1000, END * 1000,
                                        root=self.root, manifest_path=self.manifest,
                                        fetch=kwargs.pop("fetch", fixture),
                                        run_dns_probe=False, **kwargs)

    def test_independent_audit_validates_raw_not_just_report(self):
        result = self.capture()
        self.assertEqual(result["status"], "CAPTURED_AND_DUAL_AUDITED_NOT_PIT")
        self.assertEqual(result["attempt_kind"], "TEST_INJECTED_FETCH")
        self.assertFalse(result["historical_point_in_time_proven"])
        self.assertFalse(result["source_authenticity_independently_proven"])
        audit = audit_bundle_independently(Path(result["archive_directory"]))
        self.assertEqual(audit["markets"]["BTC-USD"]["rows"], 2)
        self.assertEqual(audit["markets"]["ETH-USD"]["rows"], 2)
        self.assertFalse(audit["executable_alpha_proven"])
        self.assertFalse(result["registered"])

    def test_auditors_reject_corrupt_original_page(self):
        result = self.capture()
        root = Path(result["archive_directory"])
        raw = root / "raw" / "btc_coinbase_spot_5m" / "0000.json"
        raw.write_bytes(raw.read_bytes() + b"evil")
        with self.assertRaises(IndependentAuditError):
            audit_bundle_independently(root)
        with self.assertRaises(ValueError):
            fetch_btc_eth.verify_bundle(root)

    def test_auditor_rejects_duplicate_candle_in_source(self):
        result = self.capture()
        root = Path(result["archive_directory"])
        raw = root / "raw" / "eth_coinbase_spot_5m" / "0000.json"
        doc = json.loads(raw.read_bytes())
        doc["candles"].append(doc["candles"][0])
        raw.write_text(json.dumps(doc))
        source = root / "eth_coinbase_spot_5m.source.json"
        meta = json.loads(source.read_bytes())
        meta["pages"][0]["raw_response_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        meta["pages"][0]["raw_response_bytes"] = len(raw.read_bytes())
        meta["pages"][0]["count"] += 1
        source.write_text(json.dumps(meta))
        with self.assertRaisesRegex(IndependentAuditError, "repeated candle"):
            audit_bundle_independently(root)

    def test_failed_second_product_writes_nothing(self):
        def fail_eth(product, a, b):
            if product == "ETH-USD":
                raise ValueError("simulated provider unavailable")
            return fixture(product, a, b)
        result = self.capture(fetch=fail_eth, repeat=True)
        self.assertEqual(result["status"], "CAPTURE_FAILED_UNVERIFIED")
        self.assertFalse(result["raw_market_bytes_saved"])
        self.assertFalse(result["registered"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_repeat_verifies_both_and_retains_second_archive(self):
        result = self.capture(repeat=True)
        self.assertTrue(result["same_provider_repeat_match"])
        self.assertEqual(result["status"], "CAPTURED_AND_DUAL_AUDITED_NOT_PIT")
        repeated = Path(result["repeat_archive_directory"])
        self.assertTrue(repeated.is_dir())
        self.assertTrue((repeated / "raw" / "eth_coinbase_spot_5m" / "0000.json").exists())
        self.assertEqual(audit_bundle_independently(repeated)["markets"]["BTC-USD"]["rows"], 2)

    def test_repeat_mismatch_blocks_registration(self):
        counts = {"BTC-USD": 0, "ETH-USD": 0}
        def mutate_on_second_fetch(product, a, b):
            counts[product] += 1
            raw, doc = fixture(product, a, b)
            if product == "ETH-USD" and counts[product] == 2:
                # Consistent, plausible but revised historical OHLCV.
                doc["candles"][0]["volume"] = "0.9"
                raw = json.dumps(doc).encode()
            return raw, doc
        result = self.capture(fetch=mutate_on_second_fetch, repeat=True, register=True)
        self.assertEqual(result["status"], "REPEAT_DIFFERED_NO_REGISTRATION")
        self.assertFalse(result["registered"])
        self.assertEqual(json.loads(self.manifest.read_text())["datasets"], [])

    def test_register_requires_repeat(self):
        with self.assertRaisesRegex(ValueError, "requires --repeat"):
            self.capture(register=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_repeat_and_two_auditors_permit_only_unverified_chart_registration(self):
        result = self.capture(repeat=True, register=True)
        self.assertEqual(result["status"], "REGISTERED_UNVERIFIED_HISTORICAL_SPOT_BARS")
        self.assertTrue(result["registered"])
        registered = bridge.read_manifest(self.manifest)["datasets"]
        self.assertEqual(len(registered), 2)
        self.assertEqual({x["evidence_status"] for x in registered}, {"unverified"})
        self.assertTrue(all(x["chart_tick_is_display_only"] for x in registered))
        self.assertFalse(result["execution_prices_proven"])

    def test_manifest_changed_during_collection_prevents_publish(self):
        seen = False
        def tamper_manifest(product, a, b):
            nonlocal seen
            if not seen:
                seen = True
                old = json.loads(self.manifest.read_text())
                old["audit_note"] = "a second process changed this"
                self.manifest.write_text(json.dumps(old))
            return fixture(product, a, b)
        result = self.capture(fetch=tamper_manifest, repeat=True, register=True)
        self.assertEqual(result["status"], "CAPTURE_FAILED_UNVERIFIED")
        self.assertIn("Manifest changed", result["error"])
        self.assertFalse(result["registered"])

    def test_independent_audit_checks_local_ingest_time_present(self):
        result = self.capture()
        directory = Path(result["archive_directory"])
        for asset in ("btc", "eth"):
            meta = json.loads((directory / f"{asset}_coinbase_spot_5m.source.json").read_text())
            self.assertTrue(meta["pages"][0]["local_ingest_clock_utc"])
            self.assertEqual(meta["point_in_time_provenance"], "NOT_ESTABLISHED")


if __name__ == "__main__":
    unittest.main()

class TestHTTPSResponseAdmission(unittest.TestCase):
    def test_redirected_source_refused(self):
        from unittest.mock import patch
        import fetch_coinbase_5m as coinbase
        class Redirect:
            status = 200
            headers = None
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self): return 'https://not-coinbase.invalid/data'
            def read(self, *_): return b'{"candles":[]}'
        with patch.object(coinbase, "urlopen", return_value=Redirect()) as mocked:
            with self.assertRaisesRegex(coinbase.SourceError, 'redirected'):
                coinbase.request_json('BTC-USD', START, END)
            mocked.assert_called_once()

    def test_non_json_response_refused(self):
        from unittest.mock import patch
        from email.message import Message
        import fetch_coinbase_5m as coinbase
        class NotJSON:
            status = 200
            headers = Message()
            headers['Content-Type'] = 'text/html'
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self):
                request = coinbase.API_ORIGIN + coinbase.API_ROUTE.format(product='ETH-USD')
                from urllib.parse import urlencode
                return request + '?' + urlencode({'start': START, 'end': END - 1,
                     'granularity': 'FIVE_MINUTE', 'limit': coinbase.PAGE_BARS})
            def read(self, *_): return b'<html></html>'
        with patch.object(coinbase, "urlopen", return_value=NotJSON()):
            with self.assertRaisesRegex(coinbase.SourceError, 'content type'):
                coinbase.request_json('ETH-USD', START, END)

class TestCapturedProviderBoundary(unittest.TestCase):
    """Real Coinbase HTTP response in v7/evidence, rejected before correction.

    It has exactly one extra candle starting at the right endpoint of the
    requested interval. Do not treat this right-boundary bar as part of the
    2026-11-08 00:00-24:00 UTC completed-bar cohort.
    """
    def test_real_provider_returned_end_inclusive_289_bars(self):
        import fetch_coinbase_5m as coinbase
        from unittest.mock import patch
        raw_path = ROOT / 'evidence' / 'btc_20261108_rejected_289_response.json'
        raw = raw_path.read_bytes()
        expected_sha = 'eb502acd1182d7be866156074e778d1c6df4201f18d1eaa1233a658ad5673003'
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_sha)
        doc = coinbase.strict_source_json(raw)
        self.assertEqual(len(doc['candles']), 289)
        stamps = [int(x['start']) for x in doc['candles']]
        self.assertEqual(len(set(stamps)), 289)
        self.assertEqual(min(stamps), START)
        self.assertEqual(max(stamps), START + 86400)
        self.assertEqual(sum(START <= t < START+86400 for t in stamps), 288)
        with self.assertRaisesRegex(coinbase.SourceError, 'out-of-range'):
            coinbase.parse_page(doc, start_sec=START, end_sec=START+86400)

    def test_request_uses_last_second_before_exclusive_end(self):
        import fetch_coinbase_5m as coinbase
        from unittest.mock import patch
        class FakeHTTP:
            status = 200
            headers = None
            def __init__(self, url): self.url = url
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self): return self.url
            def read(self, limit): return b'{"candles": []}'
        calls = []
        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            return FakeHTTP(req.full_url)
        with patch.object(coinbase, 'urlopen', side_effect=fake_open):
            coinbase.request_json('BTC-USD', START, START+86400)
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(calls[0]).query)
        self.assertEqual(int(q['end'][0]), START+86400-1)
        self.assertEqual(int(q['start'][0]), START)
        self.assertEqual(q['granularity'], ['FIVE_MINUTE'])
