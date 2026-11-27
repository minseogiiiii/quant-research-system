import unittest
from f3_candle_bundle import candle_bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(
            product="BTC-USD",
            mode="LIVE_SOURCE",
            run_id="r",
            session_id="s",
            connection_id=1,
            state_version=4,
            source_sequence=8,
            source_raw_sha256="a" * 64,
            receiver_time_ms=180000,
        )
        self.candle = dict(
            time=60000,
            available_at=120000,
            open="100",
            high="101",
            low="99",
            close="100",
            volume="2",
            observed_trade_count=2,
        )

    def test_bound_copy(self):
        b = candle_bundle(self.row, [self.candle])
        self.candle["volume"] = "9"
        self.assertEqual(b["rows"][0]["volume"], "2")
        self.assertEqual(b["state_version"], 4)
        self.assertEqual(b["session_id"], "s")
        self.assertFalse(b["independent_candle_replay_verified"])

    def test_future_or_unclosed_blocked(self):
        for t in (119999, 180001):
            with self.assertRaises(ValueError):
                candle_bundle(self.row, [{**self.candle, "available_at": t}])
