# Independent second capture (no trading)

The initial 2026-11-13 20:37 UTC GitHub Action captured 2026-11-08 BTC-USD and ETH-USD public Coinbase 5-minute candles, 288 per product, retaining original JSON and hash metadata. Local v7 primary reconstruction and independent parser both passed on the first capture.

This isolated branch commit intentionally triggers a second **separate GitHub Actions execution** with the same source and UTC window. Compare exact normalized CSV SHA-256 for each product across the two raw-archived acquisitions. No alpha claim, no fee/funding/LOB assumption, no merge to main, no order execution. A mismatch must block registration pending manual source review.
