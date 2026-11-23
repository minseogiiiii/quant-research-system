# Q24 P0-A: Evidence of WebSocket termination, without opening orders

## Incident justification

- Actual Q22 run 45005526920 stopped after ~9,274s with generic private source health failure.
- Actual Q23 run 45085616616 stopped after ~3h50m, 18 completed SSE
  load windows and >585k observed messages. The supervisor recorded
  `SOURCE_CONNECTION_GAP`, `CONNECTION_ID_CHANGED`,
  `SOURCE_SESSION_CHANGED`, `HEARTBEAT_REGRESSION`, and
  `MESSAGE_OR_SEQUENCE_INVALID`. Those last two counters were reset
  by a new source connection, not proof of disk corruption.
- Source of the WebSocket closure remains **UNKNOWN**. Coinbase remote
  termination, intermediary network loss, GitHub runner network,
  local stale-feed fail-closed shutdown and other transport errors
  have not yet been distinguished by actual close evidence.

## Implementation boundaries

The frozen, independently pinned Q14 Coinbase collector
`btc_eth_terminal/scripts/btc_eth_multiday_v20.mjs` is NOT edited.

`q24_ws_close_probe.mjs` is loaded using an explicit opt-in Node
`--import` on a private ephemeral CI runner. It wraps the global
WHATWG WebSocket constructor and attaches passive lifecycle observers:
`SOCKET_CONSTRUCTED`, `SOCKET_OPEN`, `SOCKET_ERROR`,
`SOCKET_CLOSE`, `LOCAL_CLOSE_REQUEST`, periodic `PROGRESS`,
and `PROCESS_EXIT`.

Evidence recorded:
- WebSocket close code (integer) and clean flag when available
- Local close requested vs close event without an observed local request
- Monotonic elapsed time, open duration, reconnect time since prior close
- Number and age of received messages and Coinbase heartbeat-channel
  messages at the close/error event
- Native control-frame visibility: **false**. WHATWG WebSocket does
  not expose ping/pong control frames, so this implementation does not
  invent measurements or turn absent data into a PASS.

The collector's original raw archive, channel validation, sequence
tracking, gap quarantine, source-readiness and trade permission policy
remain unchanged. No collector restart or zero-gap waiver is added.

## Privacy and evidence integrity

The Node sidecar is an opt-in file below the source workdir, mode 0600
under a 0700 parent. Each append is strictly fixed-schema numeric,
boolean or enumerated data. The file is bounded to 480 events and
190k bytes. It NEVER stores original WebSocket endpoint, raw source
frames, prices, headers, credentials, client identifiers, error.message,
or untrusted WebSocket close reason.

Python validates **every event**, rejects untrusted extra keys,
nonmonotone counters, unsafe file links and permissions, and emits only
`q24_redacted_ws_close_receipt.json` into public GitHub artifacts.
The private trace is deleted with all raw market archives on CI cleanup.

If evidence is missing or malformed, the public record is marked
`TRACE_UNAVAILABLE_OR_REJECTED`; it must not be reported as a source
health PASS. Any close classification remains observation only:
`root_network_cause_proven=false`.

## Short testing requirements before a new long-duration run

1. JS synthetic socket close 1006, local close 1000, error, heartbeat,
   constructor replacement and zero secret leakage.
2. Python redaction/integrity tests: malformed schema, forged reason,
   unsafe links, bad numeric bounds and truncated trace all fail closed.
3. Existing actual collector with **synthetic mock source**, forced
   disconnect and reconnect, original capture not admitted to research,
   and a separate private close receipt with two socket creations.
4. Frozen 278 Q14 Git blob manifest unchanged, original Q22/Q23
   regression tests and all Q24 tests pass.
5. Genuine Coinbase 180s smoke with eight-client SSE, private close
   lifecycle witness, archive SHA and two independent final-book
   interpreters all PASS at the same source commit.
6. **Do not initiate an additional 5h or production release** solely
   because a 180s smoke passed. After a future actual disconnect,
   review specific cause evidence; then plan mitigation and a new
   five-hour acceptance separately.

## Interpretation guidelines

- External/unrequested close event: a socket close was observed without
  the collector calling close first. Does not prove Coinbase caused it.
- Local close request after 12s+ silence: consistent with local stale-feed
  behavior, but not proof of underlying network cause.
- Error preceding close: generic WebSocket error event was observed;
  its potentially sensitive message and URL are deliberately excluded.
- Close code 1006: abnormal closure was **reported** by the runtime;
  1006 is reserved and not a close code sent in a peer close frame.
- Heartbeat age and receiver-message age are source-channel timing,
  not observable native WebSocket ping/pong.

No real orders, paper PnL, verified alpha, five-hour zero-gap
acceptance, signed Coinbase source attestation or 24h host certification
are provided by P0-A.
