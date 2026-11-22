# Q23: evidence-led remediation after Q22 early source failure

## Verified previous failure, not a five-hour certification

Q22 run `45005526920` failed at `Q22Rejected:PRIVATE_SOURCE_OR_TRANSPORT_UNHEALTHY`
after 9,274 seconds and 13 completed eight-client SSE windows, not 18,000s.
The last five-minute update had 364,331 messages, ~202MB tracked RSS and
~93.9GB available disk. The original source/collector state was deleted by
fail-closed cleanup, and no full replay was performed. The precise Coinbase
source vs observer classification was NOT recoverable from the original result.

## 1. Diagnose the exact private operator violation

`q23_redacted_diagnostics.py` speaks only to `v36_monitor.sock`, only
`/api/v36/health` and `/api/v36/metrics`, with a fixed Content-Length
bound. It accepts HTTP 503 only as failure evidence, never as a green health
sample. The monitored predicates retain unchanged fail-closed continuity rules:
one session, connection ID 0, gap count 0, strictly increasing messages,
monotone heartbeats, <=15,000ms receiver age, two healthy product snapshots,
8 SSE max and trading disabled.

On any Q22/Q23 supervisor fault, a **bounded JSON receipt** is uploaded
(`q23_redacted_fault_receipt.json`) with stable enum codes, redacted engine
status/reason enum, session SHA-256 (not session ID), numeric sequence/gap/
heartbeat/message counters, receiver age, active SSE count, and only the last
16 sanitized resource/source samples. The receipt contains no market wire
frames, quotes/prices, original session ID, token, stdout or stderr.
A 503 or unreadable private monitor is always a failure.

## 2. Force disconnection and re-session in short CI

An existing independently implemented actual Node collector plus synthetic
WebSocket fault-injection test
`test_v36_2_transport_gap.py` is executed explicitly. It injects a
WebSocket disconnect, rejects the previous session's cursor, and checks that
a new connection must obtain new dual snapshots and a new session. Q23 new
tests also exercise named 503, session mismatch, gap, missing progress, and
redaction. These synthetic tests never certify real Coinbase continuity.

## 3. Bounded large-archive replay

The original Q14 `v35_replay_service.py` defaults remain untouched. Its
10k-state limit is **insufficient for a five-hour 1Hz state replay**.

Q23 creates two distinct isolated replay subprocesses. Only the primary
private 5h worker increases `MAX_ROWS_PER_PRODUCT` from 10,000 to a finite
30,000 records (covers up to 8.3h at 1Hz). Both separate workers enforce
3GiB virtual memory, 1,000 CPU seconds and an 1,100s host wall-clock timeout
for each worker. They first SHA-pin the captured manifest, never place orders,
emit private 0600 bounded JSON receipts, and remove those receipts even on
failure. Independent replay uses its existing separate L2 interpretation.

Before another 5h run, a deterministic **synthetic >10,000 states per BTC
and ETH** must show that the original primary verifier fails on its old
10k limit and the isolated primary verifier admits the extended history.
This fixture is NEVER labeled as LIVE Coinbase data.

After a genuine source completes: durable archive gate first, each independent
worker second, manifest pinned after each, then equality of source message
counts, chunk counts, BTC/ETH bid, ask, and book-depth levels. Only bounded
worker RSS and SHA receipts leave the ephemeral runner. A forced kill, memory
exception, worker timeout, state disagreement or unverified manifest is FAIL.

## Release and re-run policy

`main`, Q14 source Git blobs, v36 24h operator production budgets and
live-order paths remain unchanged. Q23 preflight must be SUCCESS at the
exact proposed commit, including Ruff, existing fault contracts, >10k test,
actual 180s public Coinbase + eight-client SSE and isolated dual archive
replay. A separate explicit ARM commit may then trigger exactly one
non-retrying 18,000-second GitHub Action. No automatic retries or false PASS.

**Important:** Q23 cannot retroactively recover the failed Q22 source
event. The next real dropout can produce the missing redacted fault evidence.
The true P0 five-hour gate remains FAIL until one full new 5h source and
post-capture validation succeeds.
