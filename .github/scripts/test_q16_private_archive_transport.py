"""Verify Q16 token isolation and tagged-release selection offline."""

import unittest

from q16_secure_private_archive_download import (
    ARCHIVE_SHA256,
    ASSET_NAME,
    EXPECTED_BYTES,
    authorized_headers,
    request_kind,
    select_asset,
)


class Q16PrivateArchiveTransportTests(unittest.TestCase):
    def test_signed_https_without_token(self):
        url = "https://storage.example.test/private.zip?sig=test"
        self.assertEqual(authorized_headers(url, ""), [])
        self.assertEqual(request_kind(url, ""), "presigned_https")

    def test_http_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "MUST_BE_HTTPS"):
            authorized_headers("http://example.test/private.zip", "")

    def test_never_send_token_to_third_party(self):
        with self.assertRaisesRegex(ValueError, "REFUSE_TO_SEND"):
            authorized_headers("https://storage.example.test/zip", "secret")

    def test_never_send_token_to_untrusted_github_paths(self):
        for path in (
            "/repos/other/repo/releases/assets/2",
            "/repos/minseogiiiii/private/issues/2",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "ENDPOINT_REQUIRED"):
                    authorized_headers("https://api.github.com" + path, "secret")

    def test_private_asset_api_requires_readonly_token(self):
        url = (
            "https://api.github.com/repos/minseogiiiii/"
            "q7-private-release-archive/releases/assets/123"
        )
        with self.assertRaisesRegex(ValueError, "TOKEN_REQUIRED"):
            authorized_headers(url, "")
        self.assertEqual(request_kind(url, "token"), "github_asset")
        self.assertEqual(
            authorized_headers(url, "read_only_token")[-1],
            "Authorization: Bearer read_only_token",
        )

    def test_private_release_tag_requires_readonly_token(self):
        url = (
            "https://api.github.com/repos/minseogiiiii/"
            "q7-private-release-archive/releases/tags/q7-frozen-2026-11-22"
        )
        with self.assertRaisesRegex(ValueError, "TOKEN_REQUIRED"):
            authorized_headers(url, "")
        self.assertEqual(request_kind(url, "token"), "github_tag")

    def test_github_query_cannot_forward_credentials(self):
        url = (
            "https://api.github.com/repos/minseogiiiii/private/"
            "releases/assets/123?redirect=evil"
        )
        with self.assertRaisesRegex(ValueError, "ENDPOINT_REQUIRED"):
            authorized_headers(url, "token")

    def test_embedded_credentials_disallowed(self):
        url = (
            "https://u:pwd@api.github.com/repos/minseogiiiii/repo/"
            "releases/assets/1"
        )
        with self.assertRaisesRegex(ValueError, "MUST_BE_HTTPS"):
            authorized_headers(url, "secret")

    def test_exact_release_asset_selected_by_name_size_digest(self):
        release = {
            "tag_name": "q7-frozen-2026-11-22",
            "assets": [{
                "id": 123,
                "name": ASSET_NAME,
                "size": EXPECTED_BYTES,
                "digest": "sha256:" + ARCHIVE_SHA256,
                "state": "uploaded",
            }],
        }
        self.assertEqual(select_asset(release, release["tag_name"]), 123)
        modified = {
            **release,
            "assets": [{**release["assets"][0], "digest": "sha256:wrong"}],
        }
        with self.assertRaisesRegex(ValueError, "DIGEST_MISMATCH"):
            select_asset(modified, release["tag_name"])
        modified["assets"] = [
            {**release["assets"][0], "size": EXPECTED_BYTES - 1}
        ]
        with self.assertRaisesRegex(ValueError, "METADATA_INVALID"):
            select_asset(modified, release["tag_name"])


if __name__ == "__main__":
    unittest.main()
