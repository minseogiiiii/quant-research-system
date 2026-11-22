"""v36 synthetic faults + genuine Coinbase-archive transition tests.

These do NOT certify a new live Coinbase connection. Archive input is always
ARCHIVE_SIMULATION, and deliberately fabricated faults are labelled fixtures.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from v35_replay_service import _features, replay_capture
from v36_live_engine import LiveMarketEngine, StateRejected
from v36_live_gateway import server_for_live

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
PRODUCTS = ("BTC-USD", "ETH-USD")


def records(limit=None):
    manifest = json.loads((SOURCE / "manifest.json").read_bytes())
    count = 0
    for chunk in manifest["chunks"]:
        with (SOURCE / chunk["file"]).open("rb") as fp:
            for line in fp:
                yield line
                count += 1
                if limit is not None and count >= limit:
                    return


def corrupt(line, *, wire_change=None, envelope_change=None):
    env = json.loads(line)
    w = json.loads(env["raw_text"])
    if wire_change:
        wire_change(w)
    env["raw_text"] = json.dumps(w, separators=(",", ":"))
    env["sha256"] = sha256(env["raw_text"].encode()).hexdigest()
    if envelope_change:
        envelope_change(env)
    return (json.dumps(env, separators=(",", ":")) + "\n").encode()


class GenuineArchiveTransition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_dir():
            raise unittest.SkipTest("Private original source not present")
        cls.ingested = LiveMarketEngine()
        for line in records():
            cls.ingested.consume(line)
        cls.replay = replay_capture(
            SOURCE, sha256((SOURCE / "manifest.json").read_bytes()).hexdigest()
        )

    def test_real_source_5177_messages_are_archive_simulation_not_live(self):
        state = self.ingested.health()
        self.assertEqual(state["messages"], 5177)
        self.assertEqual(state["status"], "HEALTHY")
        self.assertEqual(state["mode"], "ARCHIVE_SIMULATION")
        self.assertFalse(state["live_feed"])
        self.assertFalse(state["trading_enabled"])
        self.assertEqual(
            state["subscriptions"], ["heartbeats", "level2", "market_trades"]
        )

    def test_real_full_book_features_match_independent_frozen_replay(self):
        for product in PRODUCTS:
            current = _features(
                self.ingested.books[product], None, self.ingested.ofi[product]
            )
            original = self.replay.rows[product][-1]
            for key in (
                "best_bid",
                "best_ask",
                "spread",
                "microprice",
                "best_imbalance",
                "ofi_since_connection_base",
                "depth",
                "l2_book_levels",
            ):
                self.assertEqual(current[key], original[key], (product, key))

    def test_all_retained_rows_are_bounded_and_mapped_to_source(self):
        for product in PRODUCTS:
            rows = list(self.ingested.rows[product])
            self.assertTrue(60 < len(rows) <= 256)
            self.assertTrue(
                all(
                    x["mode"] == "ARCHIVE_SIMULATION"
                    and x["source_admission"] == "OFFLINE_ARCHIVE_SIMULATION_NOT_LIVE"
                    and not x["trading_enabled"]
                    for x in rows
                )
            )
            self.assertEqual(
                sorted(set(x["state_version"] for x in rows)),
                [x["state_version"] for x in rows],
            )

    def test_closed_candles_have_no_future_trade_values(self):
        for product in PRODUCTS:
            actual = self.ingested.candles_page(product)
            reference = self.replay.candles[product]
            self.assertEqual(len(actual["rows"]), 1)
            for k in ("open", "high", "low", "close", "volume", "observed_trade_count"):
                self.assertEqual(actual["rows"][0][k], reference[0][k], (product, k))
            self.assertGreaterEqual(
                actual["rows"][0]["available_at"], actual["rows"][0]["time"] + 60000
            )
            self.assertFalse(actual["live_feed"])

    def test_snapshot_product_validation_and_safe_health(self):
        for product in PRODUCTS:
            self.assertIsNotNone(self.ingested.snapshot(product)["state"])
        with self.assertRaises(StateRejected):
            self.ingested.snapshot("BTCUSDT")


class FailClosedFixtures(unittest.TestCase):
    def test_archive_never_upgrades_to_live(self):
        e = LiveMarketEngine()
        self.assertFalse(e.health()["live_feed"])
        with self.assertRaises(StateRejected):
            LiveMarketEngine(mode="LIVE_SOURCE")
        with self.assertRaises(StateRejected):
            LiveMarketEngine(
                mode="LIVE_SOURCE",
                collector_runtime_verified=True,
                collector_script_sha256="bad",
            )

    def test_modified_digest_rejected_and_no_prices(self):
        line = json.loads(next(records()))
        line["sha256"] = "a" * 64
        e = LiveMarketEngine()
        with self.assertRaises(StateRejected):
            e.consume((json.dumps(line) + "\n").encode())
        self.assertEqual(e.health()["status"], "QUARANTINED")
        self.assertIsNone(e.snapshot("BTC-USD")["state"])

    def test_gapped_sequence_quarantines_even_if_envelope_hash_repaired(self):
        e = LiveMarketEngine()
        e.consume(next(records()))
        orig = list(records(2))[1]
        damaged = corrupt(orig, wire_change=lambda w: w.update(sequence_num=100))
        with self.assertRaises(StateRejected):
            e.consume(damaged)
        self.assertEqual(e.health()["status"], "QUARANTINED")

    def test_wrong_product_snapshot_quarantines(self):
        line = next(records())

        def replace(w):
            w["events"][0]["product_id"] = "BTCUSDT"

        with self.assertRaises(StateRejected):
            LiveMarketEngine().consume(corrupt(line, wire_change=replace))

    def test_no_snapshot_before_update_and_after_invalidity(self):
        e = LiveMarketEngine()

        def mutate(w):
            w["events"][0]["type"] = "update"

        with self.assertRaises(StateRejected):
            e.consume(corrupt(next(records()), wire_change=mutate))
        with self.assertRaises(StateRejected):
            e.consume(next(records()))

    def test_receiver_clock_regression(self):
        e = LiveMarketEngine()
        e.consume(next(records()))
        first = list(records(2))
        old = json.loads(first[0])["received_utc"]
        old = (
            (datetime.fromisoformat(old.replace("Z", "+00:00")) - timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.assertRaises(StateRejected):
            e.consume(
                corrupt(first[1], envelope_change=lambda z: z.update(received_utc=old))
            )

    def test_live_stale_revokes_prices_and_requires_new_session(self):
        first = list(records(100))
        now = [
            int(
                datetime.fromisoformat(
                    json.loads(first[-1])["received_utc"].replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
        ]
        mono = [0]

        def advance():
            mono[0] += 1_000_000
            return mono[0]

        e = LiveMarketEngine(
            mode="LIVE_SOURCE",
            collector_runtime_verified=True,
            collector_script_sha256="a" * 64,
            clock_ms=lambda: now[0],
            monotonic_ns=advance,
        )
        for item in first:
            e.consume(item)
        self.assertEqual(e.health()["status"], "HEALTHY")
        self.assertIsNotNone(e.snapshot("BTC-USD")["state"])
        now[0] += 10000
        self.assertEqual(e.health()["status"], "STALE")
        self.assertIsNone(e.snapshot("BTC-USD")["state"])
        with self.assertRaises(StateRejected):
            e.consume(first[-1])

    def test_new_connection_rotates_source_session_and_clears_state(self):
        e = LiveMarketEngine()
        for x in records(100):
            e.consume(x)
        first = e.health()["session_id"]
        last = list(records(100))[-1]
        last_env = json.loads(last)
        later = (
            (
                datetime.fromisoformat(last_env["received_utc"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        changed = corrupt(
            next(records()),
            envelope_change=lambda z: z.update(
                connection_id=1,
                received_utc=later,
                local_monotonic_ns=str(
                    int(last_env["local_monotonic_ns"]) + 1_000_000_000
                ),
            ),
        )
        e.consume(changed)
        self.assertNotEqual(first, e.health()["session_id"])
        self.assertEqual(e.health()["status"], "WAITING_FOR_SNAPSHOT")
        self.assertEqual(e.health()["connection_gaps"], 1)
        self.assertIsNone(e.snapshot("BTC-USD")["state"])

    def test_live_rejects_stale_receiver_clock(self):
        e = LiveMarketEngine(
            mode="LIVE_SOURCE",
            collector_runtime_verified=True,
            collector_script_sha256="a" * 64,
        )
        with self.assertRaises(StateRejected):
            e.consume(next(records()))
        self.assertNotEqual(e.health()["status"], "HEALTHY")


class PersistentHttpContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = LiveMarketEngine()
        for line in records(1000):
            cls.e.consume(line)
        cls.server = server_for_live(cls.e)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, headers=None):
        try:
            with urlopen(
                Request(self.url + path, headers=headers or {}), timeout=4
            ) as res:
                return res.status, res.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_live_health_and_snapshot_in_archive_simulation(self):
        status, data = self.request("/api/v36/health")
        self.assertEqual(status, 200)
        h = json.loads(data)
        self.assertFalse(h["live_feed"])
        self.assertEqual(h["mode"], "ARCHIVE_SIMULATION")
        s, body = self.request("/api/v36/snapshot?product=BTC-USD")
        self.assertEqual(s, 200)
        self.assertFalse(json.loads(body)["state"]["live_feed"])

    def test_invalid_product_and_duplicated_queries(self):
        self.assertEqual(self.request("/api/v36/snapshot?product=BTCUSDT")[0], 400)
        self.assertEqual(
            self.request("/api/v36/snapshot?product=BTC-USD&product=ETH-USD")[0], 400
        )
        self.assertEqual(self.request("/api/v36/order")[0], 404)

    def test_first_event_stream_is_initial_source_backed_snapshot(self):
        req = Request(self.url + "/api/v36/stream?product=BTC-USD")
        with urlopen(req, timeout=4) as res:
            self.assertEqual(res.status, 200)
            received = b"".join(res.readline() for _ in range(7))
            self.assertIn(b"event: source\n", received)
            self.assertIn(b"event: snapshot\n", received)
            self.assertIn(b"v36:", received)
            self.assertNotIn(b'"mode":"LIVE_SOURCE"', received)

    def test_wrong_session_cursor_fails_closed(self):
        self.assertEqual(
            self.request(
                "/api/v36/stream?product=BTC-USD",
                {"Last-Event-ID": "v36:" + "f" * 32 + ":BTC-USD:1"},
            )[0],
            409,
        )

    def test_wrong_product_cursor_fails_closed(self):
        last = self.e.snapshot("BTC-USD")["state"]
        token = f"v36:{last['session_id']}:ETH-USD:{last['state_version']}"
        self.assertEqual(
            self.request("/api/v36/stream?product=BTC-USD", {"Last-Event-ID": token})[
                0
            ],
            409,
        )

    def test_no_public_bind(self):
        with self.assertRaises(StateRejected):
            server_for_live(self.e, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
