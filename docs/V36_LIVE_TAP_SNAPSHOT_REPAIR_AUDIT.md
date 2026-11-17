# v36 actual Coinbase live tap repair and verification

**Engineering status:** REAL PUBLIC LIVE SOURCE SMOKE VERIFIED, not a production or 24-hour operational certification. Trading remains disabled and there is NO CONFIRMED EXECUTABLE ALPHA.

## Failure root cause, independently reproduced

- Earlier [run 44015527497](https://github.com/minseogiiiii/quant-research-system/actions/runs/44015527497) completed a **single real Coinbase public WebSocket** capture with 1,329 archived messages and 0 collector faults, but the v36 observer received 0 messages and never admitted either book. Public WSS DNS resolved and HTTP Upgrade returned 101. The issue was between successful archival and the observer.
- The first message in the immutable v34.1 clean Coinbase archive is a **4,696,722-byte JSONL L2 snapshot envelope**. Node's original tap treated `socket.write(bytes) === false` as a rejected write and immediately destroyed the socket. In Node, false indicates **backpressure**, not necessarily rejection. The default writable high-water mark is much smaller than a 4.7MB real L2 snapshot.
- Reproduced offline using a synthetic 4,701,715-byte first message with the *old* collector: **0 delivered to the observer**. The same test against the modified collector received **8/8 records** and matched the original archived JSONL bytes exactly. No private raw Coinbase data is committed.

## Minimal repair

1. In the existing `scripts/btc_eth_multiday_v20.mjs`, use a bounded 16 MiB writable high-water mark on the optional Unix tap. Preflight the pending-byte budget before attempting an observation write.
2. Do not call `destroy()` simply because `write()` returns false. The accepted record is already buffered. If pending observation exceeds the hard resource budget, **invalidate the tap**, never interrupt raw source archival.
3. Flush accepted observer records with a bounded socket shutdown rather than calling `end()` and immediate `destroy()`.
4. Preserve the original WebSocket collector, raw chunk store, timestamps, hashing and manifest schema.
5. Add `test_v36_large_frame_tap.py` and opt-in `V36_TAP_LARGE_SNAPSHOT_TEST=1` to the **synthetic-only** existing WebSocket fixture, validating true 4.7MiB initial-message parity without storing source records in GitHub.
6. The observer's `capture_ready_for_research` gate now rejects zero-message, incomplete, faulty and nonzero-exit captures. The public WSS diagnostic reports DNS and handshake outcomes without subscribing or granting LIVE admission.

## Real public source CI evidence

[Success: run 44016494315](https://github.com/minseogiiiii/quant-research-system/actions/runs/44016494315), tested code commit `f832beb24e148710b5a63d6e66e583841524d33a`:

- DNS: RESOLVED; public WebSocket HTTP Upgrade **101**, validated `Sec-WebSocket-Accept`; no credentials required.
- Health transition observed: CONNECTING, WAITING_FOR_SNAPSHOT, **HEALTHY**.
- At admission: **37 actual public Coinbase messages**, last sequence 36, and an admitted read-only BTC/ETH snapshot with receiver-time, raw-source digest and state version:
  - BTC-USD bid **64928.7**, ask **64928.71**, state version 1, receiver timestamp **1794889234995 ms**.
  - ETH-USD bid **1827.27**, ask **1827.28**, state version 2, receiver timestamp **1794889234995 ms**.
- Full collection: **1,389** original public WebSocket messages, **36** heartbeats, one connection, **zero** manifest faults, finish reason DURATION_REACHED, completed capture status `CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH`.
- Collector and admitted observation both exited 0; `PASS HOST_OBSERVED_READ_ONLY_LIVE_SOURCE`.
- `trading_enabled=false`, exchange-signed attestation absent, and no exchange order-writing endpoints.

[Success: code-only CI 44016469877](https://github.com/minseogiiiii/quant-research-system/actions/runs/44016469877) included Node collector syntax, original small synthetic tap parity, new **4.7MB synthetic tap parity**, synthetic-only live admission contracts, no-data fail-closed observer tests, bounded HTTP and SSE source-bound cursor tests.

## Strict boundaries

A single successful short live-source smoke demonstrates the existing Collector → host-verified Unix tap → admission state machine → read-only BTC/ETH HTTP snapshot pathway. It does **not** independently demonstrate all source/archive bytes, deep-book replay parity of this newest host-run (raw records intentionally not uploaded), real live Funnest/Chromium source-to-DOM parity, sustained persistent live SSE, production service restarts, 24-hour uninterrupted capture, point-in-time model eligibility or trading profitability.

Original frozen source archives were not changed; default `main` was not modified. No raw Coinbase market JSONL was committed or uploaded as CI evidence.

## Next gates

1. Independently verify actual new live archive against `v35_archive_ingress.verify_capture` and the existing independent L2 replay in a controlled private-source environment.
2. Run authorized Chromium live market-state DOM parity for BTC/ETH, preserving source lineage.
3. Prolonged live SSE and archival fault injection; monitor queue occupancy, disconnects, precise event latency, CPU/RSS and disk budget.
4. Separately attempt uninterrupted 24-hour collection and real multi-session recovery trials.
