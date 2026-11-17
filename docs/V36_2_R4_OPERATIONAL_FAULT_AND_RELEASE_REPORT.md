# v36.2 R4 Operational Failure and Release Validation

**Verdict:** Read-only engineering candidate. No production/24-hour certification. No exchange orders or confirmed executable alpha.

## Exact code and release provenance
- Code-only integration HEAD: `584c85d3b3e5ddfaf9e1adc3ed38c2f486d1f063`, latest [backend CI PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44174606808).
- `main` was unchanged, R3 original bytes were frozen.
- R3 parent ZIP SHA-256 `a4b5d3eb4403ecae18584252a37bb568ca03954c99c8d0957dcb38ee155b058d`.
- Full R4 SHA-256 `59c2d987a238028adca1970ca1adc529003e02300f6378dac3a74402d5f0ae2a` (60,676,620 bytes; 886 entries).
- R4 code-only patch SHA-256 `41fb2e9abbbd6f7edec41d486a39217c78d50506811112d57cd4bfc54d01a0d3` (179,616 bytes; 17 changed/new entries).
- **885/885** file manifest checksums verified in an independent clean extraction; R3 + patch reproduced entire R4 byte-identically. 210/210 frozen original market data files identical. Four existing modified engine/collector files matched their GitHub blob IDs.

## Real Coinbase forced reconnect
[Actual Coinbase public-source fault CI PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44172733104). Controlled disconnect after 150 messages: 2,216 original received, 2 source connections, 2 distinct Observer session IDs, one gap, two fresh BTC/ETH snapshots plus heartbeat, full source archive **quarantined** from research admission. Old-session SSE cursor fail-closed. This is a short injected **real public market-data** test, not a real exchange outage or uninterrupted capture.

## Bounded multi-SSE archival stress
Actual immutable one-hour Coinbase archive **152,069 records**, paced across 179.955 seconds, 6 concurrent SSE receivers, 1 slow and 1 deliberate resume. **20,844 complete state frames verified**, zero mismatches, pre-gap resync zero, post-gap six resyncs; p95/p99 normal local publication-to-client **3.589ms / 6.421ms**, peak 7/8 permitted connections, 17 sampled file descriptors, Python process peak RSS **202,489,856 bytes**, growth **34,160,640 bytes**. This is `ARCHIVE_SIMULATION_NOT_LIVE`; no 24-hour memory leak or exchange-to-screen claim.

## Disk failures, interruption, durable operator
Three actual Node processes with synthetic WebSocket frames verified archive budget, unavailable reserve and SIGKILL leaves unadmitted `.partial` intact. Restart uses a fresh workdir. Added strict opt-in Linux observation mode with 40GB raw, 55GB archive, 10GiB reserve, max 86400s, never combined with source fault injection. Eight policy tests passed. systemd unit and launch script passed static validation; **no actual persistent Linux host was started**.

## Fresh independent R4 regression
R4 frozen-source Python discovery: **953** tests. Exactly 5 obsolete v31/v32/v33 single-`frontend/dist` tests were excluded by ID; **948 tests actually executed, 948 PASS, 0 fails, 0 errors, 0 skips in 405.930 seconds**. Required frozen v34.1 ZIP and clean/disconnected original archives were explicitly provided to avoid skipped fixture classes. This is **version-qualified**, not 953/953. Independently reextracted R4 new v36.2 tests **18/18 PASS**; native Funnest Datafeed Node tests **6/6 PASS**.

## Outstanding gates
- Actual uninterrupted 24h capture on a durable Linux host, real machine disk-full/SIGKILL recovery, archive replay/reconciliation across controlled supervisor restarts.
- Several hours of real newly arriving public-source SSE under sustained client slowdown and packet gaps; 180s archived paced replay does not suffice.
- Modernize five obsolete single-dist front-end tests for unqualified full regression.
- Pixel-level Funnest canvas numeric parity remains unverified.
- No blind OOS PIT/cost/execution alpha, **NO CONFIRMED EXECUTABLE ALPHA**.

No credentials, user account connectivity or real trading were enabled. No private live raw JSONL is committed to GitHub.
