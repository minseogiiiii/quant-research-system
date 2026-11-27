"""Offline pin check. No vendor import by the production engine."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
manifest = json.loads(
    (ROOT / "btc_eth_terminal/vendor/fincept/UPSTREAM.json").read_text()
)
for entry in manifest["files"]:
    data = (ROOT / entry["local"]).read_bytes()
    git_blob = hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data
    ).hexdigest()
    if git_blob != entry["git_blob"]:
        raise SystemExit("UPSTREAM_PIN_MISMATCH: " + entry["local"])
    print(hashlib.sha256(data).hexdigest(), entry["local"])
print("PASS_FINCEPT_VENDOR_EXACT_BLOBS")
