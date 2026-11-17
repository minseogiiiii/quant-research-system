# v35 Archive Ingress Gate: Verified Engineering Increment

Date: 2026-11-16. This is a bounded extension of the **existing** BTC/ETH
Unified Quant Research Terminal. It does not substitute for the inaccessible
standalone `v35_live_state.py` or `v35_live_api.py` implementation.

## Tested source

Frozen authoritative v34.1 parent ZIP SHA-256:
`8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8`.

Original public Coinbase BTC-USD / ETH-USD capture archives from the frozen
parent were independently inspected by `backend/v35_archive_ingress.py`.
The SHA-256 of each existing manifest is supplied *externally*, not
self-authenticated from its own contents.

| Original source | Manifest SHA-256 | Messages | Chunk count | Ingress result |
| --- | --- | ---: | ---: | --- |
| Clean v14 120s | `8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5` | 5,177 | 28 | VERIFIED_ARCHIVE_ENVELOPE |
| Injected-disconnect v14 120s | `35e024fc87b317ebcf06c452562ef809a996bcc350c581356b7394c8022e5955` | 5,164 | 31 | QUARANTINED_ARCHIVE_ENVELOPE |
| Original v16 one-hour | `08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7` | 152,069 | 41 | VERIFIED_ARCHIVE_ENVELOPE |

One-hour archived JSONL bytes: 325,724,466. Original raw-text bytes:
258,396,723. Measured 3.7084 seconds for a local single-process verification
of this archived source; this is a machine-specific measurement, not a
deployment latency or a throughput guarantee.

## Exact code

- `btc_eth_terminal/backend/v35_archive_ingress.py`: SHA-256
  `a9d070dd5e1e31d76dc3a3794eac722789a198e3d699e792f831d9bf112ea4c8`.
- `btc_eth_terminal/tests/test_v35_archive_ingress.py`: SHA-256
  `d769d8b1e2ed2c6b8fff1ec7b28dbd156b2d88d99c16ffdc25704b581a430075`.
- `.github/workflows/v35_archive_ingress.yml` is an independent Python
  contract-only CI job.

Local evidence: 16/16 test methods pass when both original v14 120s source
directories are explicitly supplied through `V35_CLEAN_ARCHIVE` and
`V35_DISCONNECT_ARCHIVE`. Test suite covers external manifest pinning,
chunk and raw envelope SHA, full receiver-order and per-connection sequence
consistency, independent snapshot-before-update rules, cross-product isolation,
malformed metadata, and symlink/path-traversal rejection.

[CI run 43985641849](https://github.com/minseogiiiii/quant-research-system/actions/runs/43985641849)
passed at commit `52a8232736e64e6842846ed7010dd5dfdf29c2b3`.
The GitHub checkout does not contain source archives: **14 synthetic methods
pass and the real-source test class is skipped** (unittest reports one skipped
class). Do not interpret CI as a real-source validation.

## Invariants and missing gates

`VERIFIED_ARCHIVE_ENVELOPE` proves a matched, bounded receiver-order source
envelope only. All certificates state
`mode=ARCHIVE_REPLAY`, `live_feed=false`, `trading_enabled=false`,
`execution_authorized=false`, `oos_approved=false`, and
`independent_exchange_attestation=false`. Even the clean capture is not
venue-attested complete market truth, and no fresh live source was collected.

The injected disconnect archive is deliberately **QUARANTINED** even though
the persisted files are SHA-consistent. No synthetic market data is presented
as actual or complete capture.

The original v35 standalone candidate ZIP, service/API and exact tests remain
unrecovered. This check must eventually be wired ahead of the original
`v35_live_state` replay ingress, not mistaken for a functioning HTTP/SSE
gateway, live collector, broker or order-book implementation.

Next release gates: recover and inspect original v35 source, integrate via
stable read-only contracts without duplicate collector/book engine,
validate snapshot/SSE/resync/gap and stale modes, pin real browser release,
and rerun full frozen regression against the precise final candidate.
Keep `main` unchanged. `NO CONFIRMED EXECUTABLE ALPHA`.
