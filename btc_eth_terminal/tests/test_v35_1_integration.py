"""Actual frozen-source and real HTTP/SSE v35.1 vertical slice tests.
Run with PYTHONPATH pointing to the *frozen v34.1 backend* plus this release.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from v35_replay_service import StateRejected, replay_capture
from v35_unified_gateway import server_for

DATA = Path(
    os.environ.get(
        "V34_1_BACKEND_DATA",
        str(Path(__file__).resolve().parents[1] / "backend" / "data"),
    )
)
CLEAN = (
    next((DATA / "live_v14_clean_120s").glob("coinbase*"), None)
    if (DATA / "live_v14_clean_120s").is_dir()
    else None
)
DISCONNECT = (
    next((DATA / "live_v14_disconnect_120s").glob("coinbase*"), None)
    if (DATA / "live_v14_disconnect_120s").is_dir()
    else None
)


def pin(root):
    return sha256((root / "manifest.json").read_bytes()).hexdigest()


class FrozenReplayContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if CLEAN is None or not CLEAN.exists():
            raise unittest.SkipTest(
                "Real pinned source unavailable; do not infer source validation"
            )
        cls.ds = replay_capture(CLEAN, pin(CLEAN))

    def test_ingress_certificate_never_live(self):
        d = self.ds
        self.assertEqual(d.certificate["admission"], "VERIFIED_ARCHIVE_ENVELOPE")
        self.assertEqual(d.message_count, 5177)
        for flag in ("live_feed", "trading_enabled", "execution_authorized"):
            self.assertIs(d.certificate[flag], False)

    def test_receiver_time_and_state_versions_monotone(self):
        for p, rows in self.ds.rows.items():
            self.assertGreater(len(rows), 100)
            self.assertEqual(
                [r["state_version"] for r in rows],
                sorted(set(r["state_version"] for r in rows)),
            )
            self.assertEqual(
                [r["receiver_time_ms"] for r in rows],
                sorted(r["receiver_time_ms"] for r in rows),
            )
            self.assertTrue(
                all(
                    r["product"] == p
                    and r["mode"] == "ARCHIVE_REPLAY"
                    and not r["live_feed"]
                    for r in rows
                )
            )
            self.assertTrue(
                all(
                    r["source_last_l2_receiver_ms"] <= r["receiver_time_ms"]
                    for r in rows
                )
            )

    def test_all_depth_exact_and_not_crossed(self):
        for rows in self.ds.rows.values():
            for r in rows:
                if r["status"] != "VALID_ARCHIVED_QUOTE":
                    continue
                depth = r["depth"]
                self.assertLess(Decimal(r["best_bid"]), Decimal(r["best_ask"]))
                for side, total in [
                    ("bids", "bid_total_base_10"),
                    ("asks", "ask_total_base_10"),
                ]:
                    ladder = depth[side]
                    self.assertEqual(len(ladder), 10)
                    c = Decimal(0)
                    for entry in ladder:
                        c += Decimal(entry["quantity"])
                        self.assertEqual(c, Decimal(entry["cumulative_base"]))
                    self.assertEqual(c, Decimal(depth[total]))

    def test_final_book_against_independent_dictionary_replay(self):
        # Independent plain-dict decoder; does not import v23.Book or v34.top_levels.
        import json

        books = {p: {"bid": {}, "offer": {}} for p in ("BTC-USD", "ETH-USD")}
        manifest = json.loads((CLEAN / "manifest.json").read_bytes())
        for entry in manifest["chunks"]:
            for line in (CLEAN / entry["file"]).read_bytes().splitlines():
                msg = json.loads(json.loads(line)["raw_text"])
                if msg["channel"] != "l2_data":
                    continue
                for evt in msg["events"]:
                    book = books[evt["product_id"]]
                    if evt["type"] == "snapshot":
                        book["bid"].clear()
                        book["offer"].clear()
                    for u in evt["updates"]:
                        p = Decimal(u["price_level"])
                        q = Decimal(u["new_quantity"])
                        if q == 0:
                            book[u["side"]].pop(p, None)
                        else:
                            book[u["side"]][p] = q
        for prod in ("BTC-USD", "ETH-USD"):
            latest = self.ds.snapshot(prod)["state"]
            self.assertEqual(Decimal(latest["best_bid"]), max(books[prod]["bid"]))
            self.assertEqual(Decimal(latest["best_ask"]), min(books[prod]["offer"]))
            for side, desc, key in [("bid", True, "bids"), ("offer", False, "asks")]:
                independent = sorted(books[prod][side].items(), reverse=desc)[:10]
                actual = latest["depth"][key]
                self.assertEqual(
                    [(Decimal(x["price"]), Decimal(x["quantity"])) for x in actual],
                    independent,
                )

    def test_deterministic_state_digest(self):
        second = replay_capture(CLEAN, pin(CLEAN))
        self.assertEqual(self.ds.final_state_sha256, second.final_state_sha256)
        self.assertEqual(self.ds.rows, second.rows)

    def test_bad_version_and_product_fails_closed(self):
        for prod in ("SOL-USD", "BTCUSDT", ""):
            with self.assertRaises(StateRejected):
                self.ds.snapshot(prod)
        with self.assertRaisesRegex(StateRejected, "INVALID_STATE_VERSION"):
            self.ds.snapshot("BTC-USD", 0)
        with self.assertRaisesRegex(StateRejected, "RESYNC_REQUIRED"):
            self.ds.page("BTC-USD", after=9999999)
        with self.assertRaises(StateRejected):
            self.ds.page("ETH-USD", after=3.14)

    def test_reconnect_capture_is_quarantined(self):
        if DISCONNECT is None:
            self.skipTest("Quarantine capture unavailable")
        with self.assertRaisesRegex(StateRejected, "QUARANTINED"):
            replay_capture(DISCONNECT, pin(DISCONNECT))

    def test_externally_wrong_manifest_pin_rejected(self):
        with self.assertRaisesRegex(StateRejected, "MANIFEST_PIN_MISMATCH"):
            replay_capture(CLEAN, "0" * 64)

    def test_chunk_tampered_between_verify_and_use_never_published(self):
        import v35_replay_service as m

        with tempfile.TemporaryDirectory() as d:
            clone = Path(d) / "capture"
            shutil.copytree(CLEAN, clone)
            digest = pin(clone)
            original = m.verify_capture

            def mutate_after_gate(root, expected):
                cert = original(root, expected)
                target = next((Path(root) / "chunks").glob("*.jsonl"))
                with target.open("ab") as fp:
                    fp.write(b"\n")
                return cert

            with (
                patch.object(m, 'verify_capture', mutate_after_gate),
                self.assertRaisesRegex(
                    StateRejected,
                    'SOURCE_CHANGED_AFTER_ADMISSION|SOURCE_LENGTH_CHANGED_AFTER_ADMISSION',
                ),
            ):
                replay_capture(clone, digest)

    def test_symlinked_chunk_substitution_rejected(self):
        import v35_replay_service as m

        with tempfile.TemporaryDirectory() as d:
            clone = Path(d) / "capture"
            shutil.copytree(CLEAN, clone)
            digest = pin(clone)
            original = m.verify_capture

            def swap_after_gate(root, expected):
                cert = original(root, expected)
                target = next((Path(root) / "chunks").glob("*.jsonl"))
                target.rename(target.with_suffix(".backup"))
                target.symlink_to(target.with_suffix(".backup"))
                return cert

            with (
                patch.object(m, 'verify_capture', swap_after_gate),
                self.assertRaises(StateRejected),
            ):
                replay_capture(clone, digest)


class RealHTTPContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if CLEAN is None or not CLEAN.exists():
            raise unittest.SkipTest("No real source")
        cls.dataset = (
            FrozenReplayContract.ds
            if hasattr(FrozenReplayContract, "ds")
            else replay_capture(CLEAN, pin(CLEAN))
        )
        cls.server = server_for(cls.dataset)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def get(self, path, headers=None):
        try:
            with urlopen(
                Request(self.base + path, headers=headers or {}), timeout=10
            ) as res:
                return res.status, res.headers, res.read()
        except HTTPError as ex:
            return ex.code, ex.headers, ex.read()

    def test_snapshot_http_source_and_v34_compatibility(self):
        for p in ("BTC-USD", "ETH-USD"):
            code, headers, body = self.get("/api/v35/snapshot?product=" + p)
            self.assertEqual(code, 200)
            obj = json.loads(body)
            self.assertEqual(obj["mode"], "ARCHIVE_REPLAY")
            self.assertEqual(obj["state"]["product"], p)
            self.assertFalse(obj["live_feed"])
        for route in (
            "/research",
            "/terminal",
            "/api/v30/catalog",
            "/api/v34/depth?product=BTC-USD&asof=1794642600000",
        ):
            self.assertEqual(self.get(route)[0], 200, route)

    def test_sse_state_cursor_and_finish(self):
        status, headers, body = self.get("/api/v35/stream?product=BTC-USD&limit=3")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type"))
        stream = body.decode()
        self.assertIn("event: source\n", stream)
        self.assertEqual(stream.count("event: state\n"), 3)
        self.assertIn("event: replay_end\n", stream)
        first = self.dataset.rows["BTC-USD"][2]["state_version"]
        status, _, reconnected = self.get(
            "/api/v35/stream?product=BTC-USD&limit=2",
            headers={
                "Last-Event-ID": self.dataset.certificate["manifest_sha256"]
                + ":BTC-USD:"
                + str(first)
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(
            "id: "
            + self.dataset.certificate["manifest_sha256"]
            + ":BTC-USD:"
            + str(self.dataset.rows["BTC-USD"][3]["state_version"])
            + "\n",
            reconnected.decode(),
        )

    def test_gaps_queries_and_mutating_methods_rejected(self):
        for url, status in [
            ("/api/v35/snapshot?product=SOL-USD", 400),
            ("/api/v35/snapshot?product=BTC-USD&product=ETH-USD", 400),
            ("/api/v35/history?product=BTC-USD&after=999999", 409),
            ("/api/v35/stream?product=BTC-USD&limit=99", 400),
            ("/api/v35/health?token=x", 400),
            ("/api/v35/orders", 404),
        ]:
            self.assertEqual(self.get(url)[0], status, url)
        try:
            urlopen(
                Request(
                    self.base + "/api/v35/snapshot?product=BTC-USD",
                    data=b"{}",
                    method="POST",
                ),
                timeout=5,
            )
            self.fail("POST must never succeed")
        except HTTPError as ex:
            self.assertIn(ex.code, (404, 405, 501))

    def test_host_binding_and_finite_sse(self):
        with self.assertRaisesRegex(StateRejected, "LOOPBACK_ONLY"):
            server_for(self.dataset, host="0.0.0.0")
        code, headers, body = self.get("/api/v35/stream?product=ETH-USD&limit=1")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertNotIn(b"event: live", body)


if __name__ == "__main__":
    unittest.main()
