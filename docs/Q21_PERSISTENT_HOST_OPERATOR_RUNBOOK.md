# Q21 P0: persistent Linux BTC/ETH observation and host witness

**Status: OPERATOR EVIDENCE TOOLING CANDIDATE. NOT DEPLOYED. NOT 24H CERTIFIED.**
Use only the existing read-only Coinbase source and the reviewed Q14 market engine.
Existing launch and systemd files are deliberately unchanged by Q21.

## Infrastructure prerequisites

- A persistent Linux VM with a real durable, private filesystem. Do not use an ephemeral container filesystem or an Action runner as 24-hour operational evidence.
- Python 3.12, Node 22, `systemd`, verified network access to Coinbase public spot, synchronized clock and adequate memory.
- One 24-hour source cycle is configured with a **40GB raw budget**, **55GB archive budget** and **10GiB disk free reserve**. Startup must have at least 55,000,000,000 + 10,737,418,240 bytes free, *plus margin* for logs, OS and monitoring. Plan >100GB dedicated volume and higher capacity for archive retention.
- Locked-down `btceth` Unix user. `/opt/btc-eth-terminal` reviewed source must match the exact approved commit and hashes. `/var/lib/btceth-live/captures` restricted. Never put raw Coinbase records or a token into a public repository.
- The operator's private UNIX monitor socket is `<capture-workdir>/v36_monitor.sock` (0600). The public browser API MUST NOT be opened to the Internet until authentication, transport security and rate limits have been separately reviewed.
- The **existing** systemd service uses `Restart=always`, starts a *new* capture on each restart, and retains old captures. Arrange disk capacity, retention/archiving, and stopping/isolating the service for evidence audit. Do not automatically delete any evidence to make room.

## Actual observation procedure

These are instructions for an authorized operator on a provisioned Linux host. Commands below were **not** executed during CI. The source is public market data; no order or private account API is used.

```bash
# As the authorized host operator, after source/install and permissions review:
sudo systemctl daemon-reload
sudo systemctl start v36_2_btceth_live.service
sudo systemctl status v36_2_btceth_live.service --no-pager
# The service must have a nonzero MainPID:
sudo systemctl show -p MainPID --value v36_2_btceth_live.service

# Discover the NEW workdir for this exact run. Do not reuse an old one.
sudo ls -ld /var/lib/btceth-live/captures/*
# On the same host and as the btceth user, after the private Unix monitor
# becomes available, run Q21. Assign CAPTURE to the new exact workdir.
# Assign PID to the service's currently verified MainPID.
sudo -u btceth python3 /opt/btc-eth-terminal/scripts/q21_host_witness.py record \
  --capture-workdir "$CAPTURE" \
  --observer-pid "$PID" \
  --out "$CAPTURE/q21_operator_witness.jsonl" \
  --seconds 85700 --interval 60

# Preserve the emitted witness SHA-256 in separate access-controlled evidence.
# Never alter or repair failed/truncated files.
```

The witness samples the existing **private Unix** health/metrics. At each sample it checks real `LIVE_SOURCE`, BTC/ETH subscriptions, bounded 15s source age, one source session, continuous message counters, no connection gaps, memory/file descriptors, Linux boot identity and >=10GiB free disk. It has zero order functionality.

For a valid candidate, retain the original `V36_OBSERVER_EVIDENCE.json`, finalized `soak_evidence/...` manifest and sealed chunks, exact 24-hour service logs and Q21 witness without changes. The Q21 file is only a witness of the period it sampled (minimum 85,200 seconds), **not** a 24h certificate by itself.

## Independent audits on the host after the collector exits

```bash
# Expected values must be pinned and preserved independently. Do not derive
# a trusted pin from a mutable unreviewed file at the time of auditing.
sudo -u btceth python3 /opt/btc-eth-terminal/scripts/q21_host_witness.py audit-witness \
  --samples "$CAPTURE/q21_operator_witness.jsonl" \
  --external-sha256 "$TRUSTED_WITNESS_SHA256"

# Audit the full 24-hour capture with the separately bounded large-archive
# verifier, NOT the legacy short-capture v36_verify_live_capture.py.
sudo -u btceth python3 /opt/btc-eth-terminal/scripts/v36_3_durable_archive_gate.py \
  --capture "$EXACT_SEALED_SOURCE_DIR" \
  --manifest-sha256 "$TRUSTED_SOURCE_MANIFEST_SHA256"

# Additional independent final-book replay uses a separate source interpreter.
# For full-day archives, memory and run time must be profiled in a bounded
# offline environment. No success is claimed without this actual full run.
sudo -u btceth python3 /opt/btc-eth-terminal/backend/independent_soak_replay.py \
  "$EXACT_SEALED_SOURCE_DIR" --report "$CAPTURE/q21_independent_replay.json"
```

Required manual acceptance evidence:
1. `observer_source_complete == true`, collector exit code 0, no fault injection or reconnect, `duration 86400`, source span >= 86,395 seconds and one continuous source connection. The large archive gate and a **separate actually completed full-day BTC/ETH final-book replay** must both pass.
2. Witness file SHA-256 matches the independently preserved pin; at least 85,200 seconds of monotonic samples; sample gaps <=120 seconds, same boot ID, process PID, and source-session hash; all samples healthy, no stale market, disk pressure or resource limits.
3. Separate operator review of host service restart/stop history, disk usage, process logs, external network reachability, and source data lineage. A **host interruption** or restarted source cannot be labeled the same uninterrupted 24h trial.
4. Q18 30-minute continuous-source plus segmented eight-client SSE acceptance also needs a final **PASS**. A failed/queued/in-progress CI workflow is not proof.
5. Controlled host fault drills (SIGKILL, restart, disk pressure, SSE overload) must be run as **separate negative trials**; their faulted captures must be quarantined and must NEVER be combined with a clean 24-hour source artifact.

## Explicitly deferred

- Q20 notebook UI, P1 improvements, broker APIs, trading execution, alpha claims.
- The legacy `v36_verify_live_capture.py` uses the original short-archive `v35_archive_ingress` defaults (including 1GB total input), so it must not be presented as a valid 24-hour archive gate. Use the large-archive `v36_3_durable_archive_gate.py` and independently replay final books after separate resource profiling.
- An unattended process is not 24-hour operational certification without the complete immutable archive and external host evidence.
- No public site has been deployed by this Q21 change. No services provisioned or paid resources created. No `main`, `integration`, or `staging` merges.

**Q21 verdict remains BLOCKED for production until real operator evidence and explicit human read-only release approval.**
