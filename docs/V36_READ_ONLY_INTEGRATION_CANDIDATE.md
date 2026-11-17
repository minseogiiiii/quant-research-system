# v36 BTC/ETH unified real-time research terminal: engineering candidate

## Critical verdict
**IMPLEMENTED:** Read-only v36 integrated source code, safe optional observer tap on the original Node collector, source-admission state machine, reused v23 Book and v35.1 feature contracts, bounded persistent HTTP/SSE, and React/Funnest workspace `/research/v36`. No real order transport is enabled.

**VERIFIED_WITH_REAL_ARCHIVE:** Previously archived authentic public Coinbase BTC-USD/ETH-USD data, not fresh live data. The original 120-second capture contains 5,177 messages; the one-hour source contains 152,069 messages in 41 chunks. The new engine processed the one-hour source in approximately 20.9–21.1 seconds (~7,222 messages/s) in this environment, emitted 59 completed 1-minute trade bars per product, and matched the original v35 Replay Service on final best bid/ask, spread, microprice, OFI, 10-level depth and level counts for both products (16 comparisons).

**VERIFIED_WITH_SYNTHETIC_FIXTURE:** Existing Node collector `scripts/btc_eth_multiday_v20.mjs` optional UNIX tap delivers the byte-identical JSONL envelope only after a successful archive append. Synthetic exchange replay tested shutdown, 8 original messages, checksum parity and no-live promotion. Source gap/stale handling, client resynchronization, bounded state history and malformed input were tested without live exchange access.

**VERIFIED_GITHUB_CI:** [v36 source and original collector tests](https://github.com/minseogiiiii/quant-research-system/actions/runs/44012038063), [TypeScript/Vite SHA-pinned frontend build](https://github.com/minseogiiiii/quant-research-system/actions/runs/44011888013), [legacy v35.1 synthetic Chromium regression](https://github.com/minseogiiiii/quant-research-system/actions/runs/44011997140). Synthetic browser tests are **not** real-market-browser E2E.

**BLOCKED: VERIFIED_WITH_REAL_LIVE_SOURCE**: Fresh public Coinbase WebSocket network attempt returned network/non-101 WebSocket errors. Real operator completed with exit code 2, **0 messages, 0 snapshots, 0 heartbeats, no admitted market state**. Do not claim actual exchange-to-screen latency or live production readiness.

**BLOCKED: BROWSER_E2E_VERIFIED**: Browser HTTP precheck against actual SHA-pinned archive passed 9 endpoints, but Chromium `Page.goto` was rejected by `ERR_BLOCKED_BY_ADMINISTRATOR`. No direct DOM value or Funnest canvas numeric parity was observed. Execute `scripts/v36_archive_browser_e2e.py` in an authorized local environment.

**NOT IMPLEMENTED / NOT CERTIFIED:** 24-hour uninterrupted fresh live collection, actual exchange gap/reconnect recovery, persistent Linux-host monitoring, independently verified market-day cohorts, PIT features/labels, blind OOS, executable alpha, production operations, order authorization. Trading disabled.

## How existing components are reused

- `backend/v35_archive_ingress.py` continues to verify **archives only**. Archive admission cannot confer LIVE privileges.
- `backend/v36_live_engine.py` shares the unchanged v23 `Book` order-book transitions and v35 feature/depth extraction. It requires a separately attested fresh host observer, dual product snapshots, channel subscription acknowledgements, heartbeat, exact wire sequence, timestamps and freshness to show trusted live prices.
- `scripts/btc_eth_multiday_v20.mjs` is the original v29 underlying Node collector with a strictly optional bounded local observer socket **after** successful raw JSONL storage. Collection remains independent of all UI consumers.
- `scripts/v36_observe_existing_collector.py` supervises an explicit, public-only Linux observer with a private UNIX socket and peer PID checks, no credentials. Failed source admission hides prices.
- `backend/v36_live_gateway.py` inherits the v35.1 historical gateway and adds `/api/v36/{health,snapshot,candles,stream}`. Live SSE source cursor is `v36:session_id:product:version`; session restart, discontinuity, stale state and expired cursor force resync. It is **persistent event-driven SSE**, not repeated finite history. Bounded max 8 clients, 256 stored states/product, 3-second socket I/O timeout.
- `frontend/src/LiveTerminal.tsx` mounts at `/research/v36`; previous `/research`, `/terminal`, `/research/v35.1` routes remain. New bundle is separately SHA-pinned in `frontend/v36_dist_integrity.json`, and the original bundle files are preserved.
- `scripts/v36_play_verified_archive.py` tests the same state/SSE API from a fully admitted immutable archived source in **ARCHIVE_SIMULATION**, rehashing each chunk after archive admission. Never becomes LIVE_SOURCE.
- Historical completed receiver-time trade-derived OHLCV is causal and is not Coinbase official exchange OHLCV; incomplete minute buckets are not published.

## Preservation

The original v35.1r2 ZIP SHA-256 remains `9923d8b7fb2fd70b8fa5feda37b3f62efb7fceb29057e6b470178f519c55e646`. The original v34.1 ancestor remains `8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8`. Compared to the v35.1r2 original 815 files, only the existing `frontend/src/main.tsx`, `frontend/src/style.css`, and `scripts/btc_eth_multiday_v20.mjs` were intentionally modified; all original 210 `backend/data` files remain byte-identical. No raw Coinbase JSONL is uploaded to public GitHub. `main` is unchanged.

## Operational guardrails

Use `scripts/v36_build_frontend.py` to build and SHA-verify the v36 bundle; run `scripts/v36_observe_existing_collector.py --execute-public-capture --workdir /brand/new/secure/location --duration 120 --port 8766` on Linux with public Coinbase WebSocket reachability. Browser is loopback-only at `http://127.0.0.1:8766/research/v36`. Observer output, collector manifest, and evidence are private. The v36 observer itself is bounded to <=1 hour; it is **not** the independent v29 24-hour supervisor. Do not reclassify a reconnecting source as uninterrupted.

## Research verdict
`NO CONFIRMED EXECUTABLE ALPHA`. No real exchange orders, private API credentials, leveraged futures, Paper OMS integration, or executable return claim.
