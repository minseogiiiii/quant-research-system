from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import fetch_coinbase_5m as src

START = int(datetime(2026, 10, 28, 0, 0, tzinfo=UTC).timestamp())
END = START + 3 * 300


def fixture(product: str, start: int, end: int):
    rows = [
        {
            "start": str(t),
            "open": "100.00",
            "high": "101.00",
            "low": "99.00",
            "close": "100.50",
            "volume": "1.2",
        }
        for t in range(start, end, 300)
    ]
    # Coinbase response usually arrives newest first; reverse to exercise sorting.
    raw = json.dumps({"candles": list(reversed(rows))}, separators=(",", ":")).encode()
    return raw, json.loads(raw)


class CoinbaseDataSourceTests(unittest.TestCase):
    def test_complete_bars_and_time_semantics(self):
        bars, meta = src.collect("BTC-USD", START * 1000, END * 1000, fixture)
        self.assertEqual(
            [int(x["time"]) for x in bars], [t * 1000 for t in range(START, END, 300)]
        )
        self.assertIsNone(meta["available_at"])
        self.assertEqual(meta["point_in_time_provenance"], "NOT_ESTABLISHED")
        self.assertEqual(meta["rows"], 3)
        self.assertEqual(
            meta["pages"][0]["raw_response_sha256"],
            hashlib.sha256(fixture("BTC-USD", START, END)[0]).hexdigest(),
        )

    def test_missing_bar_fails_closed(self):
        def missing(p, a, b):
            raw, obj = fixture(p, a, b)
            obj["candles"].pop()
            return json.dumps(obj).encode(), obj

        with self.assertRaisesRegex(src.SourceError, "Missing 1"):
            src.collect("BTC-USD", START * 1000, END * 1000, missing)

    def test_price_bounds_fail(self):
        for change in (
            {"high": "99.5"},
            {"volume": "-1"},
            {"open": "NaN"},
            {"close": "0"},
        ):
            with self.subTest(change=change):

                def bad(p, a, b):
                    raw, obj = fixture(p, a, b)
                    obj["candles"][0].update(change)
                    return json.dumps(obj).encode(), obj

                with self.assertRaises(src.SourceError):
                    src.collect("BTC-USD", START * 1000, END * 1000, bad)

    def test_invalid_api_failure_fails(self):
        with self.assertRaisesRegex(src.SourceError, "candles"):
            src.collect(
                "BTC-USD",
                START * 1000,
                END * 1000,
                lambda *_: (b'{"error":"rate_limit"}', {"error": "rate_limit"}),
            )

    def test_unsupported_instrument_and_unaligned_interval(self):
        with self.assertRaises(src.SourceError):
            src.collect("BTC-USDT", START * 1000, END * 1000, fixture)
        with self.assertRaises(src.SourceError):
            src.collect("BTC-USD", START * 1000 + 1000, END * 1000, fixture)

    def test_safely_writes_provenance_and_does_not_invent_asof(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            p, meta = src.export("ETH-USD", START * 1000, END * 1000, out, fixture)
            self.assertTrue(p.exists())
            self.assertTrue(meta.exists())
            self.assertTrue(p.name.startswith("eth_coinbase_spot"))
            obj = json.loads(meta.read_text())
            self.assertEqual(
                obj["derived_csv_sha256"], hashlib.sha256(p.read_bytes()).hexdigest()
            )
            self.assertIsNone(obj["available_at"])
            self.assertNotIn("available_at", p.read_text())
            with self.assertRaisesRegex(src.SourceError, "Output exists"):
                src.export("ETH-USD", START * 1000, END * 1000, out, fixture)

    def test_register_only_with_bridge_data_root(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(src.SourceError, "backend/data"):
                src.export(
                    "BTC-USD",
                    START * 1000,
                    END * 1000,
                    Path(td),
                    fixture,
                    register=True,
                )

    def test_multi_page_count_and_detects_duplicates(self):
        s = START
        end = s + 305 * 300
        bars, meta = src.collect("BTC-USD", s * 1000, end * 1000, fixture)
        self.assertEqual(len(bars), 305)
        self.assertEqual(len(meta["pages"]), 2)

        def dup(p, a, b):
            raw, doc = fixture(p, a, b)
            doc["candles"].append({**doc["candles"][0], "close": "100.80"})
            return json.dumps(doc).encode(), doc

        with self.assertRaisesRegex(src.SourceError, "Conflicting duplicate"):
            src.collect("BTC-USD", START * 1000, END * 1000, dup)


if __name__ == "__main__":
    unittest.main()
