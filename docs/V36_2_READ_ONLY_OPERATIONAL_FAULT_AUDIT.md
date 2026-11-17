# v36.2 Read-Only Operational Fault Verification

**Status: engineering increment verified against R3, not Production Ready.** The frozen v36.1 R3 ZIP was not overwritten, `main` not modified, no original Coinbase archives committed, no order-writing interface enabled.

## Source basis

- Parent R3 full standalone ZIP SHA-256: `a4b5d3eb4403ecae18584252a37bb568ca03954c99c8d0957dcb38ee155b058d`.
- Integrated code/test commit: `2a0666b7644432999d0a6be838415827abe43ef2`.
- Git-blobs of final changed backend files independently matched against the local patch: `v36_live_engine.py` `0f6e3b24b2da5ff1b4da88b67a683768cf9ae81f`, `v36_live_gateway.py` `98bba8f69f0364d8af9d731ebb7cd242dd20938c`.

## Three observed deficiencies and minimal fixes

1. **L2 silence hidden by wall-clock rollback.** The R3 source used only wall time for per-product L2 freshness. A synthetic test simulating 6.001 seconds of product silence while unrelated heartbeats remained current and system time rolled backwards yielded `HEALTHY` on unmodified R3. The fix retains wall-clock guard and additionally uses per-product local monotonic ingest timestamps, reset at a new source connection. Any anomaly fails closed.
2. **Mixed-session initial SSE handshake.** R3 separately retrieved health and the initial snapshot. A deterministic synthetic source-rotation race yielded source event session A and snapshot session B in the same 200 SSE response. `stream_initial(product)` now captures source identity and initial quote under one engine lock.
3. **No v36-specific operational mode metrics.** The historical v35 server counters are tagged `ARCHIVE_REPLAY`; `/api/v36/metrics` previously returned 404. A dedicated read-only endpoint now reports the actual source mode/status, source-healthy flag, bounded resources and SSE counters, retaining the historical v35 metrics contract unchanged.

## Actual executed validation

- **Red-before-green:** Two new race tests failed against original R3; after patch both pass. Two more telemetry tests failed HTTP 404 before the fix; after patch both pass.
- **Existing selected regression:** 43 passed, 0 failed, 0 errors (original R3 with only two updated backend Python modules).
- **New adversarial contracts:** 4 passed, 0 failed, 0 errors.
- **Real original archival parity, not LIVE:** SHA-checked 5,177 original Coinbase source messages. Independently ran original R3 and v36.2 engines on precisely the same stream; compared BTC 112 and ETH 116 new state projections, final state versions 227/228 and completed candles, with exact feature and quote equality.
- **Local bounded archival SSE:** 6 concurrent readers, one intentionally slow reader, 150 newly delivered and ordered state frames (25/client), peak 7 of 8 permitted clients, 17 seconds of accelerated genuine archive input, observed peak Python RSS ~156 MiB. On deliberate transport loss, `/api/v36/snapshot` no longer published a trusted quote and both product subscriptions returned HTTP 409. New mode-correct `/api/v36/metrics` reported `HEALTHY` before, `RECONNECTING` after. **Not 17 seconds of newly received exchange data.**
- **[Latest backend CI PASS, 44075571575](https://github.com/minseogiiiii/quant-research-system/actions/runs/44075571575)**, on the integration code and test commit, including new race and metrics regression steps.
- **[Independent real LIVE Chrome CI PASS, 44075493417](https://github.com/minseogiiiii/quant-research-system/actions/runs/44075493417)**, at code commit `cd187bcec872ef8cce1400917418bb74f0ae03c2`: 10,701 authentic Coinbase messages captured, actual live Chrome 171.896 s, 74 distinct BTC and 69 distinct ETH exact-version DOM states, 2 matching Funnest `getBars` completed candles per product, and 3 independently reconstructed post-capture completed candles per product. The final HEAD after this run adds only test/CI changes, not changes to the live engine beyond `cd187b`.
- Standalone small patch ZIP: SHA-256 `9d71b66074c6ca55d4f37d1f58954a5700f43308d611e8ef67b69b7c94140f9f`, with two GitHub blob-identical backend files, portable local test variants, logs summary and SHA file manifest. Independent extraction: 7 manifest-listed files verified, 4 tests passed.

## Strict unverified items

This is **not** a 24-hour continuous LIVE operational certification. Full unfiltered integration regression on a combined v36.2 standalone package, independent full-package rebuilding of R3 manifest after patching, multi-hour remote-source gap/reconnect with deliberate DNS/TLS interruption, durable-host disk-full/SIGKILL and persistent data recovery, full production observability and browser canvas-pixel equivalence remain **not verified**. No executable alpha is confirmed. Trading remains disabled.

**Research verdict: `NO CONFIRMED EXECUTABLE ALPHA`.**
