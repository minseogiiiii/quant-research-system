"""Synthetic-only CI contract tests for source/product scoped SSE cursors.

Real Coinbase archive cross-source tests run locally in test_v35_3_source_bound_stream.py.
No synthetic quote is treated as verified market data here.
"""
from __future__ import annotations
import json
import unittest
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from v35_replay_service import ReplayDataset
from v35_unified_gateway import server_for

PIN_A = "a" * 64
PIN_B = "b" * 64

def dataset(pin: str):
    return ReplayDataset(
        certificate={"manifest_sha256": pin, "admission": "VERIFIED_ARCHIVE_ENVELOPE"},
        rows={p: tuple({"state_version": n, "product": p, "source_manifest_sha256": pin,
                         "mode": "ARCHIVE_REPLAY", "live_feed": False,
                         "trading_enabled": False, "fixture_kind": "SYNTHETIC_CI_ONLY"}
                        for n in (1,2,3)) for p in ("BTC-USD", "ETH-USD")},
        final_state_sha256="0"*64, message_count=0,
    )

class ScopedCursorContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for pin in (PIN_A, PIN_B):
            server = server_for(dataset(pin))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers.append((server,thread,
                f"http://127.0.0.1:{server.server_address[1]}"))
    @classmethod
    def tearDownClass(cls):
        for server,thread,_ in cls.servers:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def stream(self, which, product, token):
        url=self.servers[which][2]+"/api/v35/stream?product="+product+"&limit=2"
        try:
            with urlopen(Request(url,headers={"Last-Event-ID":token}),timeout=4) as r:
                return r.status,r.read().decode()
        except HTTPError as err:
            return err.code,err.read().decode()

    def test_valid_resume(self):
        status,body=self.stream(0,"BTC-USD",PIN_A+":BTC-USD:1")
        self.assertEqual(status,200)
        self.assertIn("id: "+PIN_A+":BTC-USD:2\n",body)
        self.assertIn("event: replay_end\n",body)

    def test_cross_source_fail_closed(self):
        status,body=self.stream(1,"BTC-USD",PIN_A+":BTC-USD:1")
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"SOURCE_CHANGED_RESYNC_REQUIRED")

    def test_cross_product_fail_closed(self):
        status,body=self.stream(0,"ETH-USD",PIN_A+":BTC-USD:1")
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"PRODUCT_CHANGED_RESYNC_REQUIRED")

    def test_legacy_numeric_cursor_fail_closed(self):
        status,body=self.stream(0,"BTC-USD","1")
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"UNSCOPED_CURSOR_RESYNC_REQUIRED")

    def test_unknown_state_fail_closed(self):
        status,body=self.stream(0,"BTC-USD",PIN_A+":BTC-USD:999")
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"RESYNC_REQUIRED")

    def test_numeric_stream_after_is_rejected_without_source_lineage(self):
        # A URL after=N is not a source/product-qualified SSE resume token.
        url=self.servers[1][2]+"/api/v35/stream?product=BTC-USD&after=1&limit=2"
        with self.assertRaises(HTTPError) as cm:
            urlopen(url,timeout=4)
        self.assertEqual(cm.exception.code,409)
        self.assertEqual(json.loads(cm.exception.read())["error"],"RESYNC_REQUIRED")

    def test_invalid_cursor_rejected(self):
        status,body=self.stream(0,"BTC-USD","FAKE")
        self.assertEqual(status,400)
        self.assertEqual(json.loads(body)["error"],"INVALID_SSE_RESUME_CURSOR")

if __name__=="__main__":unittest.main()
