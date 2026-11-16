# v33 architecture and data-admission boundaries

## What is actually shown

The left chart consumes the unchanged read-only `/api/v30/series` candle endpoint and the actual 2026-11-14 one-hour Coinbase public spot source archive. The client uses the original Funnest React charting adapter, compiled on GitHub Actions from source pinned to commit `6e88f9c9811ff42406a3259f7ac82fc1e8719f03`.

Every price bar is a completed five-minute receiver-time aggregation of **observed market_trades update events**, not an official exchange candle, total venue volume or executable price feed. Partial initial/final buckets are excluded. The chart only requests bars available at or before the selected receiver-asof decision timestamp.

The recorded L1 bid, ask and displayed sizes, midpoint, spread, best imbalance, rolling five-minute best-level OFI, and Microprice come from the existing v23 causal diagnostic at twelve frozen five-minute receiver-time decisions. The selected diagnostic must have causal L1 status and age <= 5,000 ms, otherwise the L1 quote and its derived fields disappear. The view does not reconstruct or render more than one depth level.

The right-hand tape shows at most 21 rows, selected from an existing bounded per-5-minute source trade sample. Feed `side` is displayed **verbatim** without pretending it is independently established aggressor direction. The tiny buy/sell ratio is of the *currently displayed bounded sample*, not market-wide executed flow.

Two SVG charts plot numeric values from the verified frozen feature grid only. They are engineering diagnostics, not alpha predictions or PnL. Microprice deviation is `(microprice - L1 midpoint) / L1 midpoint * 10,000`, in bps. OFI uses the existing five-minute base-unit feature, not an alternative reconstructed signal.

## What is deliberately NOT shown as data

- Ten-level L2 ladder, cumulative full depth, and heatmap: full event-time L2 reconstruction at the selected observations has **not** been projected into the existing validated API.
- Event-time liquidity recovery curve, recovery half-life or shock alpha: the existing five-minute L1 observation grid does **not** support this measurement.
- Independent exchange clock attestation, complete historical feature/label PIT, untouched blind OOS, executable alpha, live market connection, exchange account, broker routing or real fills.

## Data integrity and clock model

All clients receive the same original source-manifest and projection SHA-256 returned by the frozen API, checked before data acceptance. Cross-product rows, incorrect causal admission flags, bar `available_at` before full five-minute close, and non-monotonic feature timestamps fail closed. Product switches clear prior data and ignore asynchronous outdated responses. The historical replay controls drive the *single* selected causal observation timestamp; there is no fake forward update loop.

Python bridge binds localhost only. v33 compiled HTML/JS/CSS is pinned by `backend/v33_frontend_integrity.py` and `frontend/v33_dist_integrity.json`; tampering one file fails the entire served bundle. Original v32 dist and v32 pin remain available for exact historic regression checks but are not served by the current `/research` endpoint.

## GitHub staging caveat

Frontend implementation is on isolated branch `integration/btc-eth-unified-terminal`, not on `main`. Source artifacts and raw Coinbase archives in the full local release are not all imported into the GitHub branch. A successful GitHub frontend build alone is not a complete server deployment or a verified 24-hour collector.
