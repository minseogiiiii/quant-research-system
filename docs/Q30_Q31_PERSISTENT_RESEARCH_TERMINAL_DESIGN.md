# Q30 / Q31 Persistent BTC/ETH Research Workstation
Status: DESIGN ONLY. No deployment, no paid infrastructure, no trade permissions.
Baseline: fbf78e0b18e3a4c8224160d5ee5b4a2224b8ff80.
Q29.1 run 45307816872 is the independent 5-hour acceptance gate; do not
alter its commit, runner or evidence. Production admission remains blocked
until its exact result and durable artifacts pass review.

## 1. Product objective
A private, keyboard-first, dense, Bloomberg-inspired research workstation
for BTC-USD and ETH-USD on the existing Coinbase source. It is not a clone
of Bloomberg's licensed content or trademark. No order controls or API
credentials for trading. Keep React/Funnest charts and the verified
collector, market engine, archive, independent replay, SSE contracts.
Historical, replay and live modes must be explicitly distinct.
Do not show an executable alpha or a trade recommendation without
independently admitted research evidence.

## 2. Existing assets and gaps (reviewed baseline)
- Existing v36_2_btceth_live.service and v36_2_durable_launch.sh: 86400s
  source, 40 GB raw, 55 GB archive, 10 GiB reserve, distinct workdir,
  non-root user, systemd hardening and Restart=always. No production
  Q24/Q25/Q27/Q28 integration yet in that launcher.
- v36_live_gateway binds loopback, 8 persistent SSE subscriptions,
  max stream 300 seconds. No public network exposure.
- LiveTerminal.tsx already has Funnest completed receiver-time candles,
  top of book, depth, tape and source-admission failure UI. Existing
  ArchiveReplayPanel and FactorLabPanel are retained, not rewritten.
- v36_3_durable_archive_gate.py supports large bounded archive verification:
  up to 20,000 chunks, 30M messages and 80 GB total bytes. Actual 24h
  replay resource profile is not yet established.
- Q24 close probe currently caps 480 events / 190,000 bytes and emits
  progress every 60 seconds. A full day could generate 1,440 progress
  events alone. Q30 MUST implement bounded segmented diagnostics or
  compact periodic aggregation with preserved first-close evidence,
  explicit limits, completeness and ordering. Do not silently truncate
  a full-day witness and report it healthy.
- Q21 host witness and durable archive gate exist but are not an actual
  permanent-host 24h certification.

## 3. Architecture (single host first)
Private browser via WireGuard/Tailscale or authenticated TLS reverse proxy
 -> same-origin private React build + read-only API/SSE
 -> loopback v36 market gateway (no direct external access)
 -> validated in-memory state / session isolation
 -> original Coinbase collector with Q24/Q27 opt-in, Q25 observer guard
 -> immutable chunk archive + manifests.
Parallel bounded services: operator health monitor, persistent host witness,
Q28 forensic receipt and local evidence seal, read-only query index,
isolated offline replay/research worker. Remote independent backup is
restricted and hash-verified; Q29 evidence format extended for host identity,
not falsely reusing a GitHub run ID. Exchange payloads remain private.
No broker service, API order credentials, FIX, paper matching engine or
trading endpoints in Q30/Q31. No public internet access to the operator
Unix socket or raw archive. No multi-node Kafka/Kubernetes requirement.
Use systemd and cgroups; separate capture, query/replay and frontend
resource limits. Keep replay jobs off collector's hot path and pause
them on CPU, memory or disk pressure. Use UTC and monotonic time,
retain source and receiver timestamps separately.

## 4. Capacity and retention
Initial candidate: 4-8 vCPU, 16 GiB RAM, 500 GB dedicated persistent SSD
plus OS volume; 1 TB if longer local retention is needed. These are
design sizing assumptions, not benchmark results or provider quotes.
Prior Q28 30m archive ~172.2 MB implies ~8.27 GB/day by linear
extrapolation only. Market volume changes. Preserve existing 40 GB raw
and 55 GB archive maximum per 24h capture plus 10 GiB free reserve.
Allow concurrent active capture, sealed day, independent replay scratch,
logs and retained evidence. Seven local days at worst-case 55 GB/day
already use 385 GB before overhead; enforce admission/retention by actual
free bytes and size, not blindly by days. 30-day worst-case 1.65 TB before
overhead; offload verified cold archives or expand volume. Never delete
unverified or quarantined evidence automatically. Alert before reserve
breach, stop admission if insufficient space. Store manifest and evidence
hash pins separately; verify backup restore before eviction. Raw archive
authoritative, optional Parquet/DuckDB index rebuildable; no index is a
substitute for the source archive.

## 5. Permanent source lifecycle and two distinct outcomes
Service availability is not dataset continuity. A healthy source epoch
requires both BTC and ETH fresh snapshots, required acks, heartbeat,
sequence and state validation. Any gap revokes both books, hides live
values, invalidates old SSE cursors and closes the affected segment.
Restart/backoff is bounded; use systemd start-limit and alert on repeated
failures. A successful re-subscription is a new source epoch, not a
continuation of the prior archive. A planned 24h rotation also creates
a new session; do not pretend continuity across boundaries.
For research, admit each sealed segment independently according to its
scope; never stitch across unverified gaps. Record intentional rotations
and incident recovery separately.
24h acceptance: exact source SHA, original 278 source pins, 86400s
requested and >=86395s verified source span, no reconnect/gap/watchdog
intervention, full manifest and large archive audit, independent primary
and secondary final BTC/ETH book agreement, pinned host witness
(Q21 minimum 85200s, <=120s sample gaps), resource limits, disk reserve,
SSE load and verified evidence retention. Fault injection runs are
separate from clean acceptance: SIGKILL, heartbeat-only silence,
no initial heartbeat, sequence gap, disk pressure, host reboot,
network partition, reverse proxy restart, slow SSE client, expired
authentication, and backup restore. Record RTO per trial; never use
recovery to excuse clean-run failures.

