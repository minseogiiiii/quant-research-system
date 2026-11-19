# Q7 read-only integration: promotion gate (NOT MERGE READY)

This branch is isolated from `main` and the historical `integration/btc-eth-unified-terminal` HEAD. It does not contain the complete Q7 code yet. Do **not** merge as a Q7 release.

## Frozen lineage
- GitHub R5 integration base: `64aa85081cab73c917333249eb8e200c8d4044fa`.
- Offline Q7 full release SHA256: `0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15`.
- Offline Q7 incremental-from-Q6 SHA256: `dc03f05465cbcf49f04dda6a8ae10fd69c3e178f0300f40b8974b3762d847132`.
- Offline **source-only**, no raw market data, promotion bundle SHA256: `561a59c2b6fd4991bc3c18cd7198dd5d09d5dc92946c87ee85dcdace4904966e`, 278 files, 2,005,755 source bytes.

The full Q7 release is a verified local descendant of R5; it is **not** present in the R5 GitHub tree. The source-only transport bundle is not a deployment artifact. It excludes private raw Coinbase data, historical projections/research report, compiled distributions, and historical regression logs. A separately controlled immutable fixture/source release is needed for full Q7 runtime acceptance.

## Newly executed LIVE qualification on frozen common components

GitHub Actions run [44611980247](https://github.com/minseogiiiii/quant-research-system/actions/runs/44611980247) used the *R5 public Collector and 8-client SSE verifier*, whose Git blob hashes match the frozen Q7 source:

- Actual public Coinbase source span 175.792 seconds, 8,135 newly received messages, 5 sealed chunks, 35,277,207 bytes, 1 connection.
- 8 SSE clients, each 97 or 98 state messages, zero unexpected resync; health p95 1.37ms over 249 samples.
- Independent final BTC/ETH book replay and sealed archive gate PASS.
- Raw JSONL remained private and was not published; all order-writing remained disabled.
- SHA256 of captured Coinbase manifest: `2c69a6d6544c8bc4ff48ffcc1e623504390e5bf4385d1b9dc8f9028fe881cbc5`.

**Scope limitation:** Actual LIVE evidence applies to the shared R5 collector/reader and gateway as tested, NOT complete Q7 Observer, Factor Lab, frontend, Q7 real-browser E2E, 30-60min, a persistent host, uninterrupted 24h or fault tolerant 24h. No independent market-wide sequence/clock attestation. This branch is not production ready.

## Mandatory independent gates before merging Q7 to integration

1. Stage and review the exact 278-file source-only bundle into this isolated branch; exclude raw market data and credentials. Verify SHA and file manifest.
2. Reconstruct missing Q6/Q7 dependencies with frozen source ancestry and independently review code changes, especially Gateway, source health/nonce handling, Factor Lab report pins, and read-only behavior.
3. Run clean pinned Node 22 TypeScript/Vite build to versioned `dist_v37` without overwriting frozen legacy `frontend/dist` or `dist_v36`; verify reviewed SHA pins. Restore immutable runtime source/report fixtures separately and verify their SHA before tests.
4. Run full applicable Python and Node tests with exact historical archive fixture inputs; independently unpack candidate and reproduce critical tests. Record skips/fails; no code deletion or blind pin changes to make tests pass.
5. On authorized persistent host run 30-60min actual Q7 BTC/ETH `level2`, `market_trades`, `heartbeats` and true Chromium HTTP/SSE parity. Verify source-session isolation on disconnect and recovery, disk and resource monitoring.
6. A durable `main` release additionally needs service lifecycle, 8-client sustained test, archive recovery, 24h uninterrupted single-source and separate 24h fault-recovery proof, rollback and a strictly read-only policy.
7. Paper Trading and real-money execution are separate projects requiring explicit future authorization. Current Q7 verdict: `NO CONFIRMED EXECUTABLE ALPHA`, order APIs disabled.

**Current status: SOURCE_ONLY_BUNDLE_READY_OFFLINE; COMMON_COLLECTOR_REAL_LIVE_176S_PASS; GITHUB_Q7_SOURCE_NOT_UPLOADED; INTEGRATION_AND_MAIN_UNCHANGED.**
