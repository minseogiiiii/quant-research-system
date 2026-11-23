# Q28: Crash-resilient first-connection close and heartbeat guard forensics

## Confirmed incident

Q27 actual Coinbase public-source 30-minute Actions #45129482392
failed at ~991.28 seconds after 39,735 messages and 2/3 SSE load windows.
The real source connection ID rotated from 0 to 1 and failed the existing
strict zero-gap supervisor. It **correctly failed closed**. The archive
was deleted by private runner teardown, without independent full replay.

The redacted Q24 and Q27 receipts were
`TRACE_UNAVAILABLE_OR_REJECTED`; the underlying errors were not exposed,
so the source of the first WebSocket closure remains **UNKNOWN**. The
existing Q27 guard may have requested a local close due to missing
Coinbase application heartbeat, or the source could have disconnected for
other transport reasons. We must NOT attribute the event to Coinbase,
a GitHub runner or the Q27 guard until observed evidence distinguishes it.

## Root cause of loss of diagnostic usefulness

Q27's normal receipt validator demands a final `PROCESS_EXIT` event.
Fail-fast teardown signals the observer, which then SIGTERMs the Node
collector. Node does not promise an application `exit` callback on SIGTERM,
so a valid prefix may lack that final marker.

The same abrupt termination can interrupt a final synchronous JSONL append.
Q24's and Q27's normal validators reject a partial final line; they were
previously the ONLY public receipt paths. That is correct for success,
but needlessly discards independently fsynced earlier event records in a
failed run.

## Exact Q28 change

No changes to frozen v20 collector, v36 backend, Q25 market quarantine,
Q27 heartbeat policy, Q23 5-hour arming or real-order capability.

- Q24/Q27 receipt parsers retain their strict existing defaults.
- An explicit `preserve_crash_prefix=True` mode is used **only** to
  examine a terminated collector's crash trace. It reads the private
  root-owned mode 0600 file with hard-link and symlink protection,
  checks consistency across fstat and lstat, and discards at most an
  *uncommitted trailing JSONL line* while parsing and validating every
  complete newline-delimited event. An invalid COMPLETE event or unknown
  field still causes the WHOLE source of evidence to be rejected.
- Q28 combines FIRST socket Q24 `SOCKET_CLOSE`,
  `LOCAL_CLOSE_REQUEST`, `SOCKET_ERROR` and Q27
  `HEARTBEAT_STALE_FORCE_CLOSE` / `FIRST_HEARTBEAT_TIMEOUT`
  into a narrow enumerated **observed** classification. It never
  treats close code 1006 as a peer-sent code or assumes the exchange
  caused a connection interruption.
- **Event-order correction discovered in actual OS SIGKILL testing:** the
  unchanged collector's `fail('CLOSE:1006')` handler calls `ws.close()`
  to clean up after the remote-style close event was already emitted.
  Q24 therefore also sees a local close request; interpreting its
  eventual boolean as the ORIGINAL close cause was wrong. Q28 now checks
  the strictly validated `seq` ordering. A local close request
  **after** `SOCKET_CLOSE` is classified as cleanup, not a watchdog
  intervention or evidence of local original causality. This is covered
  by a dedicated synthetic negative test and a real OS SIGKILL test.
- Strict failure scenario: source gap always remains a FAIL. Even if
  crash-prefix diagnostics parse, `source_continuity_certified=false`,
  `network_root_cause_proven=false` and `real_orders_enabled=false`.
- If either trace is invalid, the receipt uses a fixed error-code enum.
  Missing files are not interpreted as proof of zero interventions.
  No private market frames, endpoint, IP, connection URL, token, raw
  error message or close reason appear in the public JSON.
- Supervisor reports the bounded `q28_redacted_incident_receipt.json`
  after collector termination only if `Q28_FORENSICS_ENABLED=1`.
  This is independent of normal Q24/Q27 evidence and does not
  suppress the original fail-closed exception.

## Required tests

1. Synthetic Q27 watchdog local close + actual Q24 close events classify
   as `LOCAL_Q27_HEARTBEAT_GUARD_CLOSE_OBSERVED`.
2. Independent unexpected 1006 and generic error + close classify only
   observations, without inferring a remote/network cause.
3. Real unchanged Node collector + synthetic Q27 heartbeat-only fault:
   SIGTERM afterwards must still yield committed first-close evidence.
4. Forensic incomplete tail and missing final exit preserve earlier valid
   events; normal healthy validation continues to reject truncated lines
   and missing final process-exit.
5. Malicious extra keys, untrusted close reasons, owner/mode and link
   violations are rejected; never print source raw strings or stack traces
   that contain private paths or endpoints.
6. Same-SHA genuine Coinbase 180s read-only smoke, 8 SSE clients, source
   zero gaps, Q24/Q27 strict complete traces, Q28 supplementary receipt,
   preserved Q14 278 pinned blobs and two independent final orderbooks.
7. ONLY after this, a separately gated 30m attempt with 3 real SSE windows,
   bounded resources and final independent archive replay. If it fails,
   Q28 should retain the first-close classification while every source
   admission gate remains FAIL.

An ephemeral 30m success would not establish 5h zero-gap continuity,
24h host reliability, signed exchange provenance or economic alpha.
