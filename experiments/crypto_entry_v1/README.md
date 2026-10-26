# Crypto Entry Research: G1–G6 / LONG-SHORT-WAIT

**Status:** Implemented prototype, not profitable-trading evidence.

Research principle: rank groups by their **incremental out-of-sample predictive information and net economic value**, never by number of bullish/bearish votes.

## 1. Dataset, G1–G4

Binance *Global* monthly public **SPOT 1m Klines** contain prices, total quote volume, and taker-buy quote volume. G4 uses actual aggressor-side *aggregate* flow from the klines; it does not reconstruct individual trades or OFI. The archive sometimes uses **milliseconds (2024)** versus **microseconds (2025+)**. The importer detects both, checks official `.CHECKSUM` digests, rejects gaps and duplicate rows, and logs source URLs and SHA-256.

```bash
python -m pip install -r requirements.txt
python history.py --symbol BTCUSDT --from-month 2024-01 --to-month 2026-09 --out data/binance/BTCUSDT
python ablation.py --archive-dir data/binance/BTCUSDT --output results/ablation-btc-001.json
```

This is ~33 monthly 1m archives and may take time / disk space. Run on an internet-enabled workstation. Excluded G5/G6 require live L2. After the raw dataset is collected, keep the **2026-01 through 2026-09 holdout untouched** until the protocol is frozen; the demonstration command consumes it once, so do not use this until you're ready for that final evaluation. For development, pass a different `--test-end` and `--train-end`/`--val-end` date range, or separate exploratory code.

### G1–G4 features

- G1 price trend: returns 5/15/60m, 12/60 EMA gap, 60m range position, last candle body fraction.
- G2 volume/profile: relative quote volume, 60m VWAP gap, 5m volume change, **approximate 120m volume-profile POC** (24 price bins of closing-price-weighted quote volume; not trade-level true POC).
- G3 volatility: 30/120m realized volatility, 30m ATR fraction, volatility ratio, standardized 5m price shock.
- G4 aggressor flow: net taker quote volume / total quote volume 5/30m, signed-volume flow 5/30m, directional persistence.

### Labels and ablation

Signals from fully closed minute `t`, decision at its end; reference entry price `open[t+2]` after a full 1-minute delay; evaluation price `open[t+32]` for **30m horizon**. 5m decision cadence. 35m embargo around train/validation/test boundaries. `StandardScaler + Ridge(alpha=100)` fitted **training only**. First use validation to choose among pre-specified thresholds; test is one-time. Compare `BASE_MEAN`, `ONLY_Gi`, `FULL_G1_G4`, and each `DROP_Gi`.

Metrics: Paired UTC-day block bootstrap 95% CI for group-ablation MSE/PnL differences, OOS return regression MSE and stylized LONG/SHORT/WAIT net proxy in bps per decision / per trade, with pre-specified symmetric round-trip 20bps proxy (change with `--roundtrip-bps`). **Spot-derived SHORT is only a research label**; actual short execution, funding, margin, liquidation, basis, and venue price require separate instruments. The prototype has no claim of executable alpha. Paired day-block bootstrap confidence intervals are included for group ablation, but they do not fix selection bias or execution-proxy limitations. It does not account for bid/ask historical spread or order impact beyond the rough fee proxy.

## 2. Live G5–G6 capture

```bash
python live_book.py --venue global --symbol BTCUSDT --duration 60 --out data/live
# or data from Binance.US (venue-specific liquidity):
python live_book.py --venue us --symbol BTCUSDT --duration 60 --out data/live
# Continuous collection on a durable externally managed machine:
python live_book.py --venue global --symbol BTCUSDT --duration 0 --out data/live
```

No API key or order-writing permissions. Collector starts the WebSocket BEFORE REST snapshot, buffers updates, checks U/u, reconnects/resyncs on gaps or invalid books, and never records derived features while unsynced. Persists:
- `*_raw.jsonl.gz`: depth changes, public trades, sync snapshots, errors, local receive timestamps;
- `*_features.jsonl.gz`: 1Hz top-of-book OBI, ±10bps quoted depth, microprice distance, spread, 30s top-of-book OFI, signed trade imbalance;
- `*_stats.json`: session and resync counts.

**Important:** 100ms diff depth means no L3 queue position, individual cancellation, exact order-level refill, or guaranteed trade/depth cross-stream timestamp ordering. OFI is a level-1 event-sampling approximation (not individual order arrivals). Preserve raw data and source timestamps, and use LOCAL receive time for information availability. Export data/ and large datasets outside the GitHub source tree; they should NOT be committed.

The one-minute data produces a **historical** trial. L2 requires forward data accumulation and a fresh chronological OOS test. Do not insert hindsight-reconstructed order books into older backtests.

## 3. Tests

```bash
pytest -q tests
```

Tests validate timestamp handling, early-future leakage, book sync/sequence gaps, order-book feature calculations, train-only scaling and G1–G4 ablation mechanics. Synthetic smoke tests show that software runs, NOT that alpha exists.