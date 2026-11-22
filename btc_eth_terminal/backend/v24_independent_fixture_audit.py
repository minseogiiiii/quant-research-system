"""Read-only independently coded auditor for a v24 SYNTHETIC paper ledger.

This is independent *software accounting*, NOT independent venue evidence,
exchange order verification, trade matching or a paper trading performance test.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

FIXTURE = "SYNTHETIC_FIXTURE_ONLY"
ZERO_HASH = "0" * 64
ASSETS = ("BTC-USD", "ETH-USD")


class IndependentAuditFailure(ValueError):
    pass


def audit_fixture(db_path: Path) -> dict:
    p = Path(db_path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > 16 * 1024 * 1024:
        raise IndependentAuditFailure(
            "Missing, unsafe, or excessively large local ledger"
        )
    with closing(sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT seq,token,kind,order_id,payload,previous_hash,event_hash "
            "FROM events ORDER BY seq"
        ).fetchall()
    if not rows or rows[0][2] != "INIT":
        raise IndependentAuditFailure("Missing genesis")
    previous = ZERO_HASH
    seen_tokens = set()
    orders = {}
    cash = None
    positions = {s: Decimal("0") for s in ASSETS}
    fee = None
    marks = None
    mark_time = None
    peak = None
    drawdown = Decimal("0")
    mode = "OBSERVE_ONLY"
    transitions = []
    for n, row in enumerate(rows, 1):
        seq, token, kind, oid, body, prev, digest = row
        if not isinstance(body, str) or len(body) > 65536:
            raise IndependentAuditFailure("Unbounded or malformed source event")
        verify = json.dumps(
            {
                "seq": seq,
                "token": token,
                "kind": kind,
                "order_id": oid,
                "payload": body,
                "previous_hash": prev,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if (
            seq != n
            or token in seen_tokens
            or prev != previous
            or sha256(verify.encode()).hexdigest() != digest
        ):
            raise IndependentAuditFailure(
                f"Event digest, sequence or dedup violation at {n}"
            )
        seen_tokens.add(token)
        previous = digest
        data = json.loads(body)
        if n == 1:
            if oid != "SYSTEM" or set(data) != {"policy"}:
                raise IndependentAuditFailure("Unrecognized genesis")
            policy = data["policy"]
            cash = Decimal(str(policy["starting_cash_usd"]))
            fee = Decimal(str(policy["fee_bps"])) / Decimal("10000")
            peak = cash
            if not cash.is_finite() or not fee.is_finite() or cash <= 0 or fee < 0:
                raise IndependentAuditFailure("Invalid synthetic policy")
            continue
        if kind == "MARK":
            if set(data["quotes"]) != set(ASSETS):
                raise IndependentAuditFailure("Missing fixture market quotes")
            marks = {}
            times = []
            for asset in ASSETS:
                q = data["quotes"][asset]
                if (
                    q["origin"] != FIXTURE
                    or q["integrity"] != "HEALTHY"
                    or q["instrument"] != asset
                ):
                    raise IndependentAuditFailure(
                        "External or quarantined source present"
                    )
                bid, ask = Decimal(q["bid"]), Decimal(q["ask"])
                if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= bid:
                    raise IndependentAuditFailure("Invalid mark quote")
                marks[asset] = bid
                times.append(q["decision_ms"])
            if times[0] != times[1] or (mark_time is not None and times[0] < mark_time):
                raise IndependentAuditFailure("Mark time mismatch")
            mark_time = times[0]
            equity = cash + sum((positions[a] * marks[a] for a in ASSETS), Decimal("0"))
            peak = max(equity, peak)
            drawdown = peak - equity
            if drawdown > Decimal(policy["max_drawdown_usd"]):
                mode = "HALT"
        elif kind in ("ARM", "RECOVER", "DEGRADE", "HALT"):
            if oid != "SYSTEM" or data.get("fixture_origin") != FIXTURE:
                raise IndependentAuditFailure("External risk mode actor")
            if kind == "ARM":
                if not data.get("manual") or mode != "OBSERVE_ONLY":
                    raise IndependentAuditFailure("Non-manual paper rearm")
                mode = "NORMAL"
            elif kind == "RECOVER":
                if not data.get("manual") or mode != "HALT":
                    raise IndependentAuditFailure("Unauthorized HALT recovery")
                mode = "OBSERVE_ONLY"
            elif kind == "DEGRADE":
                mode = "OBSERVE_ONLY"
            else:
                mode = "HALT"
        elif kind == "INTENT":
            if oid in orders or not oid.startswith("PAPER:"):
                raise IndependentAuditFailure("Repeated or external ID")
            if mode != "NORMAL":
                raise IndependentAuditFailure("Intent outside NORMAL risk mode")
            if data["quote"]["origin"] != FIXTURE:
                raise IndependentAuditFailure("Non-fixture quote in intent")
            instrument, side = data["instrument"], data["side"]
            if instrument not in ASSETS or side not in ("BUY", "SELL"):
                raise IndependentAuditFailure("Unknown instrument or side")
            qty, limit = Decimal(data["quantity"]), Decimal(data["limit_price"])
            if qty <= 0 or limit <= 0 or not qty.is_finite() or not limit.is_finite():
                raise IndependentAuditFailure("Invalid intent decimals")
            orders[oid] = {
                "instrument": instrument,
                "side": side,
                "quantity": qty,
                "limit": limit,
                "filled": Decimal("0"),
                "ack": False,
                "cancel_requested": False,
                "status": "ACCEPTED",
            }
        elif kind == "DENY":
            if oid in orders or not oid.startswith("PAPER:"):
                raise IndependentAuditFailure("Invalid denied intent ID")
            orders[oid] = {"status": "DENIED"}
        elif kind in ("ACK", "FILL", "REJECT", "CANCEL_REQUEST", "CANCEL_ACK"):
            if (
                oid not in orders
                or orders[oid]["status"] == "DENIED"
                or data.get("fixture_origin") != FIXTURE
            ):
                raise IndependentAuditFailure("Invalid lifecycle source")
            o = orders[oid]
            if kind == "ACK":
                if o["ack"] or o["status"] not in ("ACCEPTED", "CANCEL_PENDING"):
                    raise IndependentAuditFailure("Invalid acknowledgement")
                o["ack"] = True
                if o["status"] == "ACCEPTED":
                    o["status"] = "ACKNOWLEDGED"
            elif kind == "FILL":
                qty, price = Decimal(data["quantity"]), Decimal(data["price"])
                if (
                    not o["ack"]
                    or o["status"] not in ("ACKNOWLEDGED", "PARTIAL", "CANCEL_PENDING")
                    or qty <= 0
                    or qty > o["quantity"] - o["filled"]
                    or price <= 0
                ):
                    raise IndependentAuditFailure("Invalid synthetic fill")
                if (o["side"] == "BUY" and price > o["limit"]) or (
                    o["side"] == "SELL" and price < o["limit"]
                ):
                    raise IndependentAuditFailure("Limit price violation")
                notional = qty * price
                if o["side"] == "BUY":
                    cash -= notional * (Decimal("1") + fee)
                    positions[o["instrument"]] += qty
                else:
                    cash += notional * (Decimal("1") - fee)
                    positions[o["instrument"]] -= qty
                if cash < 0 or min(positions.values()) < 0:
                    raise IndependentAuditFailure("Negative settled spot position")
                if marks is None:
                    raise IndependentAuditFailure(
                        "Synthetic settlement before market marks"
                    )
                equity = cash + sum(
                    (positions[a] * marks[a] for a in ASSETS), Decimal("0")
                )
                peak = max(equity, peak)
                drawdown = peak - equity
                if drawdown > Decimal(policy["max_drawdown_usd"]):
                    mode = "HALT"
                if data["at_ms"] - mark_time > int(policy["max_staleness_ms"]):
                    mode = "HALT"
                o["filled"] += qty
                if o["filled"] == o["quantity"]:
                    o["status"] = "FILLED"
                elif not o["cancel_requested"]:
                    o["status"] = "PARTIAL"
            elif kind == "CANCEL_REQUEST":
                if o["status"] not in ("ACCEPTED", "ACKNOWLEDGED", "PARTIAL"):
                    raise IndependentAuditFailure("Invalid cancel request")
                o["cancel_requested"] = True
                o["status"] = "CANCEL_PENDING"
            elif kind == "CANCEL_ACK":
                if not o["cancel_requested"] or o["status"] not in (
                    "CANCEL_PENDING",
                    "FILLED",
                ):
                    raise IndependentAuditFailure("Invalid cancel acknowledgement")
                if o["status"] == "CANCEL_PENDING":
                    o["status"] = "CANCELLED"
            else:
                if o["filled"] > 0 or o["status"] not in (
                    "ACCEPTED",
                    "ACKNOWLEDGED",
                    "CANCEL_PENDING",
                ):
                    raise IndependentAuditFailure("Late rejection invalid")
                o["status"] = "REJECTED"
        else:
            raise IndependentAuditFailure("Unexpected event type")
        transitions.append(kind)
    result = {
        "schema": "V24_SYNTHETIC_INDEPENDENT_LEDGER_AUDIT_V1",
        "status": "INTERNAL_FIXTURE_ACCOUNTING_REPLAY_AGREED",
        "same_original_event_chain_sha256": previous,
        "event_count": len(rows),
        "mode": mode,
        "cash_usd": str(cash),
        "positions": {a: str(positions[a]) for a in ASSETS},
        "drawdown_usd": str(drawdown),
        "orders": {k: {"status": v["status"]} for k, v in sorted(orders.items())},
        "event_type_counts": {
            k: transitions.count(k) for k in sorted(set(transitions))
        },
        "actual_exchange_orders": 0,
        "actual_exchange_fills": 0,
        "independent_exchange_truth_verified": False,
        "preregistered_oos_alpha_verified": False,
    }
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ledger", type=Path)
    ap.add_argument("--json-output", type=Path)
    args = ap.parse_args()
    data = audit_fixture(args.ledger)
    body = json.dumps(data, sort_keys=True, indent=2) + "\n"
    if args.json_output:
        args.json_output.write_text(body)
    print(body, end="")


if __name__ == "__main__":
    main()
