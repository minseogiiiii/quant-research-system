# Q14 read-only release qualification and outstanding evidence

**Status: engineering candidate, NOT release approved.**
Scope: BTC/ETH research data collection, visualization, offline research.
No paper/live order authorization and no confirmed executable alpha.

## Exact candidate source

- Q14 source snapshot: `ca44e5ee0dc636d8d30446db839ed6d6cb2af091`.
- Candidate-only source manifest: `.github/release-evidence/q14_candidate_source_git_blobs.json`.
- Original 278-source historical pin document is unchanged:
  `.github/release-evidence/q7_q8_source_git_blobs.json`.
- Independently confirmed 278 source blobs, 227 changed vs frozen Q7/Q8,
  51 unchanged. Never copy a Q14 hash into the original Q7 manifest.
- Successor commits after the Q14 source snapshot have changed
  qualification workflows and evidence metadata, not product source.
  Confirm that comparison again before any approval.

## Independent evidence from November 21, 2026 ET

| Gate | Evidence | Verdict |
| --- | --- | --- |
| Full repository 18-stage `./scripts/check.sh`, Ruff zero, mypy, root 534 pytest | [Actions 44986988899](https://github.com/minseogiiiii/quant-research-system/actions/runs/44986988899) | PASS |
| Independent original/candidate 278 source blob audit | [Actions 44987250499](https://github.com/minseogiiiii/quant-research-system/actions/runs/44987250499) | PASS |
| React/TypeScript pinned Funnest frontend, 3/3 v37 byte-identical assets, historical immutable v35.1 | [Actions 44987235754](https://github.com/minseogiiiii/quant-research-system/actions/runs/44987235754) | PASS |
| Node historical contracts with 3 genuine archive inputs on an isolated local restore | Local Q14 test log; NOT Github CI | 31/31 PASS, SKIP 0 |
| Public Github Node contract run without private archive | [Actions 44987235754](https://github.com/minseogiiiii/quant-research-system/actions/runs/44987235754) | 28 PASS, 3 SKIP. Not release-eligible |
| GitHub private genuine archived historical Node fixture run | [Actions 44984062133](https://github.com/minseogiiiii/quant-research-system/actions/runs/44984062133) | BLOCKED: missing `Q13_PRIVATE_RELEASE_URL` |
| 24-hour single-host Linux capture, archival independent replay, disk and multiple SSE client fault recovery | No passing current candidate evidence | NOT VERIFIED |
| Historical *shipped* v36 browser DOM E2E (not only rebuilt Q14 preview) | No passing current candidate browser DOM test | NOT VERIFIED |
| Q14 after-final-fix independent 992-test archive regression | Must pass against exact SHA-pinned candidate | Not approved until verified and recorded |

## Secure GitHub historical Node archive gate

The original full frozen archive exists outside public Git and must be treated
as an immutable input, never fabricated or re-recorded for this check.

- Archive file identity: `BTC_ETH_V36_3_R5_Q7_VERIFIED_ENGINEERING_CANDIDATE.zip`.
- ZIP SHA-256:
  `0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15`.
- Required historical inputs inside that ZIP:
  `backend/data/v34_l2_depth/projection.json`,
  `verification/v36_3_r5_q6/archive_final_snapshots.json`,
  `verification/v36_3_r5_q6/archive_state_228.jsonl`.
- The private test verifies the SHA-256 of the full ZIP and each fixture,
  BTC/ETH source coverage and staged React source parity before Node 31/31.
  All nonzero skips block the gate.

**One infrastructure action is required:** securely host the immutable original
ZIP in private HTTPS storage, then configure the GitHub repository *Actions
secret* `Q13_PRIVATE_RELEASE_URL` with an access-controlled download URL.
Do not paste the URL in an issue, commit, workflow input or chat. If using a
short-lived presigned URL, regenerate just before manually triggering the
workflow.

From GitHub Actions, manually run
`Q14 historical real-fixture 31 of 31 gate (PRIVATE SOURCE REQUIRED)`
on `remediation/q14-import-order-20261122`. The runner downloads privately,
validates and deletes the archive. No market data is uploaded as an artifact.
This gate cannot produce a green result until the private source is configured.

## Release decision

Require, for one exact candidate commit:

1. Ruff zero, complete root validation, full Python historical suite and
   genuine private Node 31/31 with zero skips.
2. Existing shipped v36 browser bundle E2E and all source hashes certified.
3. 24h persistent Linux operation plus independently witnessed restart,
   disk-full, websocket reconnect, SSE backpressure and replay recovery.
4. A reviewed candidate release manifest with original Q7 pins retained,
   complete diff and rollback procedure.
5. Explicit human approval limited to **read-only** promotion.

Until all are met: no merge to the protected integration/main branches,
no Production Ready designation, no paper/live exchange order writing.
