# v36 BTC/ETH read-only market research terminal: engineering candidate

## Verdict and scope

**ENGINEERING CANDIDATE. NOT PRODUCTION READY. NOT VERIFIED WITH A REAL LIVE COINBASE STREAM. NO CONFIRMED EXECUTABLE ALPHA.** This increment does not request API credentials, send real orders, issue order intents, or connect the isolated paper OMS.

Coinbase venue: public Advanced Trade spot WebSocket, only `BTC-USD`, `ETH-USD`; only `level2`, `market_trades` and `heartbeats`. Original raw JSONL archives and the v35.1r2 package remain immutable. Archive simulation is never a live-source admission. This candidate's fresh network attempt recorded 0 messages, 0 snapshots and 0 heartbeats before a network/non-101 WebSocket error.

## Data path and trust boundaries

```
Existing v29-validated Node collector (scripts/btc_eth_multiday_v20.mjs)
       |                         |
       | atomic raw JSONL chunk  | optional bounded UNIX socket tap of the
       | + hash manifest         | already-successfully-written envelope bytes
       v                         v
Immutable archival evidence     scripts/v36_observe_existing_collector.py
                                 (isolated Linux process, SO_PEERCRED PID)
                                           |
                            v36_live_engine.LiveMarketEngine
                            (fresh connection; ACKs; 2 snapshots;
                             heartbeat; seq; monotonic and receiver clocks;
                             L2 validity; per-product freshness)
                                           |
                  existing v23 Book / v34 Decimal depth / v35 features
                                           |
                            v36_live_gateway.server_for_live
                            (8 max clients, bounded 256 states/product,
                             session-scoped SSE resume, 3s socket timeout)
                                           |
                           /research/v36 React + Funnest Charts
                           (live status, depth, tape, OFI, microprice,
                            only completed receiver-time trade candles)
```

`LIVE_SOURCE` admission requires a fresh explicitly authorized operator-spawned public collector, exact worker PID attestation on a private UNIX socket, receiver-fresh envelopes, exact raw-message SHA-256, connection-local monotonic sequence and wall/monotonic time, Coinbase subscription acknowledgements, both initial snapshots, and a heartbeat. It is host-observed public-feed data, **not exchange-signed attestation**. No archived source may authorize LIVE. The independent v35 `verify_capture` remains unchanged. A separate offline runner uses that gate and copies bounded chunks via O_NOFOLLOW + a second SHA-256 before replaying, strictly as `ARCHIVE_SIMULATION`.

**SSE source cursor:** `v36:<fresh random 32-hex session_id>:<BTC-USD|ETH-USD>:<positive global state_version>`. Every producer WebSocket connection/restart gets a new random session; cross-session, cross-product, unavailable cursor, gap, stale, or ring overflow requires resync. `v35` archive SSE cursor `manifest_sha256:product:state_version` remains unchanged. Slow clients cannot block the collector's source archival, and the optional tap is dropped on backpressure.

**Message provenance:** raw envelope SHA-256, connection ID, wire sequence, raw source receiver UTC and monotonic timestamp, session/run IDs, derived state version, product, venue, mode. Trade-side labels refer to Coinbase maker/resting side, **not aggressor direction**. No incomplete minute is promoted to a finished candle.

## Local historical research

```bash
cd btc_eth_unified_terminal_v36_candidate
python -m unittest discover -s tests -p 'test_v36_*.py' -v
python scripts/v36_archive_browser_e2e.py \
  --browser-executable /usr/bin/chromium \
  --report /tmp/v36_archive_chromium_evidence.json
```

The authentic Coinbase archive is pinned by the original manifest SHA. A browser environment that blocks Chromium localhost navigation returns a *BLOCKED* result. The harness does not synthesize market data. Note that Funnest Charts draws to canvas: the harness validates source candle API and chart presence, but does not falsely claim pixel-level OHLCV numeric equivalence without supported chart introspection.

To serve the local archived source as a clearly labeled v36 simulation, use:

```bash
python scripts/v36_play_verified_archive.py \
  --capture backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy \
  --manifest-sha256 8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5 \
  --speed 1 --hold-seconds 120 --port 8766
```

