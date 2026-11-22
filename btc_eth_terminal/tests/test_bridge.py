"""Offline functional tests for the integration bridge, NOT upstream Funnest tests."""

import json
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import bridge

BASE = {
    "symbol": "QQQ",
    "name": "QQQ research sample",
    "exchange": "RESEARCH",
    "timezone": "America/New_York",
    "session": "0930-1600",
    "type": "etf",
    "resolution": "1",
    "file": "qqq_1m.csv",
    "source_type": "synthetic",
    "evidence_status": "unverified",
    "pricePrecision": 2,
    "tickSize": 0.01,
}
ROWS = """time,open,high,low,close,volume
2026-11-02T14:30:00Z,100,101,99,100.5,300
2026-11-02T14:31:00Z,100.5,101,100,100.7,400
2026-11-02T14:32:00Z,100.7,102,100.2,101.8,250
"""
FILLS = """time,price,side,quantity,event_type
2026-11-02T14:31:00Z,100.7,buy,2,paper_fill
"""


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "qqq_1m.csv").write_text(ROWS)
        self.original_data_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.data.resolve()
        self.manifest = self.root / "manifest.json"
        self._set_manifest([BASE])

    def tearDown(self):
        bridge.DATA_ROOT = self.original_data_root
        self.tmp.cleanup()

    def _set_manifest(self, datasets):
        self.manifest.write_text(json.dumps({"datasets": datasets, "research": []}))

    def test_manifest_and_sha256(self):
        obj = bridge.read_manifest(self.manifest)
        meta = bridge.catalog(obj)["datasets"][0]
        self.assertEqual(len(meta["csv_sha256"]), 64)
        self.assertTrue(meta["read_only"])
        self.assertEqual(meta["source_type"], "synthetic")

    def test_historical_data_roundtrip_and_countback(self):
        obj = bridge.read_manifest(self.manifest)
        out = bridge.get_bars(obj, "QQQ", "1", 1793630000000, 0, 2)
        self.assertEqual(len(out["bars"]), 2)
        self.assertEqual(out["bars"][0]["close"], 100.7)
        self.assertFalse(out["noMoreData"])
        all_data = bridge.get_bars(obj, "QQQ", "1", 1793630000000, 0, 10)
        self.assertTrue(all_data["noMoreData"])
        self.assertEqual(len(all_data["bars"]), 3)

    def test_resolution_mismatch_is_error(self):
        with self.assertRaises(bridge.InputError):
            bridge.get_bars(
                bridge.read_manifest(self.manifest), "QQQ", "5", 1793630000000, 0, 100
            )

    def test_timestamp_requires_offset_or_milliseconds(self):
        self.assertEqual(bridge._time_ms("2026-11-02T14:30:00Z"), 1793629800000)
        with self.assertRaises(bridge.InputError):
            bridge._time_ms("2026-11-02T14:30:00")
        with self.assertRaises(bridge.InputError):
            bridge._time_ms("1793629800")

    def test_duplicate_or_out_of_order_timestamps_rejected(self):
        bad = ROWS + "2026-11-02T14:30:00Z,100,100,100,100,0\n"
        (self.data / "qqq_1m.csv").write_text(bad)
        with self.assertRaisesRegex(bridge.InputError, "strictly increasing"):
            bridge.parse_bars("qqq_1m.csv")

    def test_bad_ohlc_rejected(self):
        (self.data / "qqq_1m.csv").write_text(
            ROWS.replace("100,101,99,100.5,300", "100,101,100.8,100.5,300")
        )
        with self.assertRaisesRegex(bridge.InputError, "OHLC"):
            bridge.parse_bars("qqq_1m.csv")

    def test_nonfinite_and_negative_volume_rejected(self):
        (self.data / "qqq_1m.csv").write_text(ROWS.replace("100.5,300", "NaN,300"))
        with self.assertRaisesRegex(bridge.InputError, "Nonfinite"):
            bridge.parse_bars("qqq_1m.csv")
        (self.data / "qqq_1m.csv").write_text(ROWS.replace("100.5,300", "100.5,-300"))
        with self.assertRaisesRegex(bridge.InputError, "negative volume"):
            bridge.parse_bars("qqq_1m.csv")

    def test_file_traversal_rejected(self):
        with self.assertRaises(bridge.InputError):
            bridge.safe_path("../manifest.json")

    def test_live_trade_not_inferred_from_candidate_signal(self):
        (self.data / "fills.csv").write_text(FILLS)
        d = {**BASE, "fills_file": "fills.csv"}
        self._set_manifest([d])
        self.assertEqual(
            len(bridge.get_fills(bridge.read_manifest(self.manifest), "QQQ")), 1
        )
        (self.data / "fills.csv").write_text(
            FILLS.replace("paper_fill", "candidate_signal")
        )
        with self.assertRaisesRegex(bridge.InputError, "not signals"):
            bridge.get_fills(bridge.read_manifest(self.manifest), "QQQ")

    def test_audited_real_data_needs_raw_source_hash(self):
        self._set_manifest(
            [{**BASE, "source_type": "real", "evidence_status": "source_audited"}]
        )
        with self.assertRaisesRegex(bridge.InputError, "original_raw_sha256"):
            bridge.read_manifest(self.manifest)

    def test_prior_ledger_is_read_only_and_surface_as_of(self):
        original = {**BASE}
        ledger = {
            "as_of": "2026-11-04",
            "validated_executable_alpha": 0,
            "main_hypothesis": {"status": "NOT EMPIRICALLY TESTED"},
            "next_gate": "original MBO/SIP source audit",
        }
        (self.data / "qqq_alpha_ledger.json").write_text(json.dumps(ledger))
        self._set_manifest([original])
        doc = bridge.read_manifest(self.manifest)
        doc["research"] = [
            {
                "id": "QQQ-H1",
                "ledger_file": "qqq_alpha_ledger.json",
                "status": "DO NOT TRUST STATIC",
            }
        ]
        record = bridge.catalog(doc)["research"][0]
        self.assertEqual(record["status"], "NOT EMPIRICALLY TESTED")
        self.assertEqual(record["ledger_as_of"], "2026-11-04")
        self.assertEqual(record["next_gate"], "original MBO/SIP source audit")
        self.assertFalse(record["validated_executable_alpha"])
        self.assertEqual(len(record["ledger_sha256"]), 64)

    def test_end_to_end_http_catalog(self):
        old_manifest = bridge.MANIFEST
        bridge.MANIFEST = self.manifest
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        th = Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            addr = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(addr + "/api/catalog") as res:
                document = json.load(res)
            self.assertEqual(document["datasets"][0]["symbol"], "QQQ")
            self.assertFalse(document["trading_enabled"])
            with urlopen(
                addr + "/api/bars?symbol=QQQ&resolution=1&to=1793630000000&countBack=2"
            ) as res:
                bars = json.load(res)["bars"]
            self.assertEqual(len(bars), 2)
        finally:
            server.shutdown()
            server.server_close()
            th.join()
            bridge.MANIFEST = old_manifest


if __name__ == "__main__":
    unittest.main(verbosity=2)
