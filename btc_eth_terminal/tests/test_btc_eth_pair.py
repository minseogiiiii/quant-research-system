"""Independent offline two-asset source, audit and registration failure tests."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import bridge
import fetch_btc_eth
import fetch_coinbase_5m

START = int(datetime(2026, 10, 27, 0, 0, tzinfo=UTC).timestamp())
END = START + 3 * 300


def exchange_fixture(product: str, a: int, b: int):
    base = "60000.50" if product == "BTC-USD" else "3200.50"
    high = "61000" if product == "BTC-USD" else "3300"
    low = "59000" if product == "BTC-USD" else "3100"
    candles = [{"start": str(t), "open": base, "high": high, "low": low,
                "close": base, "volume": "0.500"} for t in range(a, b, 300)]
    raw = json.dumps({"candles": candles}, separators=(",", ":")).encode("utf-8")
    return raw, json.loads(raw)


class TestBTCETHPair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.data = self.work / "data"
        self.data.mkdir()
        self.manifest = self.work / "manifest.json"
        self.manifest.write_text(json.dumps({"scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY", "datasets": [],
                                             "research": [], "experiments": []}))
        self.prev_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.data

    def tearDown(self):
        bridge.DATA_ROOT = self.prev_root
        self.temp.cleanup()

    def run_pair(self, *, fetch=exchange_fixture, register=False):
        return fetch_btc_eth.export_pair(START * 1000, END * 1000,
                                         data_root=self.data, manifest_path=self.manifest,
                                         fetch=fetch, register=register)

    def test_both_sources_reconstruct_from_archived_raw(self):
        final = self.run_pair()
        result = fetch_btc_eth.verify_bundle(final)
        self.assertTrue(result["verified"])
        self.assertEqual(set(result["markets"]), {"BTC-USD", "ETH-USD"})
        self.assertEqual(result["markets"]["BTC-USD"]["rows"], 3)
        self.assertEqual(result["markets"]["ETH-USD"]["rows"], 3)
        self.assertEqual(json.loads(self.manifest.read_text())["datasets"], [])
        for product in ("BTC", "ETH"):
            prefix = product.lower() + "_coinbase_spot_5m"
            self.assertTrue((final / f"{prefix}.csv").exists())
            source = json.loads((final / f"{prefix}.source.json").read_text())
            self.assertEqual(source["point_in_time_provenance"], "NOT_ESTABLISHED")
            self.assertIsNone(source["available_at"])
            self.assertEqual(len(source["pages"]), 1)

    def test_catalog_refuses_archive_tampering_after_registration(self):
        final = self.run_pair(register=True)
        current = bridge.catalog(bridge.read_manifest(self.manifest))
        self.assertEqual(len(current["datasets"]), 2)
        self.assertTrue(all(x["csv_sha256"] for x in current["datasets"]))
        raw = final / "raw" / "eth_coinbase_spot_5m" / "0000.json"
        raw.write_bytes(raw.read_bytes() + b"tamper")
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "SHA-256"):
            bridge.catalog(bridge.read_manifest(self.manifest))

    def test_independent_btc_eth_candle_values_stay_venue_correct(self):
        self.run_pair(register=True)
        doc = bridge.read_manifest(self.manifest)
        btc = bridge.get_bars(doc, "BTC_COINBASE_SPOT", "5", END * 1000, 0, 20)
        eth = bridge.get_bars(doc, "ETH_COINBASE_SPOT", "5", END * 1000, 0, 20)
        self.assertEqual(len(btc["bars"]), len(eth["bars"]))
        self.assertEqual([b["time"] for b in btc["bars"]], [b["time"] for b in eth["bars"]])
        self.assertEqual([b["open"] for b in btc["bars"]], [60000.50] * 3)
        self.assertEqual([b["open"] for b in eth["bars"]], [3200.50] * 3)
        self.assertNotEqual(btc["csv_sha256"], eth["csv_sha256"])
        with self.assertRaisesRegex(bridge.InputError, "available_at"):
            bridge.get_bars(doc, "BTC_COINBASE_SPOT", "5", END * 1000, 0, 20, asof=END*1000)

    def test_register_both_or_neither(self):
        final = self.run_pair(register=True)
        ds = bridge.read_manifest(self.manifest)["datasets"]
        self.assertEqual({x["symbol"] for x in ds}, {"BTC_COINBASE_SPOT", "ETH_COINBASE_SPOT"})
        self.assertEqual({x["resolution"] for x in ds}, {"5"})
        self.assertTrue(all(x["source_type"] == "real" and x["evidence_status"] == "unverified" for x in ds))
        for x in ds:
            self.assertEqual(len(bridge.parse_bars(x["file"])[0]), 3)
            self.assertTrue((self.data / x["provenance_file"]).is_file())
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "already exists"):
            self.run_pair(register=True)
        self.assertTrue(final.exists())

    def test_failed_eth_cannot_write_btc_or_change_catalog(self):
        def fail_eth(product, a, b):
            if product == "ETH-USD":
                return b'{"error":"rate_limited"}', {"error": "rate_limited"}
            return exchange_fixture(product, a, b)
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "candles"):
            self.run_pair(fetch=fail_eth, register=True)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_corrupt_source_payload_detected_even_if_csv_unchanged(self):
        final = self.run_pair()
        raw_file = final / "raw" / "btc_coinbase_spot_5m" / "0000.json"
        raw_file.write_bytes(raw_file.read_bytes() + b' ')
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "SHA-256"):
            fetch_btc_eth.verify_bundle(final)

    def test_corrupt_derived_csv_detected(self):
        final = self.run_pair()
        csv_file = final / "eth_coinbase_spot_5m.csv"
        csv_file.write_bytes(csv_file.read_bytes().replace(b'3200.50', b'3200.51'))
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "CSV does not match"):
            fetch_btc_eth.verify_bundle(final)

    def test_raw_page_document_mismatch_is_rejected(self):
        def inconsistent(product, a, b):
            raw, doc = exchange_fixture(product, a, b)
            doc["candles"][0]["close"] = "1234"
            return raw, doc
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "disagree"):
            self.run_pair(fetch=inconsistent)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_input_must_be_aligned_no_output_written(self):
        with self.assertRaisesRegex(fetch_coinbase_5m.SourceError, "aligned"):
            fetch_btc_eth.export_pair(START * 1000 + 1, END * 1000, data_root=self.data,
                                       manifest_path=self.manifest, fetch=exchange_fixture)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_raw_bundle_digest_is_in_actual_derived_hash(self):
        final = self.run_pair()
        for product in ("btc", "eth"):
            stem = product + "_coinbase_spot_5m"
            raw_csv = (final / f"{stem}.csv").read_bytes()
            metadata = json.loads((final / f"{stem}.source.json").read_bytes())
            self.assertEqual(metadata["derived_csv_sha256"], hashlib.sha256(raw_csv).hexdigest())
            self.assertEqual(metadata["raw_payload_bytes_released"], True)


if __name__ == "__main__":
    unittest.main()


class TestScopeGates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        bridge.DATA_ROOT = self.previous
        self.temp.cleanup()

    def read(self, datasets, research=None, experiments=None):
        self.manifest.write_text(json.dumps({
            "scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY",
            "datasets": datasets,
            "research": research if research is not None else [],
            "experiments": experiments if experiments is not None else [],
        }))
        return bridge.read_manifest(self.manifest)

    def test_rejects_unrelated_market_before_accessing_files(self):
        with self.assertRaisesRegex(bridge.InputError, "BTC/ETH pilot"):
            self.read([{"symbol": "SOLUSDT", "type": "crypto", "resolution": "5"}])
        with self.assertRaisesRegex(bridge.InputError, "BTC/ETH pilot"):
            self.read([{"symbol": "QQQ", "type": "etf", "resolution": "1"}])

    def test_rejects_unrelated_research_and_experiments(self):
        with self.assertRaisesRegex(bridge.InputError, "unrelated research"):
            self.read([], research=[{"id": "PUMPFUN-SODA", "market": "Solana/Pump.fun"}])
        with self.assertRaisesRegex(bridge.InputError, "unrelated frozen"):
            self.read([], experiments=[{"id": "SPY"}])
