# Q16: private Q7 archive for CI without AWS or other cloud accounts

This setup uses the **existing GitHub account only**, with two repositories:
a new **private archive repository** and the existing development repository.

## 1. Create a strictly private GitHub archive repository

1. Open https://github.com/new.
2. Owner: `minseogiiiii`.
3. Repository name: `q7-private-release-archive`.
4. Visibility: **Private**. Never turn this repository public.
5. Initialize it with a README so a Release tag can point to the first commit.

## 2. Upload the exact original Q7 ZIP to a private Release

1. Open https://github.com/minseogiiiii/q7-private-release-archive/releases/new.
2. Enter new tag: `q7-frozen-2026-11-22`.
3. Title: `Q7 Frozen Private Qualification Archive`.
4. In **Attach binaries**, upload the exact original:
   `BTC_ETH_V36_3_R5_Q7_VERIFIED_ENGINEERING_CANDIDATE.zip`.
5. Publish. Since the repository is private, the Release is also private.
   Never attach it to the public `quant-research-system` repository.

The immutable full ZIP is **61,656,622 bytes** and has SHA-256
`0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15`.

GitHub Releases support file attachments up to 2 GiB. Ordinary GitHub
browser uploads to source files are limited to 25 MiB; **use a Release asset**,
not **Add file > Upload files**.

## 3. Create a least-privilege fine-grained GitHub token

1. Open https://github.com/settings/personal-access-tokens/new.
2. Resource owner: `minseogiiiii`; repository access: **Only select repositories**.
3. Select **only** `q7-private-release-archive`.
4. Repository permissions: **Contents: Read-only** (metadata read automatic).
5. Short expiration, e.g. 7 days; create and copy the token securely.
6. Do not paste the token in Git, PRs, issue comments, logs, or chat.

The existing public development repository's automatic GITHUB_TOKEN cannot
read assets in a different private repository. The separate read-only token
is therefore intentional and scope-restricted.

## 4. Replace/create two Actions Secrets in the development repository

Settings URL:
https://github.com/minseogiiiii/quant-research-system/settings/secrets/actions

**Q13_PRIVATE_RELEASE_URL** value (exactly):

`https://api.github.com/repos/minseogiiiii/q7-private-release-archive/releases/tags/q7-frozen-2026-11-22`

**Q13_GITHUB_ARCHIVE_TOKEN** value: the new **fine-grained read-only**
personal access token from step 3.

The URL is an authenticated GitHub API **Release by tag** endpoint; it is
not an anonymous file link. The Q16 downloader queries this endpoint using
the token, selects precisely one named uploaded ZIP with the correct size
and, if available, published digest, then requests its actual Release
asset and independently checks full ZIP SHA-256. Tokens are attached
only to `api.github.com` endpoints under `minseogiiiii`; curl is forced
to HTTPS on the entire redirect chain and strips Authorization on redirects
to other hosts.

A plain Release browser URL, local `/mnt/data` path, `http://` URL,
or Google Drive share page is **not** a substitute for the API tag URL.

## 5. Trigger and inspect mandatory gates

Run on `remediation/q14-import-order-20261122`:

- `Q14 historical real-fixture 31 of 31 gate (PRIVATE SOURCE REQUIRED)`.
  Acceptance: **31 tests, 31 PASS, 0 FAIL, 0 SKIP** with real archived
  original fixtures and a matching frozen frontend source.
- `Q15 byte-identical shipped v36 HTTP Chromium fail-closed gate`.
  Acceptance: original browser assets **3/3 exact hashes**, real loopback
  HTTP Chromium DOM, both BTC/ETH no-source fail-closed, no JS exceptions.

Only safe summaries of tests can be uploaded as GitHub artifacts.
The original private ZIP and private archived market data must never be
uploaded to a public GitHub repository, workflow artifact, or PR.

## Unchanged release verdict

These two successes, even after confirmation, **do not** authorize
deployment, main/integration merge, or trading. Persistent Linux 24h
real-source and fault recovery, direct LIVE evidence, operator signoff
and rollback remain mandatory. Keep Draft PR #7 in draft.
