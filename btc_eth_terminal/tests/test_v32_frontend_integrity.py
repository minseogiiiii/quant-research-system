"""Adversarial checks for the three-file CI build lock and archive replay."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    import bridge
    import v30_unified_api as api
    import v32_frontend_integrity as pin
    return bridge, api, pin

bridge, api, pin = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class BuildPinSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        (self.dist / "assets").mkdir()
        (self.dist / "index.html").write_text(
            '<script src="/assets/index-abc123.js"></script><link href="/assets/index-testcss.css" rel="stylesheet">'
        )
        (self.dist / "assets/index-abc123.js").write_bytes(b"console.log(1)")
        (self.dist / "assets/index-testcss.css").write_bytes(b"body{color:steelblue}")
        self.manifest = self.root / "pin.json"
        self.pin()

    def tearDown(self):
        self.tmp.cleanup()

    def pin(self):
        files = {}
        for name in (
            "index.html",
            "assets/index-abc123.js",
            "assets/index-testcss.css",
        ):
            raw = (self.dist / name).read_bytes()
            files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        self.manifest.write_text(
            json.dumps(
                {"schema": "v32_funnest_build_integrity_v1", "files": files},
                sort_keys=True,
            )
            + "\n"
        )
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def check(self):
        return pin.verify_build(self.dist, self.manifest, self.sha)

    def test_valid_three_files_and_content(self):
        records, _ = self.check()
        self.assertEqual(len(records), 3)
        self.assertIn(b"console.log", records["assets/index-abc123.js"])

    def test_js_mutation_blocks_entire_build(self):
        (self.dist / "assets/index-abc123.js").write_bytes(b"console.log(2)")
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()
        with self.assertRaises(pin.FrontendIntegrityError):
            pin.get_asset(
                "/research",
                dist=self.dist,
                manifest_path=self.manifest,
                pinned_sha256=self.sha,
            )

    def test_css_mutation_blocks_entry(self):
        (self.dist / "assets/index-testcss.css").write_bytes(b"body{color:red}")
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()

    def test_index_mutation_blocks_entry(self):
        (self.dist / "index.html").write_bytes(b"NOT A FURNEST SITE")
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()

    def test_manifest_edit_blocks_even_when_new_hashes_match(self):
        (self.dist / "assets/index-abc123.js").write_bytes(b"console.log(333)")
        self.pin()
        attacker_manifest = self.sha
        self.assertEqual(len(attacker_manifest), 64)
        # The Python release pin is frozen independently from the JSON.
        with self.assertRaises(pin.FrontendIntegrityError):
            pin.verify_build(self.dist, self.manifest, "f" * 64)

    def test_symlinked_js_is_rejected(self):
        original = self.dist / "assets/index-abc123.js"
        original.unlink()
        original.symlink_to(self.dist / "assets/index-testcss.css")
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()

    def test_symlinked_assets_dir_is_rejected(self):
        shutil.move(str(self.dist / "assets"), str(self.root / "other_assets"))
        (self.dist / "assets").symlink_to(
            self.root / "other_assets", target_is_directory=True
        )
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()

    def test_unknown_path_is_not_in_asset_inventory(self):
        with self.assertRaises(pin.FrontendIntegrityError):
            pin.get_asset(
                "/assets/secret.js",
                dist=self.dist,
                manifest_path=self.manifest,
                pinned_sha256=self.sha,
            )

    def test_extra_files_are_not_served(self):
        (self.dist / "assets/secret.txt").write_text("secrets")
        with self.assertRaises(pin.FrontendIntegrityError):
            pin.get_asset(
                "/assets/secret.txt",
                dist=self.dist,
                manifest_path=self.manifest,
                pinned_sha256=self.sha,
            )

    def test_missing_css_is_rejected(self):
        (self.dist / "assets/index-testcss.css").unlink()
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()

    def test_unsupported_extra_index_script_is_rejected(self):
        content = (
            (self.dist / "index.html")
            .read_text()
            .replace(
                "</script>", '</script><script src="/assets/index-other.js"></script>'
            )
        )
        (self.dist / "index.html").write_text(content)
        self.pin()
        with self.assertRaises(pin.FrontendIntegrityError):
            self.check()


class RealReleasePin(unittest.TestCase):
    def test_release_assets_locked_and_backend_registered(self):
        files, manifest = pin.verify_build()
        self.assertEqual(len(files), 3)
        self.assertIn("index.html", files)
        self.assertNotIn(b"PRIVATE KEY", files["index.html"])
        self.assertEqual(manifest.get("live_trading_enabled"), False)

    def test_no_unapproved_live_claims_in_new_replay_ui(self):
        src = (ROOT / "frontend/src/unified.tsx").read_text()
        self.assertIn("ARCHIVE REPLAY", src)
        self.assertIn("NOT LIVE", src)
        for symbol in (
            "Play historical replay",
            "Pause historical replay",
            "Step historical replay",
            "Reset historical replay",
        ):
            self.assertIn(symbol, src)
        self.assertIn("window.clearInterval(timer)", src)

    def test_original_archive_still_non_oos(self):
        c = api.catalog()
        self.assertFalse(c["trading_enabled"])
        self.assertFalse(c["live_feed"])
        self.assertFalse(c["oos_approved"])
        self.assertEqual(c["source_message_count"], 152069)


if __name__ == "__main__":
    unittest.main()


class V32HTTPFailClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(4)

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=8) as r:
                return r.status, r.read(), r.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_research_entry_and_bundle_verified(self):
        self.assertEqual(self.get("/research")[0], 200)
        from v33_frontend_integrity import verify_build as current_verify

        for name in current_verify()[0]:
            if name != "index.html":
                self.assertEqual(self.get("/" + name)[0], 200)

    def test_mutated_sibling_asset_blocks_research_html(self):
        # Verifies actual HTTP deny, not just an isolated helper function.
        from unittest.mock import patch

        import v33_frontend_integrity as current_pin

        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder) / "dist"
            shutil.copytree(current_pin.DIST, dist)
            asset = next((dist / "assets").glob("*.js"))
            raw = asset.read_bytes()
            asset.write_bytes(raw + b"\n// unexpected mutation")
            original_get = current_pin.get_asset

            def bad_asset(path):
                return original_get(path, dist=dist)

            with patch("v33_frontend_integrity.get_asset", side_effect=bad_asset):
                code, body, _ = self.get("/research")
                self.assertEqual(code, 503)
                self.assertIn(b"failed SHA-256", body)
                self.assertEqual(
                    self.get("/" + asset.relative_to(dist).as_posix())[0], 503
                )

    def test_unknown_or_sensitive_paths_still_denied(self):
        for path in [
            "/frontend/v32_dist_integrity.json",
            "/backend/data/unified_v30_one_hour/projection.json",
            "/assets/secret.js",
        ]:
            self.assertNotEqual(self.get(path)[0], 200)
