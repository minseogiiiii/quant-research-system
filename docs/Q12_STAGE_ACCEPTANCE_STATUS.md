# Q12 staging acceptance and CI triage

Status: **DIAGNOSTIC COMPLETE / FULL VALIDATION FAIL / NO MERGE APPROVAL**.

This note records GitHub observations from 2026-11-21 ET (2026-11-22 UTC).
It supersedes the earlier claim that the 30-minute public-source observation
had not succeeded, but it does not supersede pinned source hashes or the
full release acceptance process.

## Verified executable evidence

- Frozen Q7/Q8 staging source lineage remains under Draft PR #6, targeting
  `integration/btc-eth-unified-terminal`. Neither `integration` nor `main`
  was merged in this work.
- Real compiled Q7 React DOM plus Coinbase and Funnest browser datafeed:
  [Actions 44878542711](https://github.com/minseogiiiii/quant-research-system/actions/runs/44878542711),
  **SUCCESS** on `a917b0f6d6cff8d25d54a0a718fdf716b411f12e`.
  This did NOT retest the previously shipped v36 frozen bundle.
- 1800-second actual public Coinbase single-source observation:
  [Actions 44807951403](https://github.com/minseogiiiii/quant-research-system/actions/runs/44807951403),
  **SUCCESS** on `b7e5fc2f62a5fc1a937d878e2f10218e03fc8594`.
  This included one source connection, archived capture audit and an eight-client
  SSE fanout over a **240-second** configured observation, not 1800 seconds
  of each SSE client and not a persistent Linux host.
- Offline Q11 992/992 Python and 31/31 Node full-fixture tests are historical
  local-run evidence. They have NOT been reproduced with zero skips in
  GitHub's promotion workflow.

## Q12 Ruff diagnostic implementation

- Script: `.github/scripts/q12_ruff_compare.py`
- Tests: `.github/scripts/test_q12_ruff_compare.py`
- Workflow: `.github/workflows/q7_ruff_delta_diagnostic.yml`
- [Actions 44980445293](https://github.com/minseogiiiii/quant-research-system/actions/runs/44980445293):
  **SUCCESS**, all four diagnostic unit tests PASS.
- [Machine-readable Ruff delta artifact](https://github.com/minseogiiiii/quant-research-system/actions/runs/44980445293/artifacts/14737835866).

The comparator runs the same pinned Ruff 0.15.22 on frozen R5 and staging,
counts source-line/code/message fingerprints as multisets, and remains robust
to line-number shifts. Counts are not suppressed or waived. The job's green
status indicates that the diagnostic ran successfully, NOT that full Ruff
validation passed.

| Metric | Count |
| --- | ---: |
| R5 base lint violations | 1,274 |
| Staged lint violations | 4,175 |
| Introduced vs R5 | 2,952 |
| Resolved vs R5 | 51 |
| Introduced on files absent from R5 | 2,887 |
| Introduced on R5-tracked files | 65 |
| Staged BTC/ETH source lint violations | 4,172 |

Largest categories among the introduced violations:

| Rule | Count |
| --- | ---: |
| E501 (line too long) | 1,399 |
| E701 (multiple statements on one line, colon) | 507 |
| E702 (multiple statements on one line, semicolon) | 493 |
| I001 (import sorting) | 230 |
| E402 (module import not at top) | 128 |
| F401 (unused import) | 99 |

These numbers are for the exact diagnostic commit
`499c0b87fd91240ea546df4fc4a93ca3ae00742f`, not a perpetual claim
about future heads. The separate root Validation remains red. Do not
rewrite the 278 pinned product files with a bulk formatter without an
independent source manifest, test, and release qualification update.

## Three historical Node inputs missing in GitHub promotion

The full promotion job has **28 PASS / 0 FAIL / 3 SKIP**, then deliberately
fails its `# skipped 0` assertion. Skips require these exact inputs:

1. `V34_L2_PROJECTION`: pinned real v34.1 L2 projection JSON.
2. `Q5_REAL_STATE_JSONL`: verified Coinbase BTC/ETH state JSONL.
3. `Q4_REAL_STATE_JSON`: genuine archived BTC/ETH state JSON.

The raw inputs and credentials must not be committed to the public repository
or uploaded as public Actions artifacts. Provide them through an authorized
private runner or controlled private storage, verify SHA-256 and schema before
executing tests, record safe hashes and aggregate pass/fail counts, and keep
all three skips blocking until actual inputs pass. Never synthesize an
archive solely to satisfy these historical real-source tests.

## Next required gates

1. Maintain the old Ruff baseline and explicitly remediate introduced errors,
   starting with the 65 on R5-tracked files. Review all source changes and
   update any affected frozen hashes; do not silently suppress lint rules.
2. Obtain the three immutable historical inputs securely and rerun the full
   Node gate with zero skips alongside the complete applicable Python suite.
3. Revalidate the **existing shipped v36 bundle** separately from the Q7
   rebuilt preview.
4. Perform persistent Linux 24-hour normal operation plus separate
   fault-recovery, disk-capacity, stream-backpressure and archive-restoration
   certification.
5. Require a fresh review of rollback, all validation checks and immutable
   evidence before any read-only merge. Paper Trading and real orders are
   separate future authorizations.

Final verdict: `Q12_RUFF_DELTA_VERIFIED / VALIDATION_FAIL /
PRODUCTION_PENDING / REAL_ORDERING_DISABLED / NO_CONFIRMED_EXECUTABLE_ALPHA`.
