"""Q16 private archive transport rules; never touch the network in unit tests."""

import unittest

from q16_secure_private_archive_download import authorized_headers


class Q16PrivateArchiveTransportTests(unittest.TestCase):
    def test_regular_https_presigned_download_requires_no_github_token(self):
        url = "https://storage.example.test/private.zip?sig=private"
        self.assertEqual(authorized_headers(url, ""), [])

    def test_http_protocol_is_never_allowed(self):
        with self.assertRaisesRegex(ValueError, "MUST_BE_HTTPS"):
            authorized_headers("http://example.test/private.zip", "")

    def test_token_never_sent_to_third_party(self):
        with self.assertRaisesRegex(ValueError, "REFUSE_TO_SEND"):
            authorized_headers("https://storage.example.test/file.zip", "test_token")

    def test_token_never_sent_to_untrusted_github_paths(self):
        with self.assertRaisesRegex(ValueError, "ASSET_ENDPOINT_REQUIRED"):
            authorized_headers("https://api.github.com/repos/other/repo/releases/assets/2", "secret")
        with self.assertRaisesRegex(ValueError, "ASSET_ENDPOINT_REQUIRED"):
            authorized_headers("https://api.github.com/repos/minseogiiiii/private/issues/2", "secret")

    def test_private_github_release_requires_token(self):
        u = "https://api.github.com/repos/minseogiiiii/q7-private-archive/releases/assets/123"
        with self.assertRaisesRegex(ValueError, "TOKEN_REQUIRED"):
            authorized_headers(u, "")
        h = authorized_headers(u, "read_only_token")
        self.assertEqual(len(h), 3)
        self.assertEqual(h[-1], "Authorization: Bearer read_only_token")

    def test_github_query_cannot_redirect_credentials(self):
        u = "https://api.github.com/repos/minseogiiiii/priv/releases/assets/123?next=evil"
        with self.assertRaisesRegex(ValueError, "ASSET_ENDPOINT_REQUIRED"):
            authorized_headers(u, "token")

    def test_embedded_credentials_disallowed(self):
        with self.assertRaisesRegex(ValueError, "MUST_BE_HTTPS"):
            authorized_headers("https://u:pwd@api.github.com/repos/minseogiiiii/repo/releases/assets/1", "secret")


if __name__ == "__main__":
    unittest.main()
