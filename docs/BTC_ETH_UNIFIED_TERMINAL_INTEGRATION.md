# BTC/ETH Unified Terminal Integration

This branch is an **isolated integration staging branch**, created from the verified GitHub main commit `a9edeede7a3c5a57ea3c91bcd5e32a7470fe67bd`. Do not merge into `main` without the standalone v29 source and a reproducible CI validation.

## Actual source baseline

- The authoritative development base is **standalone v29**, not this branch's original HEAD.
- v29 full ZIP SHA-256: `853b2550f2612a66b7d8a263cb67d474ee77dd4636d743ad5163a11081a2752d`.
- Original v29 raw market source archives, tests, the v24 synthetic paper OMS and previous negative research outcomes are preserved in the companion v30 integration artifact, not uploaded here as large blobs.
- Coinbase public spot instruments: `BTC-USD`, `ETH-USD`, **not** BTCUSDT/ETHUSDT perpetual futures.

## Implemented Phase A/B local vertical slice

The v30 source package introduces:

- `backend/v30_unified_projection.py`: runs the existing two independent public-source auditors, binds frozen v21/v23 receiver-asof diagnostics to the verified source manifest, and generates bounded 5-minute **receiver-time observed trade update** bars and trade tape.
- `backend/v30_unified_api.py` and a narrow adaptation of `backend/bridge.py`: read-only `/api/v30/catalog`, `/api/v30/series`, `/api/v30/book` endpoints, with whitelist validation, pagination, time cutoffs and SHA-256 gates.
- `frontend/src/unified.tsx`: keeps the existing React/Funnest chart foundation.
- `frontend/standalone`: a dependency-free local browser view of the **same readonly API**, for hosts without npm registry access.
- `tests/test_v30_unified_terminal.py`: HTTP and source integrity adversarial tests.

**GitHub migration status:** source staging is partially complete: v30 projector, readonly API, bridge, the existing React/Funnest frontend entry and datafeed, the unified component, standalone UI, TypeScript contracts and new tests were committed under `btc_eth_terminal/` on this integration branch and their Git blob identities were checked against local fixed-tree code. The remaining standalone v29 dependencies, immutable source archives and generated projection are **NOT** imported into GitHub. Consequently this branch is **not independently buildable as the complete v30 application**. Use the downloadable full v30 ZIP to run locally. No raw archives or secrets were uploaded. Do not mistake the branch creation for completion of migration.

## Research safeguards

- Historical source internal replay is not independent exchange truth.
- Observer-time OHLC is not official Coinbase exchange OHLCV.
- Feature/Label origin, historical availability and OOS remain unapproved.
- No live collector is started by the UI; no live orders or credentials are connected.
- v24 paper OMS remains synthetic fixture only.
- Exact state: `NO CONFIRMED EXECUTABLE ALPHA`.

## Next PR requirements

1. Import reviewed v29 **code only**, preserving existing research history and independent auditors.
2. Import v30 source adapters and frontend source files, with no large raw archives or secrets.
3. Supply immutable source SHA manifests and safe artifact retrieval path separately.
4. Pass original and new CI regression, real Funnest production browser build, API/React contract checks, and approval review.
5. Merge into main only through an explicit separate decision.

## v31 verified historical terminal

- Full standalone v31 ZIP is the runnable distribution. GitHub branch is still a partial staged code mirror and must not be presented as a complete backend checkout.
- Fixed UI source commit: `d8555d8e25e7c44b031278801ee305a43df237d3`.
- Real GitHub Actions Funnest/TypeScript/Vite production build: https://github.com/minseogiiiii/quant-research-system/actions/runs/43794896707
- Actual compiled frontend can now be served directly by the localhost-only Python bridge at `http://127.0.0.1:8765/research` when extracted from the complete v31 distribution, without running npm.
- Fixes verified: out-of-order BTC/ETH responses, pending as-of replay data exposure, incomplete five-minute chart intervals, separate legacy CSV source lineage.
- Fixed-tree 734/734 Python tests passed, 26/26 mediated Chromium browser checks passed, no JavaScript page exceptions; independently extracted v31 distribution passed 54 critical tests and the bounded API/static serving contracts.
- Existing v30 `backend/data` 208 source files remain byte-identical; no independent blind OOS or confirmed alpha, no live order path.
- Browser network navigation is blocked in the development host, so direct browser network E2E is not claimed. Actual backend HTTP contracts and mediated real-source Chromium DOM were tested separately.
- The staging branch does not include original raw data, the full v29 runtime, an npm lockfile, or a deployable brokerage adapter. No auto-merge into main.
