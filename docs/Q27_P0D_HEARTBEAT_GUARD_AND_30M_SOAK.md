# Q27 P0-D: opt-in live heartbeat watchdog and separate 30-minute operational soak

## Motivation

Q26 proved the original frozen Coinbase public source collector can stay on
one socket while heartbeat-channel messages stop, if L2 messages continue.
The original `STALE_FEED` checks all-frame idle time, not heartbeat-channel
health. The engine already fails closed in LIVE_SOURCE after ~5s without a
heartbeat, but the original collector does not autonomously rotate solely
because this channel is silent. Q26's 4.5s watchdog was SYNTHETIC-ONLY.

## Q27 optional real-public-source implementation

`btc_eth_terminal/scripts/q27_heartbeat_guard.mjs` is a Node 22
`--import` preload. It wraps the global native WebSocket without modifying the
immutable 278-blob Q14 collector. It is active only under explicit
`Q27_HEARTBEAT_GUARD_ENABLED=1` with a known `SOAK_TEST_FIXTURE=0|1`.
The observer additionally requires both Q24 redacted close forensics and Q25
memory erasure before enabling the guard. There are NO orders.

The guard uses Node monotonic time. Once the socket opens, it allows up to
9 seconds for a first valid Coinbase `heartbeats` channel message. After
the first heartbeat, every gap over 4.5 seconds causes one local `close()`
request per socket. The unmodified collector's original close handler owns
`SOURCE_GAP` IPC control, per-connection archive quarantine, backoff and
fresh connection/snapshot. Q25 erases prior BTC/ETH quotes, trades, OFI,
candles and SSE versions. New source states remain invalid until dual fresh
L2 snapshots, three subscription acks and a heartbeat are present.

Heartbeat detection parses only small messages (<=8192 chars); it accepts
only `channel=heartbeats`, a valid numeric sequence and nonempty events. L2,
trades and other messages never renew the heartbeat deadline. Native
WebSocket ping/pong control frames remain UNOBSERVED under WHATWG WebSocket.

The private trace is created once, mode 0600 inside a 0700 source workdir,
bounded to 55,000 bytes and 400 events. Fixed enums and numeric counters
only. No exchange endpoint, price, payload, secret, untrusted error.message
or close reason is ever persisted. The Python receipt validator rejects
unsafe links, permissions, unexpected fields, forged counters, nonmonotone
clocks, truncated files and conflicting intervention counts. Only bounded
redacted JSON is uploaded; failure to read trace is NOT a PASS.

## Required acceptance

1. All immutable 278 Q14 blobs match pinned Git blob hashes, no changed
   original collector or production engine, `main` untouched.
2. All Q22/Q23/Q24/Q25/Q26 existing contracts, including Q26's negative test
   demonstrating the unmodified native heartbeat-only deficiency, pass.
3. Q27 synthetic integration on the exact ORIGINAL collector with a
   *real-compatible* Q27 opt-in preload, no Q26 test-only watcher:
   heartbeat-only silence while L2 continues -> Q27 close after 4.5s ->
   collector gap -> Q25 in-memory erasure -> partial BTC-only new connection
   returns 503 on BOTH products -> old SSE resync and cursor 409 ->
   new dual-snapshot+ack+heartbeat session. Separately no INITIAL heartbeat
   -> guard closes after 9s, no invalid quote is admitted, clean fresh
   subscription. All synthetic archives remain NOT research ready.
4. No source data, URL, error reasons or credentials in public synthetic
   diagnostic receipts. No missing, corrupt or forged Q27 trace accepted.
5. Genuine Coinbase READ-ONLY 180s smoke at the SAME SHA:
   Q24 forensic, Q25 source isolation, Q27 guard active, 8 concurrent SSE
   clients, 0 heartbeat watchdog interventions and 0 source reconnects,
   manifest/hash audit and TWO independent final BTC/ETH orderbook replays.
6. New independent **30-minute resilience smoke** only after a named, exact
   preceding 180s preflight SHA and Q14 blob manifest are verified, with
   three separate 120-second 8-client SSE windows, 15s source/health/resource
   monitoring, 0 gaps, 0 interventions, source/collector PID stable, end-to-end
   archive audit and two independent full final-book replays. On any gap
   the old zero-gap acceptance must FAIL; guard events or backend invalidity
   are not silently tolerated. The 30-minute result is NOT a five-hour,
   24-hour persistent host or live-trading authorization.

## Risk interpretation

- 4.5s is a conservative initial gate, NOT a guarantee against bursty
  exchange heartbeat jitter. Any premature guard close fails continuous-source
  acceptance and is examined separately in redacted forensics.
- The guard watches application heartbeats, NOT native ping/pong frames.
- A local close request alone never proves that Coinbase or the network
  was at fault. P0-A's independent close forensic records the local request.
- An archive containing a source gap is never silently stitched together.
  For quantitative research, use only independently verified continuous
  segments and never treat a synthetic fixture as exchange truth.
- Both the real 180s and a 30m ephemeral GitHub runner remain engineering
  evidence ONLY, without authenticated exchange provenance.
- Strict no orders, no PnL or profitability claim, no main deployment.
