# v36.1 Actual LIVE Chromium and Funnest OHLCV Integrity Audit

**Current release classification: Read-Only Engineering Candidate, NOT production certified.** NO CONFIRMED EXECUTABLE ALPHA. No real or paper order execution is authorized.

## Frozen source and preservation

- Previous v36 baseline GitHub HEAD: `5be727eca06dc3c75e1deda7aab2127f94664df6`.
- Code candidate frozen after fix: `03f7341793c3ad60c2efb6a6bc8db1afbc640c02`; follow-up workflow-only commit: `0e191fba160dcb48f28a1002607d991590a1d301`.
- Frozen v36 ancestor ZIP SHA256: `d8d7100926ff71cc439a0df09c593b353bed83c428c439b8c7f8bc7790f19f1b`.
- SHA256 checked code-only patch ZIP: `e2148b7064d7133745b30259943708c52aa7cbb672e4e0600f593c8cbe42c1b2`.
- Independently repackaged full candidate SHA256: `4767e93f51360de20ac588d0821dc1e0ad87f89085277b8185455ef331a031c5`. 873 ZIP entries, 872 manifest-listed bytes/hashes verified after independent extraction. Original **210 raw market-data files byte-identical**. GitHub main unchanged.
- Code-only patch excludes new private live-source captures. GitHub Actions real-source recordings were isolated in temporary runner storage and were not uploaded.

## Work implemented, preserving collector and historical engine

- Funnest 0.6.24 native `getBars` and `subscribeBars(onBar)` Datafeed, UTC millisecond timestamps, completed-only receiver-time 1-minute candles, exact-source history invariance; React rendering no longer remounts the Funnest chart on every minute.
- Serialized 3.5s health polling and stricter source/product/session/version, duplicate SSE and source sequence handling in existing `LiveTerminal.tsx`. Untrusted or disconnected quotes are withheld.
- Read-only `/api/v36/state?product=...&session=...&version=...` retains precise versioned reference states or returns resync-required; exact-version browser comparison no longer compares a moving display to a later `latest` quote.
- Browser Playwright receives an atomic same-tick DOM snapshot of 6 market metrics, full 10-level book and actual Funnest `getBars` trace. Repeated product switching deliberately observes 10s per product and records multiple exact states.
- Independent raw-trade Decimal OHLCV calculation not using the existing engine's aggregator; verifies all completed time/OHLCV/volume/count/availability fields vs existing engine and read-only HTTP.
- Historical `/research` asset bug fixed by restoring the original SHA-verified v33 `frontend/dist_v33` locator.

## Real public LIVE evidence, separately verifiable workflows

**[2026-11-17 real LIVE run A, Actions 44023798056: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44023798056)**

- Single real Coinbase Advanced public spot WebSocket connection, approximately **240s**; 10,349 original messages fully matched between collector and observer.
- LIVE Chromium observed **171.922 s**, with **74 BTC + 75 ETH different exact source-session state versions** matching all 6 DOM quote/feature fields and 10 levels per product.
- Real Funnest `getBars` numeric boundary matched **1 BTC + 1 ETH completed bar**; source-backed `canvas` pixel number equality NOT VERIFIED.
- Raw archived-source independent replay and book final state PASS. Independently recomputed **3 BTC + 3 ETH completed OHLCV bars** match engine and HTTP.

**[2026-11-17 real LIVE run B, Actions 44023828825: PASS](https://github.com/minseogiiiii/quant-research-system/actions/runs/44023828825)**

- Single public Coinbase source connection approximately 240s; **10,304 collector and observer messages matched**.
- LIVE Chromium **172.138s**, **77 BTC + 71 ETH exact versioned states** and 10 L2 levels each matched the same-version reference.
- **2 BTC + 2 ETH Funnest `getBars` numeric bars matched**. Independent raw-source Decimal OHLCV matched **3 BTC + 3 ETH** completed bars.

Preserved independent prior failures: [44022408488](https://github.com/minseogiiiii/quant-research-system/actions/runs/44022408488) (moving DOM read produced apparent depth mismatch) and [44023081887](https://github.com/minseogiiiii/quant-research-system/actions/runs/44023081887) (rapid product switching yielded insufficient version coverage). These runs never passed; the new synchronized DOM read and 10s instrument dwell subsequently met the original eligibility thresholds twice without lowering them.

### Additional genuine archive evidence

A frozen real Coinbase one-hour cohort, **152,069 source messages**, independently generated **59 BTC + 59 ETH** finalized receiver-time candles, each exactly agreeing with the v36 engine's Decimal values, timestamp, observed trade count and availability. Pinned Funnest Datafeed outputs `getBars` and `onBar` each matched all 59 per product using the same genuine source-derived candle inputs. This is **archived-source** verification, not 60 minutes of fresh continuous live browser observation.

## Regression and delivery

- Python v36 targeted tests from independently extracted candidate: **65/65 passed** after v33 historical asset locator repair.
- Funnest Node exact Datafeed tests from independently extracted candidate: **6/6 passed**.
- GitHub frontend build, v35 synthetic Chromium and v36 backend contract CIs passed on the repaired code.
- Full Python regression against frozen candidate: reported separately after exact process termination; do not infer success from targeted tests.
- Original markets and frozen historical research code were not rewritten.

## Remaining operational gates

1. **NOT VERIFIED:** Funnest canvas pixel-by-pixel numeric OHLCV.
2. **NOT VERIFIED:** 24-hour uninterrupted real public market-data capture, sustained live multiclient backpressure, disk exhaustion/crash recovery, live WAN gap, source silence and resync injection.
3. **NOT VERIFIED:** Actual persistent Linux host monitoring and off-host encrypted source retention.
4. **NOT VERIFIED:** Official signed Coinbase market truth or independently pre-capture pinned live original.
5. No point-in-time blind OOS alpha or conservative execution study; **NO CONFIRMED EXECUTABLE ALPHA**.
6. LIVE trading/order-writing interfaces remain disabled.

The proper next engineering work is v36.2 operational fault recovery and long-running streaming, not new predictive strategy tuning. Actual release promotion remains blocked on corresponding operational evidence.
