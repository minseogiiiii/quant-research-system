"""Real-source cross-archive SSE Last-Event-ID provenance regression."""

from __future__ import annotations

import json
import unittest
from hashlib import sha256
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from v35_replay_service import replay_capture
from v35_unified_gateway import server_for

BASE = Path(__file__).resolve().parents[1] / "backend/data"
CAPTURES = [
    BASE / "live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy",
    BASE / "live_disk_safe_final_180s_20261114/coinbase_btc_eth_soak_rV6Xox",
]


class CrossArchiveResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(p.is_dir() for p in CAPTURES):
            raise unittest.SkipTest("Real Coinbase source archives are not mounted")
        cls.datasets = [
            replay_capture(p, sha256((p / "manifest.json").read_bytes()).hexdigest())
            for p in CAPTURES
        ]
        cls.servers = []
        for ds in cls.datasets:
            server = server_for(ds)
            t = Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.03}, daemon=True
            )
            t.start()
            cls.servers.append(
                (server, t, f"http://127.0.0.1:{server.server_address[1]}")
            )

    @classmethod
    def tearDownClass(cls):
        for server, thread, _ in cls.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)
            if thread.is_alive():
                raise AssertionError("Server did not terminate")

    @classmethod
    def get(cls, idx: int, product: str, cursor: str):
        url = cls.servers[idx][2] + "/api/v35/stream?product=" + product + "&limit=2"
        try:
            with urlopen(
                Request(url, headers={"Last-Event-ID": cursor}), timeout=5
            ) as r:
                return r.status, r.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_scoped_cursor_reconnect_correct_archive(self):
        ds = self.datasets[0]
        pin = ds.certificate["manifest_sha256"]
        first = ds.rows["BTC-USD"][2]["state_version"]
        status, body = self.get(0, "BTC-USD", f"{pin}:BTC-USD:{first}")
        self.assertEqual(status, 200)
        self.assertIn(
            f"id: {pin}:BTC-USD:{ds.rows['BTC-USD'][3]['state_version']}", body
        )
        self.assertIn("event: replay_end", body)

    def test_old_archive_cursor_rejected_on_different_real_archive(self):
        first = self.datasets[0].rows["BTC-USD"][2]["state_version"]
        old = self.datasets[0].certificate["manifest_sha256"] + ":BTC-USD:" + str(first)
        status, body = self.get(1, "BTC-USD", old)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "SOURCE_CHANGED_RESYNC_REQUIRED")

    def test_legacy_unscoped_cursor_rejected(self):
        first = self.datasets[0].rows["BTC-USD"][2]["state_version"]
        code, body = self.get(0, "BTC-USD", str(first))
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["error"], "UNSCOPED_CURSOR_RESYNC_REQUIRED")

    def test_cross_product_reuse_rejected(self):
        ds = self.datasets[0]
        old = (
            ds.certificate["manifest_sha256"]
            + ":BTC-USD:"
            + str(ds.rows["BTC-USD"][2]["state_version"])
        )
        status, body = self.get(0, "ETH-USD", old)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "PRODUCT_CHANGED_RESYNC_REQUIRED")

    def test_unknown_state_requires_resync(self):
        pin = self.datasets[0].certificate["manifest_sha256"]
        status, body = self.get(0, "BTC-USD", pin + ":BTC-USD:9999999")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "RESYNC_REQUIRED")

    def test_numeric_sse_after_query_rejected_even_if_state_exists(self):
        # An old /stream?after=N URL has no source/product lineage.
        from urllib.error import HTTPError

        valid = self.datasets[0].rows["BTC-USD"][2]["state_version"]
        url = (
            self.servers[1][2]
            + "/api/v35/stream?product=BTC-USD&after="
            + str(valid)
            + "&limit=2"
        )
        with self.assertRaises(HTTPError) as exc:
            urlopen(url, timeout=5)
        self.assertEqual(exc.exception.code, 409)
        self.assertEqual(json.loads(exc.exception.read())["error"], "RESYNC_REQUIRED")

    def test_malformed_cursor_is_http_400(self):
        status, body = self.get(0, "BTC-USD", "something-unsafe")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "INVALID_SSE_RESUME_CURSOR")


if __name__ == "__main__":
    unittest.main()
