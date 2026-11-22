"""v6 adversarial provenance tests: ambiguous JSON, paired horizons, source claims.

All market prices are offline fake fixture inputs, not observed BTC/ETH data.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import bridge
import fetch_btc_eth as pair
import fetch_coinbase_5m as coinbase
from test_btc_eth_pair import END, START, exchange_fixture


class SourceProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.prior = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.data

    def tearDown(self):
        bridge.DATA_ROOT = self.prior
        self.tmp.cleanup()

    def capture(self):
        return pair.export_pair(
            START * 1000, END * 1000, data_root=self.data, fetch=exchange_fixture
        )

    def test_rejects_duplicate_response_keys_before_any_disk_write(self):
        def invalid(product, a, b):
            raw, doc = exchange_fixture(product, a, b)
            return b'{"candles":[],"candles":' + raw[len(b'{"candles":') :], doc

        with self.assertRaisesRegex(coinbase.SourceError, "duplicate JSON key"):
            pair.export_pair(
                START * 1000, END * 1000, data_root=self.data, fetch=invalid
            )
        self.assertEqual(list(self.data.iterdir()), [])

    def test_rejects_nan_and_infinity_in_raw_payload(self):
        for illegal in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(illegal=illegal), self.assertRaisesRegex(
                coinbase.SourceError, "Invalid JSON constant"
            ):
                coinbase.strict_source_json(b'{"candles":' + illegal + b"}")

    def test_rejects_duplicate_archive_metadata_keys(self):
        archive = self.capture()
        path = archive / "btc_coinbase_spot_5m.source.json"
        payload = path.read_text()
        self.assertIn('"product": "BTC-USD"', payload)
        path.write_text(
            payload.replace(
                '"product": "BTC-USD"', '"product": "BTC-USD", "product": "BTC-USD"'
            )
        )
        with self.assertRaisesRegex(coinbase.SourceError, "duplicate JSON key"):
            pair.verify_bundle(archive)

    def test_rejects_provenance_identity_rewrite(self):
        archive = self.capture()
        path = archive / "btc_coinbase_spot_5m.source.json"
        payload = json.loads(path.read_text())
        payload["source"] = "Some other data vendor"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(coinbase.SourceError, "identity/provenance"):
            pair.verify_bundle(archive)

    def test_rejects_retrieved_at_before_end(self):
        archive = self.capture()
        path = archive / "eth_coinbase_spot_5m.source.json"
        payload = json.loads(path.read_text())
        payload["retrieved_at_utc"] = datetime.fromtimestamp(START, UTC).isoformat()
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(coinbase.SourceError, "retrieval timestamp"):
            pair.verify_bundle(archive)

    def test_rejects_retrieval_without_timezone(self):
        archive = self.capture()
        path = archive / "eth_coinbase_spot_5m.source.json"
        payload = json.loads(path.read_text())
        payload["retrieved_at_utc"] = "2026-11-13T12:00:00"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(coinbase.SourceError, "retrieval timestamp"):
            pair.verify_bundle(archive)

    def test_rejects_future_retrieval(self):
        archive = self.capture()
        path = archive / "eth_coinbase_spot_5m.source.json"
        payload = json.loads(path.read_text())
        payload["retrieved_at_utc"] = (
            datetime.now(UTC) + timedelta(days=2)
        ).isoformat()
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(coinbase.SourceError, "retrieval timestamp"):
            pair.verify_bundle(archive)

    def test_independent_archives_with_shifted_cohort_cannot_fake_pair(self):
        """Each ETH record is internally self-consistent, but BTC/ETH ranges differ."""
        archive = self.capture()
        stem = "eth_coinbase_spot_5m"
        meta_path = archive / f"{stem}.source.json"
        csv_path = archive / f"{stem}.csv"
        raw_path = archive / "raw" / stem / "0000.json"
        metadata = json.loads(meta_path.read_text())
        delta_sec = 300
        metadata["start_inclusive_utc"] = datetime.fromtimestamp(
            START + delta_sec, UTC
        ).isoformat()
        metadata["end_exclusive_utc"] = datetime.fromtimestamp(
            END + delta_sec, UTC
        ).isoformat()
        metadata["pages"][0]["from_sec"] += delta_sec
        metadata["pages"][0]["to_sec"] += delta_sec
        if "http_request_end_sec_inclusive" in metadata["pages"][0]:
            metadata["pages"][0]["http_request_end_sec_inclusive"] += delta_sec
        raw_doc = json.loads(raw_path.read_bytes())
        for bar in raw_doc["candles"]:
            bar["start"] = str(int(bar["start"]) + delta_sec)
        raw = json.dumps(raw_doc, separators=(",", ":")).encode()
        raw_path.write_bytes(raw)
        metadata["pages"][0]["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
        metadata["pages"][0]["raw_response_bytes"] = len(raw)
        revised = []
        for row in csv_path.read_text().splitlines():
            if row.startswith("time,"):
                revised.append(row)
            else:
                timestamp, rest = row.split(",", 1)
                revised.append(str(int(timestamp) + delta_sec * 1000) + "," + rest)
        csv_raw = ("\n".join(revised) + "\n").encode()
        csv_path.write_bytes(csv_raw)
        metadata["derived_csv_sha256"] = hashlib.sha256(csv_raw).hexdigest()
        meta_path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(coinbase.SourceError, "mismatched time ranges"):
            pair.verify_bundle(archive)


if __name__ == "__main__":
    unittest.main()


class RegistrationSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.data
        self.manifest = self.data / "manifest.json"
        self.initial = {
            "scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY",
            "datasets": [],
            "research": [],
            "experiments": [],
        }
        self.manifest.write_text(json.dumps(self.initial))

    def tearDown(self):
        bridge.DATA_ROOT = self.old_root
        self.tmp.cleanup()

    def test_single_market_unarchived_http_pages_cannot_register(self):
        old = self.manifest.read_bytes()
        with self.assertRaisesRegex(
            coinbase.SourceError, "Single-product registration is disabled"
        ):
            coinbase.export(
                "BTC-USD",
                START * 1000,
                END * 1000,
                self.data,
                fetch=exchange_fixture,
                register=True,
                manifest_path=self.manifest,
            )
        self.assertEqual(self.manifest.read_bytes(), old)
        self.assertFalse((self.data / "btc_coinbase_spot_5m.csv").exists())

    def test_manual_coinbase_manifest_cannot_skip_raw_archive_verification(self):
        path = self.data / "btc_5m.csv"
        path.write_text(f"time,open,high,low,close\n{START * 1000},100,101,99,100\n")
        bad = {
            "symbol": "BTC_COINBASE_SPOT",
            "name": "BTC",
            "exchange": "Coinbase Advanced Trade",
            "timezone": "UTC",
            "session": "24x7",
            "type": "crypto",
            "resolution": "5",
            "file": path.name,
            "source_type": "real",
            "evidence_status": "unverified",
            "pricePrecision": 2,
            "tickSize": 0.01,
        }
        forged = {**self.initial, "datasets": [bad]}
        self.manifest.write_text(json.dumps(forged))
        with self.assertRaisesRegex(bridge.InputError, "paired raw-page provenance"):
            bridge.read_manifest(self.manifest)

    def test_manifest_change_during_collection_does_not_discard_prior_edits(self):
        original_hash = self.manifest.read_bytes()
        touched = False

        def competing_writer(product, a, b):
            nonlocal touched
            if not touched:
                modified = {
                    **self.initial,
                    "research": [
                        {"id": "UNCHANGED", "market": "BTC/ETH", "status": "NO_ALPHA"}
                    ],
                }
                self.manifest.write_text(json.dumps(modified))
                touched = True
            return exchange_fixture(product, a, b)

        with self.assertRaisesRegex(coinbase.SourceError, "Manifest changed"):
            pair.export_pair(
                START * 1000,
                END * 1000,
                data_root=self.data,
                manifest_path=self.manifest,
                fetch=competing_writer,
                register=True,
            )
        self.assertNotEqual(self.manifest.read_bytes(), original_hash)
        self.assertEqual(
            bridge.read_manifest(self.manifest)["research"][0]["id"], "UNCHANGED"
        )
        self.assertFalse(bridge.read_manifest(self.manifest)["datasets"])


class ContractStrictness(unittest.TestCase):
    def test_manifest_rejects_duplicate_keys_and_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "manifest.json"
            path.write_text('{"datasets":[],"datasets":[],"research":[]}')
            with self.assertRaisesRegex(bridge.InputError, "duplicate key"):
                bridge.read_manifest(path)
            path.write_text('{"datasets":[],"research":[],"unexpected":NaN}')
            with self.assertRaisesRegex(bridge.InputError, "nonfinite JSON constant"):
                bridge.read_manifest(path)

    def test_http_rejects_ambiguous_optional_filters_and_unknown_keys(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.error import HTTPError
        from urllib.request import urlopen

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text(
                json.dumps(
                    {
                        "scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY",
                        "datasets": [],
                        "research": [],
                        "experiments": [],
                    }
                )
            )
            old_manifest = bridge.MANIFEST
            bridge.MANIFEST = p
            server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                origin = f"http://127.0.0.1:{server.server_address[1]}"
                for suffix in (
                    "/api/catalog?surprise=1",
                    "/api/bars?symbol=BTCUSDT&resolution=5&to=1000&from=1&from=0",
                    "/api/health?asof=123",
                ):
                    with self.subTest(suffix=suffix):
                        with self.assertRaises(HTTPError) as error:
                            urlopen(origin + suffix)
                        self.assertEqual(error.exception.code, 400)
                        self.assertIn(
                            "query parameter", error.exception.read().decode()
                        )
                with urlopen(origin + "/api/health") as response:
                    self.assertTrue(json.load(response)["ok"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
                bridge.MANIFEST = old_manifest
