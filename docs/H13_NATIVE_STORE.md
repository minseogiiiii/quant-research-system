# H13-A/B private native store
Local POSIX owner-only directory, one writer with advisory flock. Public Coinbase collector, one bounded 60–3600s clean epoch per invocation, explicit archive budget and free-space admission. No unattended loop, API key, paid purchase, trading or raw upload. Failed attempts stay in incoming/ (including logs); only H1 audited single-epoch raw + Parquet + admission receipt atomically published under sessions/<manifest sha>. Imports require external manifest pin and original audit, then copied archive independently re-audited. H4 catalog verifies derived receipts, hashes/counts and interval disjointness; inventory rechecks raw manifest but does not rehash all raw chunks. All sessions development only. Fsync tree and directory publication, local storage trust boundary; not an adversarial security guarantee or certified power-loss recovery. Disk estimate is not a hard derived-data quota. No automatic deletion, operator must monitor available disk and retain backups. A cloud CI pilot uses ephemeral storage; it cannot establish persistent host capacity.

From repository root on local Linux/macOS with Node 22+ and Python dependencies:
```sh
export PYTHONPATH=btc_eth_terminal/backend:btc_eth_terminal/scripts
python btc_eth_terminal/scripts/h13_native_store.py --root "$HOME/quant-native-store" capture --duration 1800 --archive-budget-bytes 1000000000
python btc_eth_terminal/scripts/h13_native_store.py --root "$HOME/quant-native-store" inventory
```
Run additional bounded sessions explicitly; no claim that intervals between sessions are observed or gap-free. Keep store outside repository and never upload raw data without authorization.
Use `inventory --reaudit-raw` to hash and semantically re-audit every sealed original chunk and compare counts/bytes with admission and dataset receipts. This is intentionally more expensive than normal inventory. Imports read pinned manifest bytes, enforce exact chunk names and sizes, stream bounded regular files without following terminal symlinks, and recheck SHA-256 during copying. Parent storage and source owner remain trusted.
