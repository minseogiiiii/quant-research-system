"""Independently compare Q7 1m L1 OFI with v23 separate sorted-book arithmetic.

Same archived feed; distinct parser, book and OFI arithmetic. Not independent
market evidence, OOS confirmation, or a quote/fill certification.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from v23_independent_microstructure_audit import SortedL2, independent_delta
from v37_factor_lab import BASE_ROOT, PRODS, load_report


class IndependentQ7Reject(ValueError):
    pass


def independently_compare(root=BASE_ROOT):
    original, pin = load_report()
    root = Path(root)
    manifest_bytes = (root / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != original["source_manifest_sha256"]:
        raise IndependentQ7Reject("Source manifest changed")
    m = json.loads(manifest_bytes)
    wanted = {
        p: {
            x["decision_receiver_ms"]: x
            for x in original["signals_with_future_labels_isolated"][p]
        }
        for p in PRODS
    }
    expected_times = sorted(set(t for p in PRODS for t in wanted[p]))
    books = {p: SortedL2() for p in PRODS}
    ofi = {p: Decimal(0) for p in PRODS}
    history = {p: deque() for p in PRODS}
    last_l2 = {p: None for p in PRODS}
    seq_last = {p: None for p in PRODS}
    i = 0
    matched = 0
    prev_seq = None

    def check(t):
        nonlocal matched
        for p in PRODS:
            q = history[p]
            while q and q[0][0] <= t - 300000:
                _, v = q.popleft()
                ofi[p] -= v
            target = wanted[p].get(t)
            if target is None:
                continue
            top = books[p].top()
            if top is None or last_l2[p] is None or not 0 <= t - last_l2[p] <= 5000:
                raise IndependentQ7Reject("Wrong admitted receiver-time quote")
            bp, bq, ap, aq = top
            f = target["features_asof"]
            expected = {
                "ofi_l1_rolling_5m_base": ofi[p],
                "obi_l1": (bq - aq) / (bq + aq),
                "microprice_displacement_bps": (
                    (bp * aq + ap * bq) / (bq + aq) - (bp + ap) / 2
                )
                / ((bp + ap) / 2)
                * 10000,
                "spread_bps": (ap - bp) / ((bp + ap) / 2) * 10000,
            }
            for key, v in expected.items():
                if Decimal(str(f[key])) != v:
                    raise IndependentQ7Reject("Independent Q7 " + key + " mismatch")
            if target["source_message_sequence_local_connection"] != seq_last[p]:
                raise IndependentQ7Reject("Source sequence identity changed")
            if target["feature_max_received_ms"] != last_l2[p]:
                raise IndependentQ7Reject(
                    "Future received event appears in asof feature"
                )
            matched += 1

    for c in m["chunks"]:
        path = root / c["file"]
        if path.is_symlink():
            raise IndependentQ7Reject("Unsafe source chunk")
        b = path.read_bytes()
        if sha256(b).hexdigest() != c["archive_sha256"]:
            raise IndependentQ7Reject("Source chunk mismatch")
        for line in b.splitlines():
            rec = json.loads(line)
            msg = json.loads(rec["raw_text"])
            sequence = msg["sequence_num"]
            if prev_seq is not None and sequence != prev_seq + 1:
                raise IndependentQ7Reject("Source gap")
            prev_seq = sequence
            if msg["channel"] != "l2_data":
                continue
            recv = int(
                datetime.fromisoformat(
                    rec["received_utc"].replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
            while i < len(expected_times) and expected_times[i] < recv:
                check(expected_times[i])
                i += 1
            for event in msg["events"]:
                p = event["product_id"]
                prior = books[p].top()
                books[p].set_levels(event["type"], event["updates"])
                now = books[p].top()
                if event["type"] == "update":
                    x = independent_delta(prior, now)
                    history[p].append((recv, x))
                    ofi[p] += x
                last_l2[p] = recv
                seq_last[p] = sequence
    while i < len(expected_times):
        check(expected_times[i])
        i += 1
    if matched != sum(map(len, wanted.values())):
        raise IndependentQ7Reject("Missing independent decisions")
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise IndependentQ7Reject("Source changed during independent study")
    return {
        "status": "VERIFIED_INDEPENDENT_SOFTWARE_SAME_ARCHIVE_NOT_OOS",
        "factor_report_sha256": pin,
        "source_manifest_sha256": original["source_manifest_sha256"],
        "receiver_decision_rows_checked": matched,
        "products": list(PRODS),
        "valid_exchange_clock_attestation": False,
        "confirmed_tradable_alpha": False,
    }


if __name__ == "__main__":
    print(json.dumps(independently_compare(), indent=2))
