# Q25 P0-B: Fail-closed source epoch isolation, never gap stitching

## Scope

Q23's real five-hour Coinbase public-feed attempt failed after roughly 3h50m
when the collector's WebSocket connection ID changed from 0 to 1. The
previous session, source sequence and heartbeat counters could no longer be
treated as one continuous sample. Q24 P0-A added reliable private source
close forensics, separately verified by genuine public Coinbase 180s smoke.

This phase **does not attempt another five-hour run**. It enforces
source-session segregation during a disconnect/reconnect and proves the
observable API and SSE boundary.

## Existing preserved contracts

The frozen Q14 `v36_live_engine.py`, `v36_live_gateway.py`, v20 Node
collector, archive and order code are unchanged. Their original rules require:

- New connection ID and sequence zero after a gap
- Fresh independently constructed BTC-USD and ETH-USD L2 snapshots
- `level2`, `market_trades`, `heartbeats` subscribe acknowledgement
- Heartbeat before transitioning to `HEALTHY`
- Uniquely randomized new session ID, no old version pin or SSE cursor reuse
- STOPPED, STALE or QUARANTINED must never emit valid market quotes

## Added Q25 defense-in-depth

`q25_session_isolation_guard.py` is an opt-in subclass activated by
`Q25_SESSION_GUARD_ENABLED=1` exclusively in the disposable CI observer.

Whenever any source invalidation occurs, under the engine's original RLock,
it destroys the retained in-memory books, per-product OFI, L2 buffers,
trade history, candle history, pending candles, and 256-row SSE version
buffers. Forensic session IDs, counters, and gap counts remain available.
Readers cannot observe market state until the engine lock releases.

Private health adds a strictly numeric/bool witness:
`q25_source_epoch_isolation_v1`, `q25_revocations_total`,
`q25_retained_rows_per_product`, `q25_open_candles_per_product`,
`q25_quote_buffers_scrubbed`, `q25_orders_enabled=false`.
No prices, trades, source payloads or tokens are added to diagnostics.
The CI supervisor verifies this guard is active, and rejects revocations
during a required zero-gap real-source 180-second smoke.

## Adversarial test design

The new deterministic mock source sends:

1. Valid BTC and ETH snapshots, subscribe acks and heartbeat in conn 0.
2. Synthetic forced disconnect after a known committed envelope.
3. A clean session restart at conn 1, with **BTC snapshot, subscribe acks,
   heartbeat, but deliberately NO ETH snapshot for 1.5 seconds**.
4. New ETH snapshot and a new isolated pair of market quotes.

During phase 3, both product snapshots must be unavailable (HTTP 503), even
though a valid BTC snapshot has arrived. Existing `/state`, `/stream`,
`/candles` and source cursor requests must not expose the old session.
The former SSE subscriber must receive `resync_required`; after new dual
snapshot and acks, old session/cursor must continue returning HTTP 409.
New BTC/ETH quotes must identify only connection 1 and the new session.
Archived chunks must remain connection-tagged, and the synthetic manifest
must be `ready_for_research=false` with one or more source faults.

Test also forces STALE, QUARANTINED and STOPPED paths, tests that all internal
price/trade/OFI/candle structures are erased, and validates no real-order
path was activated.

## Deployment and source integrity

CI and real-source smoke run only on a separate
`p0/q25-session-gap-isolation-20261123` branch. Source blob hashes for
all 278 frozen Q14 artifacts are verified against immutable release
evidence. The smoke uses an actual 180-second public Coinbase source,
8-client SSE, read-only Q24 WS-close witness, and dual private, memory
bounded archive replay. Tests with synthetic feed DO NOT attest Coinbase.

The existing 18,000-second Q23 arming file is deliberately not updated.
No new long run is triggered by this change, and `main`, real-order
authorization, persistent 24-hour deployment, and data/alpha certification
are unchanged.

P0-B is accepted only if ALL synthetic contracts and genuine 180-second
smoke pass at the exact same source commit, with no skipped critical tests.
Five-hour zero-gap certification remains FAIL until a new independently
reviewed five-hour observation and two full replays succeed.
