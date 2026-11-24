"""Adversarial, offline artifact transport contracts. No market evidence."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from q29_artifact_transport import (
    SMOKE_FILE,
    SMOKE_NAME,
    TemporaryUnavailable,
    TransportRejected,
    extract,
    fetch_bytes,
    request,
)

SHA = "a" * 40
PAYLOAD = b'{"synthetic":true,"market_evidence":false}\n'


def archive(entries=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries or [(SMOKE_FILE, PAYLOAD)]:
            z.writestr(name, data)
    return buf.getvalue()


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.raw = archive()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.meta = {
            "id": 99, "name": SMOKE_NAME, "expired": False,
            "workflow_run": {"id": 42, "head_sha": SHA},
            "size_in_bytes": len(self.raw), "digest": "sha256:" + self.digest,
        }

    def retrieve(self):
        return fetch_bytes("owner/repo", "test-token", artifact_id=99,
                           name=SMOKE_NAME, run_id=42, sha=SHA,
                           zip_sha=self.digest)

    def test_retries_transient_404_and_reads_exact_id(self):
        with patch("q29_artifact_transport.request", side_effect=[
            TemporaryUnavailable("HTTP_404"),
            json.dumps(self.meta).encode(), self.raw,
        ]) as call, patch("q29_artifact_transport.time.sleep") as sleep:
            self.assertEqual(self.retrieve(), self.raw)
            self.assertEqual(call.call_count, 3)
            self.assertTrue(call.call_args.args[0].endswith("/99/zip"))
            sleep.assert_called_once_with(2)

    def test_retry_budget_bounded(self):
        with patch("q29_artifact_transport.request", side_effect=(
            TemporaryUnavailable("HTTP_503")
        )) as call, patch("q29_artifact_transport.time.sleep"):
            with self.assertRaises(TransportRejected):
                self.retrieve()
            self.assertEqual(call.call_count, 6)

    def test_invalid_identity_not_retried(self):
        variants = [
            {"id": 98}, {"name": "another"}, {"expired": True},
            {"workflow_run": {"id": 43, "head_sha": SHA}},
            {"workflow_run": {"id": 42, "head_sha": "b" * 40}},
            {"digest": "sha256:" + "c" * 64},
        ]
        for variant in variants:
            with self.subTest(variant=variant), patch(
                "q29_artifact_transport.request",
                return_value=json.dumps({**self.meta, **variant}).encode(),
            ) as call:
                with self.assertRaises(TransportRejected):
                    self.retrieve()
                self.assertEqual(call.call_count, 1)

    def test_tampered_zip_rejected(self):
        bad = bytes([self.raw[0] ^ 1]) + self.raw[1:]
        with patch("q29_artifact_transport.request", side_effect=[
            json.dumps(self.meta).encode(), bad,
        ]) as call:
            with self.assertRaises(TransportRejected):
                self.retrieve()
            self.assertEqual(call.call_count, 2)

    def test_extract_exact_members_and_fresh_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "verified"
            extract(self.raw, dest, {SMOKE_FILE}, lambda p: None)
            self.assertEqual((dest / SMOKE_FILE).read_bytes(), PAYLOAD)
            with self.assertRaises(TransportRejected):
                extract(self.raw, dest, {SMOKE_FILE}, lambda p: None)

    def test_extra_path_or_duplicate_member_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ["../escape", "raw.jsonl", SMOKE_FILE]:
                with self.subTest(name=name):
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        bad = archive([(SMOKE_FILE, PAYLOAD), (name, b"x")])
                    with self.assertRaises(TransportRejected):
                        extract(bad, root / "verified", {SMOKE_FILE},
                                lambda p: None)
                    self.assertFalse((root / "verified").exists())

    def test_failed_validator_does_not_publish_directory(self):
        def reject(_root):
            raise TransportRejected("BAD_MANIFEST")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "verified"
            with self.assertRaises(TransportRejected):
                extract(self.raw, dest, {SMOKE_FILE}, reject)
            self.assertFalse(dest.exists())

    def test_token_cannot_be_sent_to_non_github_host(self):
        with self.assertRaises(TransportRejected):
            request("https://example.invalid/blob", "secret", limit=1024)


if __name__ == "__main__":
    unittest.main()
