from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import v35_1_frontend_integrity as candidate
from v35_replay_service import replay_capture
from v35_unified_gateway import server_for

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(
    os.environ.get(
        "V34_1_BACKEND_DATA",
        str(Path(__file__).resolve().parents[1] / "backend" / "data"),
    )
)
SOURCE = next((DATA / "live_v14_clean_120s").glob("coinbase*"), None)


class CandidateBrowserServing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SOURCE is None:
            raise unittest.SkipTest(
                "Real frozen source unavailable; browser HTTP test not certified"
            )
        ds = replay_capture(
            SOURCE, sha256((SOURCE / "manifest.json").read_bytes()).hexdigest()
        )
        cls.server = server_for(ds)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read()
        except HTTPError as e:
            return e.code, e.read()

    def test_both_legacy_and_candidate_routes(self):
        self.assertEqual(self.get("/research")[0], 200)
        self.assertEqual(self.get("/terminal")[0], 200)
        for path in (
            "/research/v35.1",
            candidate.CANDIDATE_JS,
            candidate.CANDIDATE_CSS,
        ):
            code, data = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertGreater(len(data), 100)
        self.assertEqual(self.get("/api/v35/health")[0], 200)

    def test_candidate_bundle_sha_and_page_contract(self):
        raw = candidate.MANIFEST.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), candidate.PINNED_MANIFEST_SHA256)
        m = json.loads(raw)
        for name, meta in m["files"].items():
            data = (candidate.DIST / name).read_bytes()
            self.assertEqual(len(data), meta["bytes"])
            self.assertEqual(sha256(data).hexdigest(), meta["sha256"])
        js = self.get(candidate.CANDIDATE_JS)[1]
        self.assertIn(b"/api/v35/health", js)
        self.assertIn(b"ARCHIVE_REPLAY", js)
        self.assertIn(b"NO CONFIRMED EXECUTABLE ALPHA", js)

    def test_mutated_candidate_refuses_http_but_legacy_is_intact(self):
        with tempfile.TemporaryDirectory() as temp:
            dst = Path(temp) / "dist"
            shutil.copytree(candidate.DIST, dst)
            js = dst / candidate.CANDIDATE_JS.removeprefix("/")
            js.write_bytes(js.read_bytes() + b"\n// tamper")
            with patch.object(candidate, "DIST", dst):
                self.assertEqual(self.get("/research/v35.1")[0], 503)
                self.assertEqual(self.get(candidate.CANDIDATE_JS)[0], 503)
                self.assertEqual(self.get("/research")[0], 200)
            self.assertEqual(self.get("/research/v35.1")[0], 200)

    def test_query_assets_forbidden_and_no_orders(self):
        self.assertEqual(self.get("/research/v35.1?user=abc")[0], 503)
        self.assertEqual(self.get("/api/v35/orders")[0], 404)


if __name__ == "__main__":
    unittest.main()
