# Q29.1 exact Artifact-ID evidence transport

Q29 run 45300926987 failed immediately after successfully uploading its
180s source evidence. The name-based downloader reported "Artifact not
found". The underlying reason for that lookup failure is not established.
The uploaded artifact 14879266244 was retrieved independently; ZIP
SHA-256 b276b83212d9fa8293a045561582e2a595a5a05de26bced0823bcc88d9dddb4c
matches GitHub's upload digest; all six receipt hashes and byte counts
match its evidence manifest. This does not retroactively approve the
failed Q29 pipeline or substitute old evidence for a new run.

The new read-only REST transport requires the actual immutable artifact
ID and upload digest, exact run ID and source SHA, expected artifact name
and expiry status, size, ZIP hash, exact flat member list and independently
held manifest hash. It sends the GitHub token only to api.github.com;
the signed HTTPS blob redirect is requested without the token. Temporary
404/408/429/5xx or network errors have six bounded attempts; identity,
content and schema failures are not retried. Extraction uses a fresh
staging directory, not extractall. Only a validated directory is published.
No raw market data is included.

Standalone smoke workflow 45307537059 proved eight offline transport
contracts and eight original evidence contracts, then successfully retrieved
synthetic artifact 14880758918 by ID both on the upload runner and a
separate runner. This synthetic receipt is NOT market data.

The final exact source commit must itself pass the standalone smoke before
a one-file arm commit can start Q29.1. The pipeline keeps all Q22-Q29
regressions, the original 278 pinned blobs, 180s source, 1800s source,
18000s source and the original strict continuity, resource, independent
replay and no-order conditions. All same-run and preceding-stage downloads
use ID+ZIP digest and independent manifest verification. No automatic
rerun, no main merge, no 24h or execution authorization.
