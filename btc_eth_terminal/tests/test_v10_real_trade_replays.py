"""Replay extra Coinbase 120s real source; no live fill/alpha inference."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from audit_l2_deep import StrictL2Error, deep_replay
from audit_l2_pilot import L2AuditError, audit_session

SOURCE = (
    Path(__file__).resolve().parents[1] / "backend" / "data" / "l2_pilot_120s_20261113"
)


class RealTradeReplayTests(unittest.TestCase):
    def test_real_source_accepts_only_exact_update_replays_when_explicitly_enabled(
        self,
    ):
        if not SOURCE.is_dir():
            self.fail("Required 120-second real source evidence not bundled")
        a = deep_replay(SOURCE, allow_exact_trade_replays=True)
        b = audit_session(SOURCE, allow_exact_trade_replays=True)
        self.assertEqual(a["total_messages"], 4451)
        self.assertEqual(b["received_message_count"], 4451)
        self.assertEqual(a["input_stream_sha256"], b["source_text_stream_sha256"])
        self.assertEqual(a["identical_trade_replays_excluded"], {"BTC-USD": 5})
        self.assertEqual(b["identical_trade_replays_excluded"], {"BTC-USD": 5})
        self.assertEqual(
            a["per_product"]["BTC-USD"]["source_reported_trades_total"], 1283
        )
        self.assertEqual(
            b["per_product"]["BTC-USD"]["individual_trades_reported"], 1283
        )
        self.assertEqual(
            a["per_product"]["ETH-USD"]["source_reported_trades_total"], 273
        )
        self.assertEqual(b["per_product"]["ETH-USD"]["individual_trades_reported"], 273)
        self.assertFalse(a["independent_exchange_reference_book_verified"])

    def test_default_legacy_policy_remains_strict(self):
        with self.assertRaises(StrictL2Error):
            deep_replay(SOURCE)
        with self.assertRaises(L2AuditError):
            audit_session(SOURCE)

    def test_conflicting_same_id_is_rejected_even_when_stream_rehashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            shutil.copyfile(
                SOURCE / "capture_status.json", dest / "capture_status.json"
            )
            # Change only one of the 5 duplicated BTC trade IDs' economic fields.
            # Recompute both the message SHA and full source stream SHA to show
            # the semantic audit, not the checksum-only gate, catches the fault.
            seen = set()
            lines = []
            modified = False
            total_hash = hashlib.sha256()
            for line in (
                (SOURCE / "raw_websocket_payloads.jsonl")
                .read_bytes()
                .splitlines(keepends=True)
            ):
                row = json.loads(line)
                market = json.loads(row["raw_text"])
                if market.get("channel") == "market_trades":
                    for event in market["events"]:
                        for trade in event["trades"]:
                            if (trade.get("product_id"), trade.get("trade_id")) == (
                                "BTC-USD",
                                "1122208129",
                            ):
                                if ("BTC-USD", "1122208129") in seen and not modified:
                                    trade["price"] = str(float(trade["price"]) + 1)
                                    modified = True
                                seen.add(("BTC-USD", "1122208129"))
                if modified and market.get("sequence_num") == 3697:
                    row["raw_text"] = json.dumps(market, separators=(",", ":"))
                    row["sha256"] = hashlib.sha256(row["raw_text"].encode()).hexdigest()
                raw_text = row["raw_text"].encode()
                total_hash.update(raw_text + b"\n")
                lines.append(json.dumps(row, separators=(",", ":")) + "\n")
            self.assertTrue(modified)
            (dest / "raw_websocket_payloads.jsonl").write_text("".join(lines))
            status = json.loads((dest / "capture_status.json").read_text())
            status["stream_hash"] = total_hash.hexdigest()
            (dest / "capture_status.json").write_text(json.dumps(status))
            with self.assertRaises(StrictL2Error):
                deep_replay(dest, allow_exact_trade_replays=True)
            with self.assertRaises(L2AuditError):
                audit_session(dest, allow_exact_trade_replays=True)


if __name__ == "__main__":
    unittest.main()
