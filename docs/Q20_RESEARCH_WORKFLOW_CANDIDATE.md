# Q20: Observe → Hypothesize → Check Evidence (read-only research workflow)

**Engineering candidate only. Not deployed, promoted, merged or approved.**
This development increment is an isolated frontend candidate based on Q17/Q18/Q19.
Original Q14 product-source and original shipped v36/v37 asset manifests must remain frozen.

## User workflow in the Q20 candidate preview

1. Observe a BTC-USD or ETH-USD archived state under the existing unified terminal. The existing Replay, chart, trade tape, full L2, recovery and Factor Lab already share the selected receiver-asof grid.
2. Open **Research Workflow** to capture the exact product, receiver-asof timestamp, immutable source manifest SHA and v30 projection SHA.
3. Create an **exploratory hypothesis draft** using OFI, OBI or microprice displacement; a bounded threshold and an explicit up/down expectation for the next **60 seconds**. User choice does not constitute a preregistration.
4. Click **Check evidence readiness**, which performs a read-only HTTP GET to the existing `/api/v37/factor-lab` research section at the exact product/asof.
5. Reject wrong instrument/time/hash/report, non-read-only payload, any forged predictive effect/PnL, incorrect horizon or blind OOS promotion. Admit only genuine historically available *descriptive counts* from the already-explored archive. The result is always **INSUFFICIENT_EVIDENCE** until a future, separately reviewed research engine and untouched data justify a new contract.
6. Export JSON containing the draft hypothesis, source/projection fingerprint and the admission gate result. Export is a local researcher record, not a registration ledger or model artifact.

LIVE v36 remains a separately labeled real-time observation route. The archived notebook must not splice that state with historical outcomes. Only BTC/ETH Coinbase public spot are supported.

## No false alpha, lookahead or release claims

- No broker or trading routes, API credentials, execution, predictions, model fitting, after-cost PnL, realized fills or OOS verdicts are created by Q20.
- Current Q7 Factor Lab is one already-explored archived cohort. Historically visible future-label *counts* do not provide an independent test; no forward-return values are exported to the notebook.
- This is the **evidence admission check**, not the final statistical hypothesis test. A genuine controlled out-of-sample experiment will need new untouched multi-day captures, a prespecified trial budget, causal feature and label availability, train/test boundaries, cost estimates and independent audit.
- Wrong source/session/as-of, changed active instrument, stale fetch result, invalid numeric threshold or suspect Factor Lab payload must fail closed.
- Export has `is_verified_oos_result: false`, `research_artifact_only: true`, and no order authorization.

## Immutable deployment boundaries

- New Q20 React component and pure research protocol are mounted in the new **Research Workflow** tab of source `unified.tsx`, and included in a **separate unpromoted Vite candidate build**.
- The existing pinned `frontend/v37_dist_integrity.json`, historical v36/v37 original bundles, `v37_frontend_integrity.py`, collector and all backend modules remain untouched.
- **Current served `/research/v37` will not expose this new tab** until a new versioned Q20/v38 public bundle is independently SHA-reviewed, pin-approved, routed and tested against exact historical and LIVE source contracts.
- Strictly keep Q20 separate from Q18/Q19 in review, since those workflow gates validate Q14 product source, not this modified UI.
- Treat the Q20 CI artifact as a preview-only compiled asset, NOT a released site.

## Remaining checkpoints before a public read-only service

1. Q18 thirty-minute actual public-source + segmented SSE gate must complete and be inspected. Q19 real Coinbase candidate preview already passed separately.
2. Source-pinned real HTTP Chromium Q20 browser workflow testing including stale product/as-of response and real archived fixture identity. No production qualification via simulated prices.
3. Persistent Linux host 24-hour capture, process restart/kill/disk/backpressure faults, independent archive recovery and observed resource limits.
4. Versioned Q20 static bundle manifest, operator-reviewed routing, rollback, access restrictions, and approved read-only release.
5. Only after continuous new independent market captures, add an offline OOS experiment engine as a distinct reviewed research release.

**Policy:** Draft PR only, do not merge to Q14/staging/integration/main. Real and paper order APIs remain disabled.
