# v36 Real Public Coinbase Live SSE and Chromium Source-to-DOM Audit

**Status:** Verified short REAL LIVE source + persistent SSE, plus REAL ARCHIVED-SOURCE Chromium DOM quote parity. **NOT PRODUCTION READY.** No real or paper order execution is authorized; research verdict `NO CONFIRMED EXECUTABLE ALPHA`.

## Baseline and source lineage

- Development branch: `integration/btc-eth-unified-terminal`. Existing `main` remains `a9edeede7a3c5a57ea3c91bcd5e32a7470fe67bd`.
- Frozen original v35.1r2 SHA-256: `9923d8b7fb2fd70b8fa5feda37b3f62efb7fceb29057e6b470178f519c55e646`.
- Original Coinbase raw JSONL was not committed or uploaded. New live-source examples remained in private, ephemeral GitHub runner workdirs.
- New captured manifest pins below are **collector-generated output hashes**, NOT independently known pre-capture Coinbase signatures and NOT attestation from the exchange.

## New strict final-message reconciliation

Previously, initial LIVE source admission could pass after ~40 initial messages without checking whether all subsequently captured messages made it into the optional observer tap. `scripts/v36_observe_existing_collector.py` now joins the observer receive worker after collector shutdown and requires final raw capture messages, last sequence and heartbeats to exactly match its retained engine state. New `test_v36_observer_full_capture.py` injects 12 failures, including dropped last frame, missing snapshots, gaps and incomplete reader. [Code-only CI 44017184609: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44017184609).

New `scripts/v36_verify_live_capture.py` checks the finalized private source with existing `v35_archive_ingress.verify_capture`, primary `v35_replay_service.replay_capture` and independent `independent_soak_replay.replay`, comparing BTC/ETH final bid/ask and bid/ask price-level counts.

- [Real public capture CI 44017200547: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44017200547): 1,457 actual Coinbase messages, 1,457 observer frames, one source connection, 36 heartbeats, zero collector faults, independent final book agreement. The independent primary state SHA-256 was `a2e15560dc769c6084ab81bb1652a1e9bfaba98f83b4af8edd1cea144a16462e`.

## Persistent LIVE SSE of new messages

`scripts/v36_verify_live_sse.py` opens simultaneous BTC-USD and ETH-USD **real live** SSE subscriptions. It requires an initial snapshot, subsequent new increasing states, source-qualified cursors, product/session continuity, receiver-time and source-sequence monotonicity and uncrossed bids/asks.

- [Real LIVE SSE CI 44017443281: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44017443281). Both products received two new `state` events after their original `snapshot`, sharing the same source session. BTC states advanced 1 → 6; ETH 2 → 5. Total observation duration was ~1.9 seconds for **two** new states, not measured exchange-to-browser latency. The same runner admitted and independently replayed 1,309 completed original messages, with all Observer counts equal.

This **does not** prove 24-hour persistent streaming, intermittent WAN reconnection, live Chromium visual correctness or sustained memory growth.

## Real archived-source Chromium discovery and repair

First [browser integration CI 44017768025](https://github.com/minseogiiiii/quant-research-system/actions/runs/44017768025) returned 503 on legacy `/research`: the public code checkout excludes frozen older frontend `dist` assets. The harness now explicitly labels legacy routes `UNVERIFIED_MISSING_FROZEN_ASSETS_IN_CODE_ONLY_CI` rather than conflating them with v36 source validation or falsely declaring compatibility.

Next [browser CI 44017985394](https://github.com/minseogiiiii/quant-research-system/actions/runs/44017985394) reached Chromium and found **a genuine frontend bug**: immediately after selecting ETH, the BTC bid still appeared under the ETH selection, causing an expected vs observed quote mismatch. Fixed `frontend/src/LiveTerminal.tsx` by clearing the prior state on product change and never rendering a quote/chart when `state.product !== selected_product` or the source session does not match. The existing browser harness now waits for the exact target product state version.

The edited React bundle was built in CI and SHA-256-pinned:
- JS asset `assets/index-Dqfy5zqK.js`: `8a69561694d7184a5831cbe0722efe8dfd2d3942cf452ef0b252f7ef96c3fe14`.
- CSS asset `assets/index-Ckn1nBC7.css`: `e46be294943262601df34f27c14f090e0bf8ca4acd183940c2f21c2e81a6b8b6`.
- HTML `index.html`: `2e3acc9ac646f40f8ea08c8e6c023ef0975a6fe8f53a7817caf150fd86e1a52b`.
- `v36_dist_integrity.json` pinned SHA-256: `f845bf42b9b3c6eee7b154cc095168ab83c3b6766ad2805fbb83efd60700e238`.

[Frontend build CI 44018272186: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44018272186); [synthetic browser CI 44018272192: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44018272192).

## Verified real private Coinbase archival input → browser DOM

**[Full real Coinbase pipeline CI 44018298924: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44018298924).**

- Actual Coinbase Advanced public WSS collector → validated host-owned Unix tap → LIVE Source Admission → read-only dual product LIVE SSE with **two advancing states each**.
- Capture finished with **1,361 messages in one source connection**, 36 heartbeats, 0 recorded collector faults, matching final Observer messages. Private raw archive source hash `9ff88aad521e39fbe043be9d3a179681b3cb30b9700e1e58a4ac41da6c89a777`.
- Exact archived source passed existing Archive Ingress, original primary replay and separate independent L2 replay. Primary replay digest `968d3bb2df73f94963b3f9cb3ba9254f59738befba546405f35e26085149328e`.
- A new, SHA-pinned v36 frontend was served by the genuine read-only gateway. **Real Google Chrome** (Playwright) switched products and compared DOM Best Bid, Best Ask, Spread, Microprice, cumulative OFI and top-level book prices/sizes with the admitted archived-source engine state. BTC state version `69`; ETH state version `70`. JS page errors were checked; no mocked market payload was injected.
- The browser verification uses **finite, archived replay** of that genuine live capture, explicitly labeled `ARCHIVE_SIMULATION`. The simultaneously tested actual LIVE SSE used the separate LIVE_SOURCE mode.

## Strict remaining gates

1. **NOT VERIFIED:** Actual live (not archived-replay) source displayed in a continuously running real Chromium browser, with source-state event equality.
2. **NOT VERIFIED:** Funnest canvas numeric OHLCV parity. The 30-second capture generally lacks a completed minute candle, and a canvas presence check is not a numeric candle proof. A longer 120+ second authentic capture plus chart datafeed/canvas inspection is required.
3. **UNVERIFIED IN PUBLIC CODE-ONLY CI:** Frozen historical `/research`, `/terminal` and `/research/v35.1` static assets. Existing release package has separate historical regression evidence; browser code-only job must not claim this coverage.
4. **NOT VERIFIED:** Full regression rerun against the exact final GitHub HEAD including the latest browser/manifest edits. Only the cited targeted workflow passes have been newly observed.
5. **NOT VERIFIED:** 24-hour uninterrupted live capture, advanced reconnect and SOURCE_GAP/RESYNC under real network interruptions, long-running multi-client memory, and release package parity for these newest GitHub commits.
6. No signed Coinbase exchange attestation, no independently approved original Feature/Label PIT, no blind OOS research, no confirmed executable alpha, and no authorized real-money trading.

The original collector/Book/replay engines were reused rather than replaced; source is read-only with all order-write surfaces disabled.
