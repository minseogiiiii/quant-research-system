"""Q6 source-pinned v36 bundle must never overwrite frozen legacy /research."""
from __future__ import annotations

import unittest
from hashlib import sha256
from pathlib import Path

from v34_frontend_integrity import verify_build as verify_legacy
from v36_frontend_integrity import DIST, MANIFEST, PINNED_MANIFEST_SHA256, LIVE_JS, v36_asset


class Q6FrontendIsolation(unittest.TestCase):
    def test_q6_is_a_distinct_pinned_bundle_with_liveness_guard(self):
        original, _ = verify_legacy()
        index, _ = v36_asset('/research/v36')
        js, kind = v36_asset(LIVE_JS)
        self.assertIn(b'index-BpiBDnMe.js', index)
        self.assertIn(b'HEALTH_PROBE_STALE_OR_CLOCK_ANOMALY', js)
        self.assertEqual(kind, 'application/javascript')
        self.assertEqual(sha256(MANIFEST.read_bytes()).hexdigest(), PINNED_MANIFEST_SHA256)
        self.assertIn('index.html', original)
        self.assertNotEqual(original['index.html'], index)
        self.assertEqual(DIST.name, 'dist_v36')

    def test_versioned_build_script_never_targets_legacy_dist(self):
        source = (Path(__file__).resolve().parents[1] / 'scripts/v36_build_frontend.py').read_text()
        self.assertIn("'--outDir', staging.name", source)
        self.assertIn("verify_historical()", source)
        self.assertIn("parser.add_argument('--promote'", source)
        self.assertNotIn("shutil.rmtree(FE / 'dist')", source)
        self.assertNotIn("shutil.copytree(FE / 'dist', FE / 'dist_v36')", source)


if __name__ == '__main__':
    unittest.main()
