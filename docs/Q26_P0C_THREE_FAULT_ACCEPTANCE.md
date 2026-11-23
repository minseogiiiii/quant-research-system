# Q26 P0-C: network disconnect, heartbeat interruption, sequence fault

## Incident and scope

Q22 and Q23 public Coinbase read-only five-hour attempts failed because of
source WebSocket session changes; Q23 lacked a close-cause witness at the time.
Q24 subsequently introduced read-only close code and heartbeat-channel
timing diagnostics. Q25 added a CI-optional epoch isolation guard and proved
that stale books, OFI, trades, candles and SSE cursors are not stitched across
source sessions.

This phase extends the **same unmodified frozen Node collector** with
deterministic, synthetic-only WebSocket fault sources. Tests must not contact
an exchange, place orders, or treat the replay fixture as authentic Coinbase
data.

## Four deliberately distinct cases

| Case | First-source stimulus | Original collector action | Admission |
| --- | --- | --- | --- |
| NETWORK_CLOSE | synthetic unexpected close with reported code 1006 | `CLOSE:1006`, quarantine, reconnect | new session only |
| SEQUENCE_GAP | valid first 7 messages then sequence 11 | `SOURCE:Error: SEQUENCE_GAP`, quarantine, reconnect | new session only |
| ALL_FEED_SILENCE | all frames, including heartbeat, cease | collector `STALE_FEED` after >12s without any frame | new session only |
| HEARTBEAT_ONLY_SILENCE | continuous L2 while heartbeat events cease | **synthetic-only watchdog** forces source rotation at 4.5s; native collector lacks a heartbeat-only watchdog | new session only in opt-in test |

The distinction in the last row is mandatory. The frozen collector's
12-second `STALE_FEED` is an **any-frame-idle timeout**. A healthy stream
of order-book messages can prevent that timer from firing even when the
heartbeat channel stalls. The frozen v36 engine can fail closed after 5
seconds, but that does NOT establish that the original collector will
autonomously reconnect.

The fourth case is therefore a **synthetic, local proof-of-concept
remediation**, loaded by an explicit test-only Node preload. It is not a
production fix or evidence of a successful real Coinbase heartbeat-only
recovery. Any such rollout would need separate operator review and live
safety testing.

## Exact assertions on every recoverable test

1. Old BTC/ETH session first enters `HEALTHY` after two L2 snapshots,
   all three subscribe acks and a heartbeat.
2. Exact original Node collector archives every accepted synthetic frame
   before handing it to the observer. The fault does not silently truncate
   the prior source epoch or reclassify synthetic data as research-ready.
3. Source failure invalidates both books/trades/OFI/candle and SSE memory
   under the existing epoch isolation lock, with old REST snapshots 503,
   old version and SSE cursors 409, and former SSE subscription instructed
   to `resync_required`.
4. A new session starts at connection ID 1, per-connection sequence 0,
   with BTC snapshot and acks/heartbeat **before ETH snapshot**. During
   this deliberately delayed period neither product is admissible.
5. Only after the ETH snapshot does the engine become `HEALTHY`,
   publish two new-session books and reject old-session history.
6. A trusted, **private**, fixture manifest gives the exact expected
   fault classification. A Q24 read-only WS trace independently records
   expected close code and local close-request semantics.
7. Measure monotonic gap-to-recovery and WebSocket construction delay;
   bound collector-gap-to-healthy to 5 seconds and close-to-reconnect to
   4 seconds. For all-source silence, source stale detection occurs before
   the 12-second collector watchdog, so measure its extra silent period
   separately rather than hiding it.
8. Synthetic manifest is `ready_for_research=false`, no orders enabled,
   no gap falsely admitted as uninterrupted, and private raw frames/URLs,
   error messages and process logs are never uploaded.

## CI acceptance

- Frozen Q14 278 source Git blobs verified against an immutable manifest.
- Q22/Q23/Q24/Q25 regressions remain green.
- All four Q26 scenarios pass on an actual unchanged Node collector with a
  mock socket; their four bounded fixture summaries must all exist.
- Independent **genuine public Coinbase 180-second smoke** at the same SHA:
  8 SSE clients, Q24 private close diagnostics, Q25 epoch isolation, private
  archive pinning and two independent BTC/ETH final books match.
- No long-running Q23/Q22 workflow trigger, no modifications to `main`,
  no live/paper trades or economic alpha claim.

## Known limitations

- These are simulated transport events, **not** a real network packet drop,
  a genuine Coinbase sequence violation or a real remote 1006 frame.
- WHATWG WebSocket does not expose native ping/pong control frames. The
  heartbeat signal is a public Coinbase `heartbeats` channel event.
- 1006 is a local abnormal-close indicator, not a code physically sent by
  the exchange. Neither synthetic 1006 nor a real future 1006 proves the
  remote exchange caused a network outage.
- P0-C short acceptance is not five-hour zero-gap certification, not
  24-hour permanent Linux host reliability, and does not prove profitable
  or executable trading strategies.
