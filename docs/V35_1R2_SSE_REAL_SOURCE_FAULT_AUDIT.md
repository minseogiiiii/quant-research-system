# v35.1r2 Coinbase SSE provenance and fault-validation audit

**Status: ENGINEERING CANDIDATE, not production deployment.** Trading remains disabled; research outcome `NO CONFIRMED EXECUTABLE ALPHA`.

## Baseline, preservation and current code
- Frozen full v35.1r1 parent ZIP SHA-256: `283b3e93e7a797ece280bfb1ed1e728cefc21693805e5cf561085dcac2b7d0ff`.
- Integration code and CI-tested commit: `1abd99afeb71447fb7aade4b8d77b380dcdcb422`. `main` remains `a9edeede7a3c5a57ea3c91bcd5e32a7470fe67bd`.
- Compared against the parent ZIP, **793 files unchanged**, only 3 original files intentionally modified, and all **210 frozen original backend/data files byte-identical**.
- All source admissions remain `ARCHIVE_REPLAY`, never `LIVE` or execution authorized. No raw market-data chunks were committed.

## Discovered and fixed
A numeric SSE `Last-Event-ID: 5` was incorrectly accepted with HTTP 200 after restarting on a different, genuine Coinbase archived-source capture. New `id` values bind **`manifest_sha256:product:state_version`**. Cross-source resume, cross-product reuse, old numeric header IDs and unscoped numeric `/api/v35/stream?after=N` now fail closed with HTTP 409. Malformed IDs return 400. Historical `/api/v35/history` pagination is unchanged and carries source SHA in responses.

## Measured authentic archived-source evidence
- 120-second source: 5,177 admitted original messages. HTTP history **114 BTC/117 ETH rows**, corresponding SSE pages **114/117 rows**, exact payload equality. Six version-pinned snapshots and both finished source-backed receiver-time candles agreed with the backend reference.
- 120-second SSE soak: 6 parallel client workers; 10,266 responses, 123,192 verified state frames, zero mismatch/errors. Request p50 1.90 ms, p95 5.10 ms, p99 7.61 ms. Gateway peak sampled RSS 125,317,120 bytes. Forced SIGKILL and source switching recovered with unchanged state digest; different-source cursors were rejected 409.
- 180-second repeat on final cursor code: **15,896 verified SSE responses and 190,752 full state payloads**, no mismatches/errors. Latency p50/p95/p99 **1.84/5.15/9.82 ms**; peak sampled gateway RSS **125,440,000 bytes** (~119.6 MiB); per-process RSS delta **0.9–1.5 MiB**. Same-source SIGKILL recovery **1.753 s**, alternate archived-source switch-and-restore **4.108 s** with old cursor rejected.
- Five unfinished HTTP header sockets experienced EOF when the process was SIGKILLed; same archive was reloaded in 1.629 s, digest unchanged. A separate 200-request early-disconnect burst produced **200 SSE aborted sends** with no state corruption or permanent slot occupation.
- Finite replay EOF and four seconds of post-EOF silence did not fabricate new market states. This **does not** validate live-exchange stall recovery.
- These are repeated finite **historical** SSE responses, not durable subscriptions to a continuous live market feed. Samples do **not** establish 24-hour memory stability or production capacity.

## Test and release evidence
- Fresh final source-tree full Python regression: **841 tests, 401.112 s, OK**.
- New actual-Coinbase cross-source cursor regression: **7/7 PASS**.
- Exact full package and patched-parent extraction: **815 package files, 814 manifest-verified hashes, exact patch-reproduction**; fresh extraction regression **22 + 11 + 7 PASS**.
- [GitHub bounded HTTP, import, synthetic-only SSE cursor and static no-order CI PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44003848218).
- Full packaged candidate SHA-256: `9923d8b7fb2fd70b8fa5feda37b3f62efb7fceb29057e6b470178f519c55e646`.
- Code-only patch SHA-256: `8980ed815adf4bea0f26ce2dc504e1d6945371964a280017afa754edf9aa8d55`.
- Privately delivered package includes source, reproduction harness, original raw source, and detailed local result JSONs. Public GitHub does not host the raw archives.

## Browser and remaining gates
A real archived Coinbase capture passed eight HTTP prescreen endpoints including `/research/v35.1`, original `/research` and `/terminal`, but local Chromium `Page.goto` was blocked by **`ERR_BLOCKED_BY_ADMINISTRATOR`**. **Real Coinbase source → Chromium Funnest DOM exact OHLCV/Bid/Ask/replay matching remains BLOCKED, NOT PASSED.** The existing synthetic browser CI cannot replace this requirement. Reproduce using `scripts/v35_2_real_archive_browser_e2e.py` in a legitimately authorized local browser environment with local private source access. No security restrictions were bypassed.

Unverified: 24-hour live collection, continuous live SSE/stale alarms, full operational deployment and independent blind OOS. No exchange order-writing APIs enabled.
