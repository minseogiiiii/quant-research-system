# Q29.2 resilience incident audit (not release acceptance)

Base: fbf78e0b18e3a4c8224160d5ee5b4a2224b8ff80. Failed run 45307816872, artifact 14886603218.
Artifact SHA-256 verified: 5a133fb0f0e3c8b00643dbfdf45455053b557e518685d5161d9c04aeca7b213f.

Observed at 3483.02 s: health HTTP 200 / HEALTHY, connection 1, one gap, new session digest, cumulative messages 159711, epoch sequence 279, epoch heartbeats 6, receiver age 37 ms. Prior heartbeat count 3467; prior messages 159145. This proves a new session was admitted, not zero-gap continuity. Physical trigger is unknown. Q24 rejection NONMONOTONE_OR_INCONSISTENT_WS_TRACE, Q27 rejection PRIVATE_TRACE_SOCKET_INDEX_INVALID. Both parsers assumed globally increasing socket index even for late callbacks; the Q29.2 fixture reproduces a plausible mechanism, not proof of the lost historical event order. Preserve global event order, monotonic clock, consecutive construction identity, per-socket counters, schema/privacy and no-order invariants.

## Admission gates (all pending until independently evidenced)
- Regressions: late close/error/cleanup, unknown/duplicate socket, clock/count regression, SIGKILL, malformed trace, privacy, prior Q22-Q29 recovery and evidence tests.
- Durable independent incident journal: exact bounded failure reason, pre-shutdown checkpoint, append/fsync and crash recovery, retained immutable gap ledger. Test disk exhaustion, partial writes, process loss and watchdog/event-loop stalls.
- Separate resilience supervisor from strict zero-gap supervisor; never certify lost intervals. Validate old-session revocation, dual fresh snapshots, acknowledgements, heartbeat, epoch-local sequence, monotonic cumulative counters, SSE resync, stale state suppression and archive replay per epoch.
- Parallel independently collected sessions: compare common event IDs only when protocol guarantees equivalence; otherwise independent state checkpoints and provenance, never infer gapless history from snapshots. Fault isolation across processes/hosts, bounded retries and backoff; test simultaneous outage, disagreement, duplication, out-of-order, and failed failover.
- Live 180s then 30m; exact evidence round-trip. Independent controlled real-compatible fault harness plus a 5h resilience soak; strict 5h zero-gap acceptance stays separate and unchanged. Persistent 24h after both gates, no live orders.

This commit only remediates diagnostic parsing and adds regression tests; it does not implement redundant ingestion or certify resilience, continuity, profitability, merge or release.
