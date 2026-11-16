# v35 read-only state service: engineering candidate

Status as of 2026-11-16. This branch is an **incomplete staging mirror**, NOT the authority for the runnable v35 candidate.

## Frozen parent and verified scope

- Frozen standalone v34.1 parent: SHA-256 `8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8`.
- Standalone v35 candidate: SHA-256 `ed3e2ffde384c977781e833ddc6af6e3f8bdaf6eaebd94549d7b809d20cce506`.
- v34.1 to v35 patch: SHA-256 `37b3f9bd2a990a3bb33e2dd46f42b1dbca7bd0682d3421a9faf43d836a16fe2d`.
- Exactly 210 existing `backend/data/` and four pinned older React/Funnest frontend files are unchanged.
- Archive replay of actual Coinbase BTC-USD and ETH-USD JSONL into a separate read-only state process was exercised. HTTP/SSE snapshots and resync were tested on loopback; **no new actual live Coinbase capture has been verified**.
- Separate state and gateway code exists in the standalone release under `backend/v35_live_state.py`, `backend/v35_live_api.py`, `tests/test_v35_*.py`. No broker/order adapters are enabled.

## Deployability and CI gates

**This staging branch has not been synchronized with full v34.1/v35 backend code and raw archives. It is NOT a standalone runnable deployment.**

The newly edited `frontend/src/unified.tsx` mode selector has **not** produced a pinned new browser release. The deployed archive still serves the previous pinned historical Funnest UI. Local `npm ci` was network-blocked, and Playwright direct navigation to localhost returned `ERR_BLOCKED_BY_ADMINISTRATOR`. These checks are BLOCKED, not PASS.

Before any merge or live label:
1. Import reviewed v34.1 dependency code and v35 adapters without public raw data.
2. Build the exact v35 React source with pinned `npm ci`, create a new immutable asset manifest, and pass direct browser HTTP E2E.
3. Obtain actual fresh, source-admitted Coinbase collector evidence with a persistent calibrated host and test reconnect, gap, slow-client faults.
4. Prove full frozen regression and source preservation for the final exact candidate.
5. Keep source admission, paper OMS risk authorization, and real broker execution physically separate.

All values are research engineering evidence only. `NO CONFIRMED EXECUTABLE ALPHA`. Real orders: 0. Keep `main` unchanged.