## 6. Terminal information architecture
Desktop 1440p primary, usable 1080p with collapsible dock and tabs.
Dark charcoal background, orange accent for selection/commands,
muted gray metadata, green/red only for market direction (also text
and signs), separate amber/red operational warnings. Numeric tabular
font, compact rows, keyboard-first. No decorative marketing cards.
Top command bar: symbol, command, workspace, source mode, source epoch,
data age, clock, health. Left watchlist: BTC/ETH venue-pair, bid, ask,
spread bps, OFI, trade volume, age. Center: Funnest candle chart and
linked volume/feature panels, chart interval selector, cursor and
receiver/exchange time context. Right: 10-level book, spread,
microprice, depth imbalance and trade tape. Bottom: research or replay
dock, data-health timeline, incident log, session archive and provenance.
All panels carry product, mode, timestamp/age, epoch or dataset identity.
Explicit disconnected/reconnecting/untrusted state shows no current
quote, never stale numbers masquerading as live. Historical values may
be shown only with an unmistakable historical label.
Suggested commands:
MKT (market overview), BTC/ETH (symbol), CHART, BOOK, TAPE, MICRO,
REPLAY, DATA (lineage and coverage), HEALTH (operator view),
LAB (factor experiments), RUNS (research registry).
Command palette is local, allowlisted and keyboard navigable; no shell
execution or order placement. Workspace layouts saved as UI preferences.
Two-symbol comparison, normalized returns and lead-lag research later
only with time alignment and no cross-epoch features.
Existing 8 SSE slots and 300s rotation limit: a shared connection per
symbol per browser, not per widget; multiplex UI state in client,
verify reconnect/resync, visibility/cleanup and stale behavior. More
users require a separately validated fanout relay, not simply raising
gateway limits. Reverse proxy SSE must disable response buffering and
have suitable timeout, authentication, TLS and rate limiting.
No unsupported claim of tick-by-tick fidelity if SSE intentionally
samples or batches canonical state; raw archive is the replay truth.

## 7. Quant research data contracts
Canonical read-only market state remains versioned by product/session/
state-version. For historical and research, store source archive hash,
source/receiver timestamps, segment ID, feature definition/version,
availability time and sample coverage. OHLCV is completed receiver-time
trade-derived. OFI, depth imbalance, microprice, spread and realized
volatility use only available observations; label staleness and gaps.
First research workflow: choose admitted dataset -> inspect coverage ->
generate causal features -> select prediction horizon -> time-ordered
train/validation/test with leakage controls -> cost sensitivity ->
register immutable experiment and reproduce. No confirmed executable
alpha until independent out-of-sample evidence. Paper execution and
risk governor belong to Q32 after read-only release.

## 8. Work packages and release gates
Q30-A (can develop while Q29 runs): isolated persistent-launch wrapper,
config schema, permissions/loopback checks, host witness adapter,
24h-safe forensic budget/rotation, explicit retention/backup design.
Unit/synthetic tests for no-opt-in, incorrect permissions, missing
manifest, malformed clock, disk reserve, trace rotation and SIGKILL.
Q30-B (after exact 5h accepted): provision authorized Linux, staged
install with pinned source, chrony/NTP, private TLS/VPN, firewall,
verify read-only route and systemd sandbox, 180s then 30m host tests.
Q30-C: separate controlled host fault drills, 24h clean run, full
archive+replay and externally retained witness/hash checks.
Q31-A (parallel UI design only): existing React layout refactor in a
separate branch, command palette, linked panels, explicit modes and
responsive desktop workspace. Mock data labeled simulation.
Q31-B: use actual admitted source; browser Playwright DOM vs REST/SSE
parity, gap/resync, chart/tape/book integrity, 8-client SSE capacity,
keyboard and accessibility tests, auth/HTTPS/rate-limit checks.
Q31-C: read-only release review with immutable build/source receipts,
rollback plan and independent operator sign-off. main not merged
automatically. No public launch or live orders.
Q32+ only after explicit gate: separate paper simulator/risk governor,
cost model, OOS research and future authorization.

## 9. Operational checklists
Before deployment: approved Q29.1 artifact verification, explicit host
authorization, sizing and storage budget, pinned dependency lockfile,
service unit review, least privilege, firewall, authenticated endpoint,
clock sync, disk reserve, backup encryption and restore test.
Every clean run: start/end times, source SHA and manifest pins, boot/
process/session witness, source and heartbeat counters, no gaps,
resource metrics, SSE validation, forensic receipts, replay reports and
durable evidence retrieval check. On failure: stop release, preserve
private evidence, classify observed events only, avoid root-cause
claims without data, rerun only after specific remediation.
