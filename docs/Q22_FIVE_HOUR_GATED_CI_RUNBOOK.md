# Q22: Q18-success-gated 5h GitHub Actions source qualification

Status: **CANDIDATE / NOT FIVE-HOUR VERIFIED** until the full Q22 workflow,
private archive integrity, independently replayed final books, and public
summary evidence have all completed successfully.

## Safeguards before spending five hours of CI runtime

1. The Q18 **specific** GitHub Actions run ID `45001633746`, expected HEAD
   `d832110e8e3484e82fafa2533270a6d38bee0865`, must finish with
   GitHub conclusion `success`. In progress, cancelled, skipped, timed out
   or failed must **never** launch Q22. The expected Q18 source manifest
   and approved runner branch must match exactly.
2. Q22 source starts as a child of Q21, not a rewrite. Its additional files
   cannot change any of the 278 Q14 candidate Git blobs, production v36
   collector, archived v37 UI, 24h observer, or order-execution paths.
3. The separate Q22 short preflight workflow runs Ruff, Python
   compilation, 16 adversarial Q22 contracts, existing source/SSE/storage
   fault tests, Linux CLI contract tests, and an **actual 180-second**
   Coinbase public-source / 60-second eight-client SSE smoke plus full
   independent replay on an ephemeral Linux runner. No mock is labeled LIVE.
4. Only after that latest preflight run is `success`, the Q22 arm file is
   committed. The five-hour workflow is triggered **only by this arm file**
   and validates the exact previously tested commit. Its short gate polls
   Q18 status and fails closed if Q18 does not pass before the bounded wait.
   No action is needed after Q18 passes if the arm-triggered gated run is
   waiting successfully.
5. 5h job checks workspace capacity *before* spawning the observer, and
   checks the first five minutes of genuine raw archive volume. Unsafe
   projected 5h volume or remaining disk aborts the collector immediately.

## Actual five-hour phase

- One unchanged BTC/ETH Coinbase public market source records for **18,000s**.
- Q22 CI-only observer reuses the frozen `btc_eth_multiday_v20.mjs`, v36
  `LiveMarketEngine`, read-only gateway, original host-PID-verified tap,
  original archive format and Unix-only private operator monitor. It does
  **not** weaken the production 24h `--durable-operator-mode`.
- CI-specific strict limits: 4,000,000,000 raw text bytes,
  6,000,000,000 archive bytes, 4GiB per-write disk reserve, at least
  700MB additional disk margin at start and 500MB throughout.
- Every 15 seconds: verify a single source session, strictly increasing
  message counters, no reconnect/gaps, both product snapshots, fresh data
  (<=15s), valid private Unix monitor, unchanged Node process PID, combined
  RSS <=2GiB, open FDs <=512, sufficient remaining disk.
- **24 independent, genuine eight-client SSE windows of 240 seconds** each,
  spaced every 720 seconds from approximately minute 5.5. Aggregate
  nominal SSE load is 96 minutes across the 5h source. This is *not*
  one uninterrupted five-hour SSE connection. Each validates initial
  snapshots, strictly increasing source-bound cursors, no fast-client
  unexpected resync, exactly eight simultaneous connections, source
  identity, bounded errors and slot drain before the next window.
- Any failure stops the collector early; no failed source is promoted.
  The CI job has a strict timeout under GitHub's six-hour job limit.

## Independent full-archive phase (same ephemeral runner)

Upon a clean source exit, in the same job and before deleting private data:

1. Verify observer output, complete tap ingestion, single source
   connection, no injected faults, exact 18,000s duration and >=17,995s
   monotonic source span.
2. Verify externally observed capture-output SHA against the finalized
   manifest, every sealed chunk hash, source sequence, schema, no
   unmanifested finalized files and no pending partial.
3. Run the original primary final-book replay with explicit **process-only**
   large-source read bounds, then run the separately implemented
   `independent_soak_replay` on the same immutable captured bytes.
4. Compare complete counts, BTC/ETH final bid/ask and book depth between
   the two replay interpretations. Hash-check manifest again after replay.
5. Publish only size-bounded diagnostics, checksums, resource and SSE
   summaries as Action artifacts. Do **not** upload private raw JSONL
   chunks or observer log files.

The primary result is a **five-hour ephemeral Engineering Acceptance** gate,
not 24-hour persistent host certification, external venue authentication,
statistical OOS alpha, after-cost PnL or permission to place any real orders.

## Failure handling and runtime accounting

The short gate and preflight have separate runner jobs. The five-hour job
has no automatic retries. A failed five-minute sample or SSE window stops
the run early. A full archive that fails validation remains a failure
even if collection and live SSE were healthy.

The private capture is removed from the runner at job exit, regardless of
success or failure. The publicly retained bounded Q22 summary is sufficient
for failure classification but not for replaying the full market data.
If needed, a separate privacy-reviewed and explicitly provisioned secure
storage process must be designed before long-term raw retention.

Operationally, do not start this long test if standard ephemeral runner
disk is under the required free-space budget. Prefer failure *before*
the five-hour collector starts to lowering storage reservations.
