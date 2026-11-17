# v35.1 Unified Coinbase historical-replay terminal: audited integration candidate

**Status: ENGINEERING CANDIDATE, not production deployment.** Frozen v34.1 parent SHA-256: `8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8`. The original standalone v35 candidate ZIP is not currently recovered. This is a minimal compatible alternative, not a byte-for-byte restoration.

## Integrated source tree

- `backend/v35_archive_ingress.py`: existing externally pinned read-only archive gate. No source can be promoted to LIVE or trading.
- `backend/v35_replay_service.py`: reuses frozen v23 Book, v34 ten-level Decimal depth, guarded file descriptor / rehash on replay. Reconstructs BTC/ETH L2, spread, Microprice, connection-cumulative OFI, bounded, receiver-asof state and completed 60-second receiver-time OHLCV from **observed market_trades updates only**. Startup historical trade snapshots are excluded from new flow.
- `backend/v35_unified_gateway.py`: same-origin, loopback, read-only `/api/v35/{health,snapshot,history,candles,stream}`; bounded SSE, Last-Event-ID resume, resync errors; inherits existing v34 bridge historical endpoints.
- `backend/v35_1_frontend_integrity.py`: SHA-pinned React/Vite/Funnest candidate assets served at `/research/v35.1` alongside untouched historical `/research` and `/terminal`.
- `frontend/src/ArchiveReplayPanel.tsx`: verifies same-source certificates and candles, displays Funnest OHLCV only after receiver-time bucket completion, ten-level book, Microprice, OFI, replay controls and SSE diagnostics.
- Seven missing original v34.1 backend dependency files restored as code only. No historical market JSONL/raw chunks were committed to public GitHub.

## Independently executed test evidence

- Local actual clean Coinbase 120s archive: **5,177** source messages admitted and replayed; independent dictionary book ten-level parity for BTC and ETH.
- Local reconnect/quarantined 120s archive: **5,164** source messages rejected before publishing trusted state.
- Local one-hour authentic archive: **152,069** messages, **41** original chunks; **59 BTC + 59 ETH completed 1m trade-update candles**; 27.0337s run (~5,625 msgs/sec) on this environment. Final replay digest: `1b11d2e54057cb277e162c1f5f582b7a31c5dcc0237ede2f21a80cbd3f750bc3`.
- Independent dictionary 1h L2 replay agrees on BTC/ETH **best bid, best ask and all top ten bid and ask price/quantity levels**.
- Final standalone source-tree Python full-suite run **831 passed, 137 subtests passed, 389.67 seconds, exit code 0**. New unit/HTTP/candle integration suite: **22 passed**. Historical v30-v34.1 targeted regression: **121 passed, 49 subtests passed**.
- GitHub [frontend build and immutable manifest validation](https://github.com/minseogiiiii/quant-research-system/actions/runs/43994188650): PASS.
- GitHub [Chromium browser DOM contract test](https://github.com/minseogiiiii/quant-research-system/actions/runs/43994188632): PASS with **synthetic, plainly labeled test fixture only**, including archive chart, BTC/ETH switching, replay, SSE and quarantine display. NOT a claim of a browser validating the original private Coinbase archive.
- GitHub [backend import and loopback/no-order smoke](https://github.com/minseogiiiii/quant-research-system/actions/runs/43994204679): PASS without private archives.
- v34.1 file manifest ancestry: 764 parent entries, 763 byte-identical, 1 intentional `frontend/src/unified.tsx` edit; **all 210 existing `backend/data` files unchanged**.

## Strict outstanding gates

- **BLOCKED**: direct real-archive-to-Chromium localhost browser E2E in current local execution environment due `ERR_BLOCKED_BY_ADMINISTRATOR`. GitHub browser CI proves UI behavior only using a synthetic fixture.
- **UNVERIFIED**: original standalone v35 ZIP hash/recovery, fresh source-admitted live capture, 24h uninterrupted host operation, long-running SSE with live disconnects, durable operational monitoring, independent original Feature/Label PIT, blind OOS, simulated fill realism and profitable alpha.
- **NOT ENABLED**: real brokerage, exchange order writes, leverage, credentials or authorized live execution.
- Gateway is finite archive replay and does not implement continuous Coinbase stream or background feed monitoring. Silence handling beyond the tested finite replay/EOF behavior requires further operational tests.
- The v34.1 and frozen raw archives are immutable, independent original cohort identity retained. `main` unchanged.

**Verdict: `NO CONFIRMED EXECUTABLE ALPHA`.**


## 2026-11-16 v35.1r1 bounded gateway resilience increment

Engineering increment, NOT a production release and NOT a live exchange-feed certification.

- Preserved the original v35.1 ZIP SHA-256 `5d6e437eb59e8e0e967b98d9d47a3d1e2e833e5ac4aa236a1f9868648fcd8969` and v34.1 parent `8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8`. New standalone candidate is separately delivered, never overwrites these originals.
- Read-only `backend/v35_unified_gateway.py` now assigns a three-second socket read/write timeout to accepted peers, retains the 8-client BoundedSemaphore and adds locked, per-server counters via `/api/v35/metrics`: accepted/rejected connections, current/peak active connections, finite SSE started/completed/aborted and RESYNC_REQUIRED.
- The SSE endpoint remains finite, pre-composed, receiver-clock `ARCHIVE_REPLAY` only. No live SSE subscription engine or exchange write path has been added; `sse_queue_depth=0` means no persistent stream queue exists, not that a continuous live feed was load-certified.
- Added `tests/test_v35_2_transport_contract.py` and executed its **4 synthetic transport-only cases** through [GitHub backend CI run 44000049087](https://github.com/minseogiiiii/quant-research-system/actions/runs/44000049087), success on `a9aebc0493ca7356da7b6444efe4dc5e2ee6d800`. CI additionally passed Python compilation, import safety and limited order-write marker scans.
- Separate full-source local tests used actual SHA-pinned Coinbase 120-second original archive: 7 new gateway/concurrency/fault cases passed; existing 22 v35.1 integration cases passed. Frozen final source tree plus new transport cases: **154 passed**, exit code 0, including v30-v34.1 related regressions. Independently extracted full candidate: the same 154 passed. **The historical full 831-case Python suite was NOT rerun** in this increment.
- Real archived-source browser E2E harness checked 8 same-host HTTP/API paths and source identities with **5,177 original messages**. Local Chromium returned `ERR_BLOCKED_BY_ADMINISTRATOR`; actual archive-to-DOM chart values remain **BLOCKED**, not PASS. Existing synthetic browser CI cannot fill this evidence gap. Reproducible harness and machine-readable BLOCKED report are included in the standalone candidate.
- One-host sample benchmark: 120-second archive replay 1.1261 seconds; 100 snapshot HTTP requests at concurrency 4: p50 3.115 ms, p95 5.131 ms, max 18.736 ms; max process RSS 138.11 MiB. Not a production latency SLA or 24-hour reliability test.
- All **210 historical `backend/data`** files remain SHA-identical to v34.1. Main branch unmodified. Research verdict: `NO CONFIRMED EXECUTABLE ALPHA`.

**Remaining high-priority gates:** actual market-archive browser E2E in an authorized Chromium environment, sustained multi-client/slow-reader soak under a durable host, complete final 831-suite regression if required for promotion, actual fresh Coinbase admission, source stall monitoring, operational risk review and independent OOS.
