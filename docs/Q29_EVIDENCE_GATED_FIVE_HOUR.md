# Q29 evidence-gated five-hour public-source engineering acceptance

## Prior evidence and known defect

Q28 run 45268172790 at 780c9757cd76e086783f7234c6f3591560e879af
successfully completed a genuine 1800-second capture, 3 eight-client SSE
windows and two independent book replays. The actual final archive held
75,592 messages in 22 chunks (172,209,862 bytes), one source connection,
zero reconnects, 1806 application heartbeats and zero guard interventions.
Its Actions log establishes those observations, but no downloadable JSON
artifact was retained: the upload step pointed to q27_30m_public rather
than q28_30m_public and missing files were only warnings. Do not
reconstruct missing receipts or represent the old run as a durable bundle.

## Q28.1 evidence remediation

- Correct the legacy Q28 upload directory and make missing output an error.
- The new Q29 evidence gate is a separate read-only CI helper. It checks
  six named JSON receipts with expected schemas, no extra files, regular
  single-link files, bounded sizes, unique JSON keys and finite numbers.
- Independently reconcile duration, monitored session and collector PID,
  exact SSE window count and elapsed time, resource ceilings, archive
  messages, independent BTC/ETH book digests, Q24/Q27 counts, clean trace
  completion, no Q27 intervention and Q28 first-close classification.
- Seal a manifest containing each file's byte count and SHA-256, source
  Git SHA, GitHub run ID, first attempt, and duration. A missing, modified,
  stale or substituted receipt fails the job. This binds evidence to the
  engineering run; it is not an exchange signature.
- Upload the exact verified directory with missing files treated as error,
  download it from Actions storage into a distinct directory, verify the
  expected manifest digest and every file again. Store synthetic regression
  results separately from market receipts. Never upload raw archive,
  credentials, collector stdout/stderr or private WebSocket JSONL.
- Failure diagnostics are a separate artifact and cannot be used to
  advance acceptance, even if a partial diagnostic is valid.

## Q29 one-shot progression

The single arm file specifies the preceding source commit, read-only
authorization and exactly [180, 1800, 18000] seconds. Only an arm-only
commit on the named Q29 branch triggers this workflow. Reruns of the same
attempt are rejected. Every job uses the same Git SHA and rechecks the
original 278 pinned product blobs. No main merge or real/paper order
permission is changed.

1. Run all inherited Q22-Q28 regressions and new adversarial Q29 evidence
   tests. Perform genuine 180s public Coinbase smoke with all Q24/Q25/Q27/Q28
   guards and one eight-client SSE window; two independent final-book
   replays; seal/upload/download/verify six live receipts plus manifest.
2. Re-download and verify the 180s artifact on a fresh runner. Capture a
   new 1800s source with three 120s eight-client SSE windows. Repeat
   archive audit, independent replay and artifact roundtrip.
3. Only after both preceding jobs succeed and their actual artifacts
   verify again on the new runner, perform 18000s capture with the existing
   24 x 240s eight-client SSE windows, 15s health/resource samples, original
   storage budgets and Q24/Q25/Q27/Q28 guards. Require stable source and
   PID, zero gaps/reconnects/interventions, at least 17400s sample span,
   two independent replays, and a successful final artifact roundtrip.
4. A failure or skipped gate blocks downstream stages. A CI runner failure
   is not automatically an exchange/network failure. No automatic rerun,
   no relaxation of continuity, no source stitching.

## Scope boundaries

Even a successful five-hour Actions run proves only ephemeral-host
engineering acceptance for this commit and this observed interval.
It does not prove continuous 24-hour persistent host service, browser DOM
parity, signed exchange provenance, profitability, paper-trading readiness
or authority to execute orders. The next separately reviewed stage is
persistent Linux deployment and a 24-hour acceptance run, followed by
read-only integration review; paper trading comes only afterward.
