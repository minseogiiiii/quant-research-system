"""Synthetic cross-input comparison and actual observer re-admission."""
import tempfile
import time
import unittest
from pathlib import Path

from q292_resilient_gateway import ManagedSource, ResilientRoute, compare
from q292_stream_gateway import Publisher, TransitionLedger, audit_ledger


def observation(digest, qty):
    depth = {side: [{"price": str(i + 1), "quantity": qty}
                    for i in range(10)] for side in ("bids", "asks")}
    row = {"source_raw_sha256": digest, "depth": depth}
    return None, {p: row for p in ("BTC-USD", "ETH-USD")}


class ResilienceContracts(unittest.TestCase):
    def test_exact_input_comparison_only(self):
        a = observation("a" * 64, "1")
        self.assertEqual(set(compare(a, observation("a" * 64, "1.0")).values()),
                         {"MATCH"})
        self.assertEqual(set(compare(a, observation("a" * 64, "2")).values()),
                         {"DIVERGENCE"})
        self.assertEqual(set(compare(a, observation("b" * 64, "2")).values()),
                         {"NOT_COMPARABLE"})
        self.assertEqual(set(compare(a, None).values()), {"SOURCE_UNAVAILABLE"})

    def test_actual_observer_restart_and_sticky_failover(self):
        with tempfile.TemporaryDirectory(prefix="q292g_") as directory:
            root = Path(directory)
            p = ManagedSource("primary", root / "p", 45, True)
            s = ManagedSource("secondary", root / "s", 45, True)
            route = ResilientRoute(p, s)
            ledger = TransitionLedger(root / "route.jsonl")
            publisher = Publisher(route, ledger)
            publisher.start()

            def until(predicate):
                end = time.monotonic() + 18
                while time.monotonic() < end:
                    body = publisher.read()
                    if predicate(body):
                        return body
                    time.sleep(.1)
                self.fail("RESTART_OR_FAILOVER_DEADLINE")

            try:
                first = until(lambda x: x["active_source"] == "primary")
                p.worker.process.kill()
                second = until(lambda x: x["active_source"] == "secondary")
                self.assertNotEqual(first["source_session"], second["source_session"])
                until(lambda x: x.get("source_attempts", {}).get("primary", 0) >= 2
                      and x.get("checkpoint_comparison", {}).get("BTC-USD")
                      in ("MATCH", "NOT_COMPARABLE"))
                self.assertEqual(publisher.read()["active_source"], "secondary")
                s.worker.process.kill()
                third = until(lambda x: x["active_source"] == "primary")
                self.assertNotEqual(first["source_session"], third["source_session"])
                self.assertGreater(third["generation"], second["generation"])
                self.assertFalse(third["zero_gap_certified"])
                self.assertFalse(third["historical_gap_recovered"])
                self.assertTrue((p.root / "attempt_01").is_dir())
                self.assertTrue((p.root / "attempt_02").is_dir())
                self.assertGreaterEqual(audit_ledger(root / "route.jsonl")["records"], 3)
            finally:
                publisher.close()
                p.close()
                s.close()
                ledger.close()


if __name__ == "__main__":
    unittest.main()
