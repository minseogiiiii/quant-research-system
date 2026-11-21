"""Fail-closed 5-minute completed-candle availability regression tests."""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import bridge
import export_ohlcv

T = 1793630100000
FIVE_MIN = 300_000


class TestCompletedBarAvailability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        previous = (bridge.DATA_ROOT, export_ohlcv.DATA_ROOT)
        bridge.DATA_ROOT = self.data
        export_ohlcv.DATA_ROOT = self.data
        self.addCleanup(self.restore, previous)

    @staticmethod
    def restore(previous):
        bridge.DATA_ROOT, export_ohlcv.DATA_ROOT = previous

    def manifest(self, relative: str) -> dict:
        payload = {
            "scope": "BTC_ETH_SPOT_5M_RESEARCH_ONLY", "datasets": [{
                "symbol": "BTC_TEST_SPOT", "name": "Synthetic test only",
                "exchange": "TEST", "timezone": "UTC", "session": "24x7",
                "type": "crypto", "resolution": "5", "file": relative,
                "source_type": "synthetic", "evidence_status": "unverified",
                "pricePrecision": 2, "tickSize": 0.01,
            }], "research": [], "experiments": [],
        }
        location = self.root / "manifest.json"
        location.write_text(json.dumps(payload))
        return bridge.read_manifest(location)

    def test_direct_manifest_cannot_replay_early_complete_bar(self):
        (self.data / "btc.csv").write_text(
            "time,open,high,low,close,volume,available_at\n"
            f"{T},100,101,99,100,7,{T + 1}\n"
        )
        doc = self.manifest("btc.csv")
        with self.assertRaisesRegex(bridge.InputError, "precedes the bar close"):
            bridge.catalog(doc)
        with self.assertRaisesRegex(bridge.InputError, "precedes the bar close"):
            bridge.get_bars(doc, "BTC_TEST_SPOT", "5", T, 0, 10, asof=T + FIVE_MIN)

    def test_exact_close_allowed_but_unknown_receipt_not_inferred(self):
        (self.data / "btc.csv").write_text(
            "time,open,high,low,close,volume,available_at\n"
            f"{T},100,101,99,100,7,{T + FIVE_MIN}\n"
        )
        doc = self.manifest("btc.csv")
        self.assertEqual(len(bridge.catalog(doc)["datasets"]), 1)
        self.assertEqual(
            bridge.get_bars(doc, "BTC_TEST_SPOT", "5", T, 0, 10, asof=T + FIVE_MIN)["n_history_available"],
            1,
        )
        (self.data / "btc.csv").write_text(
            "time,open,high,low,close,volume\n" f"{T},100,101,99,100,7\n"
        )
        with self.assertRaisesRegex(bridge.InputError, "requires explicit available_at"):
            bridge.get_bars(doc, "BTC_TEST_SPOT", "5", T, 0, 10, asof=T + FIVE_MIN)

    def test_export_rejects_early_availability_without_partial_output(self):
        src = self.root / "btc_features_5m.csv.gz"
        with gzip.open(src, "wt", encoding="utf-8") as fp:
            fp.write("time,open,high,low,close,volume,observed\n")
            fp.write(f"{T},100,101,99,100,7,{T + 1}\n")
        dst = self.data / "btc_exported.csv"
        mapping = dict(time="time", open="open", high="high", low="low", close="close",
                       volume="volume", available_at="observed")
        with self.assertRaisesRegex(bridge.InputError, "precedes the bar close"):
            export_ohlcv.convert(src=src, dst=dst, columns=mapping)
        self.assertFalse(dst.exists())

    def test_export_exact_close_and_absent_availability(self):
        src = self.root / "btc_features_5m.csv.gz"
        with gzip.open(src, "wt", encoding="utf-8") as fp:
            fp.write("time,open,high,low,close,volume,observed\n")
            fp.write(f"{T},100,101,99,100,7,{T + FIVE_MIN}\n")
        dst = self.data / "btc_exported.csv"
        mapping = dict(time="time", open="open", high="high", low="low", close="close",
                       volume="volume", available_at="observed")
        result = export_ohlcv.convert(src=src, dst=dst, columns=mapping)
        self.assertTrue(result["asof_supported"])
        self.assertEqual(result["rows"], 1)
        # Missing availability can be visualized but cannot assert PIT replay.
        dst2 = self.data / "btc_unknown.csv"
        mapping.pop("available_at")
        result2 = export_ohlcv.convert(src=src, dst=dst2, columns=mapping)
        self.assertFalse(result2["asof_supported"])
        self.assertNotIn("available_at", dst2.read_text())