The observable browser URL is `http://127.0.0.1:8766/research/v36`. `--speed 0` replays without delay. Only a previously externally SHA-pinned source may be admitted; disconnected or invalid captures fail closed before replay. This is still historical simulation, not fresh live.

## Local public live observation on a Linux host

Requires Linux, Node.js 22+, Python 3.12+, available disk with at least 4 GiB reserve, and outbound public Coinbase WSS available. No secrets or brokerage accounts are needed.

```bash
python scripts/v36_build_frontend.py    # npm ci + Vite build + SHA-verification
python scripts/v36_observe_existing_collector.py \
  --execute-public-capture \
  --workdir /absolute/new/private/v36_capture_session_001 \
  --duration 120 --port 8766
```

Do not create the workdir in advance. The workdir is private, new, isolated and holds original JSONL plus evidence. The Node collector independently controls raw/archive budgets and disk reserve. Observe `http://127.0.0.1:8766/api/v36/health` and open `http://127.0.0.1:8766/research/v36`. No trusted quotes appear until all live admission conditions are satisfied; network outage leaves it unadmitted. The operator quits with an explicit incomplete capture verdict if WebSocket cannot connect. A laptop sleep or host shutdown interrupts collection. This operator is bounded to 15..3600 seconds, **not a certified 24-hour durable capture supervisor**. For long monitored capture use the original v29 supervisory chain with its original 40 GB raw / 55 GB archive / 10 GiB reserve budgets and independent replay audits after compatibility is verified.

## Build integrity and historical UI preservation

The `frontend/v36_dist_integrity.json` file pins v36 HTML, JS, CSS; `backend/v36_frontend_integrity.py` pins the manifest itself. Run `python scripts/v36_build_frontend.py --verify-existing --from-v36-dist` to verify already-packaged `dist_v36` without recompiling, or `python scripts/v36_build_frontend.py --offline-build` after pinned node_modules is installed. The v34 `frontend/dist` and v35.1 `frontend/dist_v35_1` build copies are preserved unchanged, along with `/research`, `/terminal`, `/research/v35.1`. New `/research/v36` is strictly read-only.

## Monitoring and controlled failure

Health states: CONNECTING, WAITING_FOR_SNAPSHOT, HEALTHY, STALE, QUARANTINED, RECONNECTING, STOPPED. Missing source, inconsistent sequence, crossed book, invalid UTC/monotonic clocks, missing dual snapshots, subscription mismatches or stale heartbeats suppress trusted price publication. `/api/v36/health`, `/api/v36/snapshot?product=BTC-USD`, `/api/v36/candles?product=ETH-USD`, `/api/v36/stream?product=BTC-USD` are the read-only contracts. Fast finite archival source EOF is not the same as real live source silence. Restart never stitches sessions; a new session invalidates previous SSE cursor.

Existing v35.1 `/api/v35/*` continues to work when its immutable archive dataset is loaded; the standalone v36 observer does not create that dataset automatically. Static historical pages remain available. Read-only v36 Gateway uses loopback only and cannot accept external network binds.

## Measured bounded historical work, not live operational certification

Real frozen one-hour source contained 152,069 messages, 41 chunks, 258,396,723 raw source bytes and 325,724,466 archived JSONL bytes. In this one environment, v36 historical-simulation engine processed the messages in 20.9–21.1 seconds (~7,222 messages/s) and peaked near 148 MiB Python RSS; original v35 Replay Service independently processed in 24.1 seconds. The 16 final BTC/ETH book and feature comparisons matched exactly, with 59 completed trade-derived minute candles per instrument. **These are not exchange-to-browser measurements.** At that particular observed one-hour rate, the simple 24-hour linear extrapolation would be about 7.82 GB of JSONL, but volatile market regimes may require far more and the collector budgets are hard limits, not an expected data size.

Known blocked gates: public WebSocket connection/network (0 fresh messages), local Chromium navigating localhost (`ERR_BLOCKED_BY_ADMINISTRATOR`), live source-to-screen DOM parity, persistent 24h live operation, independent live-exchange reconnection, signed exchange attestation, PIT/Blind OOS and executable alpha. Existing v35.1 synthetic Chromium regression passing does not prove real live rendering. **No real trading orders.**