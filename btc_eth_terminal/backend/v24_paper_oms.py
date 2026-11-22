"""v24 isolated, event-sourced PAPER fixture order-management state machine.

SECURITY BOUNDARY: synthetic event injection only.  NO network imports,
exchange adapters, order-routing methods, account secrets, or real fills.
A successful test is NOT validation of execution economics or P3 readiness.

SQLite transactions make each append atomic, and full hash-chain replay rejects
ordinary storage tampering. The chain has NO externally anchored signature and
cannot protect against an administrator able to rewrite the whole database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from v24_paper_risk import (
    ASSETS,
    FIXTURE,
    ZERO,
    FixtureQuote,
    PaperPolicy,
    PaperReject,
    dec,
    integer,
    validate_intent,
)

ZERO_HASH = "0" * 64
MAX_FIXTURE_EVENTS = (
    5000  # Explicit engineering capacity limit; not production throughput.
)
MAX_LEDGER_FILE_BYTES = 16 * 1024 * 1024
ALLOWED_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
ALLOWED_KINDS = frozenset(
    {
        "INIT",
        "MARK",
        "ARM",
        "DEGRADE",
        "HALT",
        "RECOVER",
        "INTENT",
        "DENY",
        "ACK",
        "FILL",
        "CANCEL_REQUEST",
        "CANCEL_ACK",
        "REJECT",
    }
)


def canonical(obj: object) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def validate_id(s: str, label: str) -> str:
    if not isinstance(s, str) or not ALLOWED_ID.fullmatch(s):
        raise PaperReject(f"{label}: invalid identifier")
    return s


def as_quote(obj: dict) -> FixtureQuote:
    if not isinstance(obj, dict) or set(obj) != {
        "instrument",
        "bid",
        "ask",
        "received_ms",
        "decision_ms",
        "integrity",
        "origin",
    }:
        raise PaperReject("Invalid fixture quote schema")
    return FixtureQuote(**obj)


def quote_payload(q: FixtureQuote) -> dict:
    return {k: getattr(q, k) for k in q.__dataclass_fields__}


def _new_state(policy: PaperPolicy) -> dict:
    return {
        "policy": policy,
        "mode": "OBSERVE_ONLY",
        "cash_usd": dec(policy.starting_cash_usd, "initial_cash"),
        "positions": {s: ZERO for s in ASSETS},
        "orders": {},
        "marks": None,
        "mark_at_ms": None,
        "peak_equity": dec(policy.starting_cash_usd, "initial_cash"),
        "last_equity": None,
        "drawdown_usd": ZERO,
        "last_mode_at_ms": None,
    }


def _account_view(st: dict, policy: PaperPolicy) -> dict:
    cash_reserved = ZERO
    buy_qty = {s: ZERO for s in ASSETS}
    buy_notional = {s: ZERO for s in ASSETS}
    sell_qty = {s: ZERO for s in ASSETS}
    open_count = 0
    for o in st["orders"].values():
        if o["status"] in ("DENIED", "REJECTED", "CANCELLED", "FILLED"):
            continue
        remain = o["quantity"] - o["filled"]
        if remain <= 0:
            raise PaperReject("Invalid open remaining quantity")
        open_count += 1
        if o["side"] == "BUY":
            cash_reserved += (
                remain * o["limit_price"] * (Decimal("1") + policy.fee_factor)
            )
            buy_qty[o["instrument"]] += remain
            buy_notional[o["instrument"]] += remain * max(
                o["limit_price"],
                (st["marks"] or {s: ZERO for s in ASSETS})[o["instrument"]],
            )
        else:
            sell_qty[o["instrument"]] += remain
    return {
        "mode": st["mode"],
        "cash_usd": st["cash_usd"],
        "positions": st["positions"],
        "reservations": {
            "cash_usd": cash_reserved,
            "buy_qty": buy_qty,
            "buy_notional_usd": buy_notional,
            "sell_qty": sell_qty,
        },
        "open_orders": open_count,
        "drawdown_usd": st["drawdown_usd"],
        "marks": st["marks"] or {s: ZERO for s in ASSETS},
    }


def _remeasure_equity(st: dict, policy: PaperPolicy) -> None:
    """Revalue immediate synthetic holdings after every paper fill/mark."""
    if st["marks"] is None:
        raise PaperReject("No complete dual-market synthetic marks")
    equity = st["cash_usd"] + sum(
        (st["positions"][s] * st["marks"][s] for s in ASSETS), ZERO
    )
    st["last_equity"] = equity
    st["peak_equity"] = max(st["peak_equity"], equity)
    st["drawdown_usd"] = st["peak_equity"] - equity
    if st["drawdown_usd"] > dec(policy.max_drawdown_usd, "max_drawdown_usd"):
        st["mode"] = "HALT"


def _apply(st: dict, kind: str, order_id: str, p: dict, policy: PaperPolicy) -> None:
    """Validate every transition during replay, not just when first appended."""
    if not isinstance(p, dict) or kind not in ALLOWED_KINDS:
        raise PaperReject("Unknown event schema")
    if kind == "INIT":
        raise PaperReject("INIT is only valid at event 1")
    if kind == "MARK":
        if set(p) != {"quotes"} or set(p["quotes"]) != set(ASSETS):
            raise PaperReject("Both BTC and ETH marks are required")
        quotes = [as_quote(p["quotes"][s]) for s in ASSETS]
        bids = {}
        for q in quotes:
            bid, _ = q.validate(policy)
            bids[q.instrument] = bid
        t = quotes[0].decision_ms
        if quotes[1].decision_ms != t or (
            st["mark_at_ms"] is not None and t < st["mark_at_ms"]
        ):
            raise PaperReject("Mark clock mismatch or regression")
        st["mark_at_ms"] = t
        st["marks"] = bids
        _remeasure_equity(st, policy)
        return
    if kind in ("ARM", "HALT", "RECOVER", "DEGRADE"):
        if order_id != "SYSTEM" or set(p) != {
            "reason",
            "at_ms",
            "manual",
            "fixture_origin",
        }:
            raise PaperReject("Invalid risk mode event")
        integer(p["at_ms"], "risk_mode_at_ms")
        if st["last_mode_at_ms"] is not None and p["at_ms"] < st["last_mode_at_ms"]:
            raise PaperReject("Risk transition clock cannot go backwards")
        if (
            not isinstance(p["reason"], str)
            or not 5 <= len(p["reason"]) <= 200
            or type(p["manual"]) is not bool
            or p["fixture_origin"] != FIXTURE
        ):
            raise PaperReject("Missing human-readable risk authorization")
        if kind == "ARM":
            if st["mode"] != "OBSERVE_ONLY" or not p["manual"] or st["marks"] is None:
                raise PaperReject("Manual arm requires observable synthetic source")
            if not 0 <= p["at_ms"] - st["mark_at_ms"] <= policy.max_staleness_ms:
                raise PaperReject("Cannot arm on stale or future mark")
            if st["drawdown_usd"] > dec(policy.max_drawdown_usd, "max_drawdown_usd"):
                raise PaperReject("Cannot arm after loss breach")
            st["mode"] = "NORMAL"
        elif kind == "RECOVER":
            if st["mode"] != "HALT" or not p["manual"]:
                raise PaperReject("HALT recovery requires explicit manual action")
            st["mode"] = "OBSERVE_ONLY"  # never jump directly to NORMAL
        elif kind == "DEGRADE":
            if st["mode"] != "NORMAL":
                raise PaperReject("Only NORMAL can degrade to OBSERVE_ONLY")
            st["mode"] = "OBSERVE_ONLY"
        else:
            st["mode"] = "HALT"
        st["last_mode_at_ms"] = p["at_ms"]
        return
    if kind in ("INTENT", "DENY"):
        required = {"instrument", "side", "quantity", "limit_price", "quote", "at_ms"}
        if kind == "DENY":
            required = required | {"reason"}
        if (
            set(p) != required
            or order_id in st["orders"]
            or not order_id.startswith("PAPER:")
        ):
            raise PaperReject("Invalid or repeated synthetic intent")
        integer(p["at_ms"], "intent_time")
        q = as_quote(p["quote"])
        if q.decision_ms != p["at_ms"]:
            raise PaperReject("Decision time and fixture clock differ")
        if kind == "DENY":
            if not isinstance(p["reason"], str) or not p["reason"]:
                raise PaperReject("Denied intent must have explicit reason")
            st["orders"][order_id] = {"status": "DENIED", "reason": p["reason"]}
            return
        if (
            st["mark_at_ms"] is None
            or not 0 <= p["at_ms"] - st["mark_at_ms"] <= policy.max_staleness_ms
        ):
            raise PaperReject("Missing recent dual-market account marks")
        qty, px = validate_intent(
            instrument=p["instrument"],
            side=p["side"],
            quantity=p["quantity"],
            limit_price=p["limit_price"],
            quote=q,
            account=_account_view(st, policy),
            policy=policy,
        )
        st["orders"][order_id] = {
            "status": "ACCEPTED",
            "side": p["side"],
            "instrument": p["instrument"],
            "quantity": qty,
            "limit_price": px,
            "filled": ZERO,
            "acknowledged": False,
            "cancel_requested": False,
            "last_at_ms": p["at_ms"],
        }
        return
    if kind not in ("ACK", "FILL", "CANCEL_REQUEST", "CANCEL_ACK", "REJECT"):
        raise PaperReject("Unknown transition")
    if order_id not in st["orders"]:
        raise PaperReject("Unknown order")
    o = st["orders"][order_id]
    if o["status"] == "DENIED":
        raise PaperReject("Denied order has no execution lifecycle")
    if "at_ms" not in p or type(p["at_ms"]) is not int or p["at_ms"] < o["last_at_ms"]:
        raise PaperReject("Out-of-order lifecycle clock")
    if p.get("fixture_origin") != FIXTURE:
        raise PaperReject(
            "Only manually injected synthetic lifecycle events are supported"
        )
    if kind == "ACK":
        if (
            set(p) != {"at_ms", "fixture_origin"}
            or o["acknowledged"]
            or o["status"] not in ("ACCEPTED", "CANCEL_PENDING")
        ):
            raise PaperReject("ACK state invalid")
        o["acknowledged"] = True
        if o["status"] == "ACCEPTED":
            o["status"] = "ACKNOWLEDGED"
    elif kind == "CANCEL_REQUEST":
        if set(p) != {"at_ms", "fixture_origin"} or o["status"] not in (
            "ACCEPTED",
            "ACKNOWLEDGED",
            "PARTIAL",
        ):
            raise PaperReject("CANCEL_REQUEST state invalid")
        o["status"] = "CANCEL_PENDING"
        o["cancel_requested"] = True
    elif kind == "CANCEL_ACK":
        if (
            set(p) != {"at_ms", "fixture_origin"}
            or not o["cancel_requested"]
            or o["status"] not in ("CANCEL_PENDING", "FILLED")
        ):
            raise PaperReject("Cancel acceptance not supported by order state")
        if o["status"] == "CANCEL_PENDING":
            o["status"] = "CANCELLED"
    elif kind == "REJECT":
        if (
            set(p) != {"at_ms", "fixture_origin", "reason"}
            or not isinstance(p["reason"], str)
            or not p["reason"]
        ):
            raise PaperReject("Malformed synthetic rejection")
        if o["filled"] != ZERO or o["status"] not in (
            "ACCEPTED",
            "ACKNOWLEDGED",
            "CANCEL_PENDING",
        ):
            raise PaperReject("Cannot retroactively reject filled order")
        o["status"] = "REJECTED"
    elif kind == "FILL":
        if set(p) != {"at_ms", "fixture_origin", "quantity", "price"}:
            raise PaperReject("Invalid fixture fill event")
        if not o["acknowledged"] or o["status"] not in (
            "ACKNOWLEDGED",
            "PARTIAL",
            "CANCEL_PENDING",
        ):
            raise PaperReject("Fill before synthetic ACK or after terminal state")
        qty = dec(p["quantity"], "fill_quantity", positive=True)
        price = dec(p["price"], "fill_price", positive=True)
        if qty > o["quantity"] - o["filled"]:
            raise PaperReject("Fill quantity exceeds remaining")
        if (o["side"] == "BUY" and price > o["limit_price"]) or (
            o["side"] == "SELL" and price < o["limit_price"]
        ):
            raise PaperReject("Synthetic limit price violated")
        notional = qty * price
        fee = notional * policy.fee_factor
        asset = o["instrument"]
        if o["side"] == "BUY":
            if st["cash_usd"] + Decimal("0.00000000001") < notional + fee:
                raise PaperReject("Negative synthetic cash prohibited")
            st["cash_usd"] -= notional + fee
            st["positions"][asset] += qty
        else:
            if st["positions"][asset] < qty:
                raise PaperReject("Negative synthetic inventory prohibited")
            st["positions"][asset] -= qty
            st["cash_usd"] += notional - fee
        o["filled"] += qty
        if o["filled"] == o["quantity"]:
            o["status"] = "FILLED"
        elif not o["cancel_requested"]:
            o["status"] = "PARTIAL"
        _remeasure_equity(st, policy)
        if (
            st["mark_at_ms"] is None
            or p["at_ms"] - st["mark_at_ms"] > policy.max_staleness_ms
        ):
            st["mode"] = "HALT"
    o["last_at_ms"] = p["at_ms"]


def _state_json(st: dict, head_hash: str, event_count: int) -> dict:
    policy = st["policy"]
    view = _account_view(st, policy)
    return {
        "schema": "V24_SYNTHETIC_PAPER_STATE_V1",
        "source_type": FIXTURE,
        "live_orders_possible": False,
        "actual_fills_verified": False,
        "mode": st["mode"],
        "cash_usd": str(st["cash_usd"]),
        "positions": {s: str(st["positions"][s]) for s in ASSETS},
        "reserved_cash_usd": str(view["reservations"]["cash_usd"]),
        "reserved_sell_quantity": {
            s: str(view["reservations"]["sell_qty"][s]) for s in ASSETS
        },
        "open_orders": view["open_orders"],
        "drawdown_usd": str(st["drawdown_usd"]),
        "mark_at_ms": st["mark_at_ms"],
        "event_count": event_count,
        "unanchored_local_event_chain_sha256": head_hash,
        "orders": {
            k: (
                {"status": v["status"], "reason": v["reason"]}
                if v["status"] == "DENIED"
                else {
                    "status": v["status"],
                    "instrument": v["instrument"],
                    "side": v["side"],
                    "quantity": str(v["quantity"]),
                    "filled_quantity": str(v["filled"]),
                    "remaining_quantity": str(v["quantity"] - v["filled"]),
                    "limit_price": str(v["limit_price"]),
                    "acknowledged": v["acknowledged"],
                    "cancel_requested": v["cancel_requested"],
                }
            )
            for k, v in sorted(st["orders"].items())
        },
    }


class FixturePaperOMS:
    """Local SQLite-only mock order lifecycle, never routes exchange orders."""

    def __init__(self, path: str | Path, policy: PaperPolicy | None = None):
        self.path = Path(path)
        self.policy = policy if policy is not None else PaperPolicy()
        if (
            self.path.is_symlink()
            or self.path.is_dir()
            or not self.path.parent.is_dir()
            or any(part.is_symlink() for part in self.path.parents)
            or ".." in self.path.parts
        ):
            raise PaperReject("Unsafe SQLite ledger path")
        new = not self.path.exists()
        if new:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            with self._connect() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL)""")
                if new:
                    self._insert(
                        c,
                        1,
                        "v24:init",
                        "INIT",
                        "SYSTEM",
                        {"policy": self.policy.mapping()},
                        ZERO_HASH,
                    )
                else:
                    state, _, _ = self._replay(c)
                    if state["policy"].mapping() != self.policy.mapping():
                        raise PaperReject(
                            "Paper risk policy cannot change on ledger reopen"
                        )
        except BaseException:
            if new:
                self.path.unlink(missing_ok=True)
            raise

    @contextmanager
    def _connect(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise PaperReject("Ledger is missing, moved or replaced by symlink")
        if self.path.stat().st_size > MAX_LEDGER_FILE_BYTES:
            raise PaperReject("Paper fixture ledger exceeded bounded storage budget")
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA journal_mode=DELETE")
            yield c
        finally:
            c.close()

    @staticmethod
    def _digest(
        seq: int, token: str, kind: str, order_id: str, payload: str, previous: str
    ) -> str:
        return sha256(
            canonical(
                {
                    "seq": seq,
                    "token": token,
                    "kind": kind,
                    "order_id": order_id,
                    "payload": payload,
                    "previous_hash": previous,
                }
            ).encode()
        ).hexdigest()

    def _insert(self, conn, seq, token, kind, order_id, payload, previous):
        body = canonical(payload)
        digest = self._digest(seq, token, kind, order_id, body, previous)
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (seq, token, kind, order_id, body, previous, digest),
        )
        return digest

    def _replay(self, conn):
        rows = conn.execute(
            (
                'SELECT seq,token,kind,order_'
                'id,payload,previous_hash,eve'
                'nt_hash FROM events ORDER BY'
                ' seq'
            )
        ).fetchall()
        if not rows or rows[0][2] != "INIT":
            raise PaperReject("Missing genesis policy event")
        if len(rows) > MAX_FIXTURE_EVENTS:
            raise PaperReject(
                "Paper fixture event count exceeds bounded replay capacity"
            )
        previous = ZERO_HASH
        state = None
        for i, (seq, token, kind, oid, body, prev, digest) in enumerate(rows, start=1):
            if (
                seq != i
                or prev != previous
                or kind not in ALLOWED_KINDS
                or self._digest(seq, token, kind, oid, body, prev) != digest
            ):
                raise PaperReject(f"LEDGER_TAMPER_OR_GAP at event {i}")
            try:
                p = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PaperReject("Malformed event JSON") from exc
            if body != canonical(p):
                raise PaperReject("Noncanonical event JSON")
            if i == 1:
                if (
                    kind != "INIT"
                    or oid != "SYSTEM"
                    or token != "v24:init"
                    or set(p) != {"policy"}
                ):
                    raise PaperReject("Invalid ledger genesis")
                saved = PaperPolicy(**p["policy"])
                if saved.mapping() != self.policy.mapping():
                    raise PaperReject("Policy mismatch")
                state = _new_state(saved)
            else:
                _apply(state, kind, oid, p, self.policy)
            previous = digest
        return state, previous, len(rows)

    def _write(self, token: str, kind: str, order_id: str, payload: dict) -> dict:
        validate_id(token, "deduplication token")
        validate_id(order_id, "synthetic order identifier")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                st, previous, count = self._replay(conn)
                same = conn.execute(
                    "SELECT kind,order_id,payload FROM events WHERE token=?", (token,)
                ).fetchone()
                if same:
                    saved_kind, saved_id, saved_payload = same
                    # An identical requested INTENT may originally have become a
                    # DENY under concurrent reservations. The retry must remain
                    # idempotent even after the risk state changes.
                    original = json.loads(saved_payload)
                    requested = dict(payload)
                    if saved_kind in ("INTENT", "DENY") and kind in ("INTENT", "DENY"):
                        original.pop("reason", None)
                        requested.pop("reason", None)
                        semantically_equal = original == requested
                    else:
                        semantically_equal = (
                            saved_kind == kind and original == requested
                        )
                    if saved_id != order_id or not semantically_equal:
                        raise PaperReject(
                            "IDEMPOTENCY_CONFLICT: same token with different meaning"
                        )
                    conn.execute("COMMIT")
                    return _state_json(st, previous, count)
                try:
                    _apply(st, kind, order_id, payload, self.policy)
                except PaperReject as exc:
                    # A second concurrent submission can exhaust reserved funds
                    # after preflight. Convert only a new INTENT into a durable
                    # denial INSIDE the exact same SQLite write transaction.
                    if kind != "INTENT" or order_id in st["orders"]:
                        raise
                    kind = "DENY"
                    payload = {**payload, "reason": str(exc)}
                    _apply(st, kind, order_id, payload, self.policy)
                if count >= MAX_FIXTURE_EVENTS:
                    raise PaperReject("Paper fixture event capacity reached")
                head = self._insert(
                    conn, count + 1, token, kind, order_id, payload, previous
                )
                conn.execute("COMMIT")
                return _state_json(st, head, count + 1)
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def snapshot(self) -> dict:
        with self._connect() as conn:
            st, head, count = self._replay(conn)
            return _state_json(st, head, count)

    def mark(self, token: str, quotes: dict[str, FixtureQuote]) -> dict:
        if set(quotes) != set(ASSETS):
            raise PaperReject("Both instruments required")
        return self._write(
            token,
            "MARK",
            "SYSTEM",
            {"quotes": {asset: quote_payload(quotes[asset]) for asset in ASSETS}},
        )

    def risk_mode(
        self, token: str, kind: str, *, reason: str, at_ms: int, manual: bool
    ) -> dict:
        if kind not in ("ARM", "HALT", "RECOVER", "DEGRADE"):
            raise PaperReject("Unsupported safety mode transition")
        return self._write(
            token,
            kind,
            "SYSTEM",
            {
                "reason": reason,
                "at_ms": at_ms,
                "manual": manual,
                "fixture_origin": FIXTURE,
            },
        )

    def submit(
        self,
        token: str,
        client_order_id: str,
        *,
        instrument: str,
        side: str,
        quantity: str,
        limit_price: str,
        quote: FixtureQuote,
    ) -> dict:
        validate_id(client_order_id, "client order id")
        oid = "PAPER:" + client_order_id
        p = {
            "instrument": instrument,
            "side": side,
            "quantity": quantity,
            "limit_price": limit_price,
            "quote": quote_payload(quote),
            "at_ms": quote.decision_ms,
        }
        # Denials are durable and explicit, but cannot be promoted to tradeable orders.
        with self._connect() as conn:
            st, _, _ = self._replay(conn)
        try:
            if (
                st["mark_at_ms"] is None
                or not 0
                <= quote.decision_ms - st["mark_at_ms"]
                <= self.policy.max_staleness_ms
            ):
                raise PaperReject("Missing or stale dual-market marks")
            validate_intent(
                instrument=instrument,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                quote=quote,
                account=_account_view(st, self.policy),
                policy=self.policy,
            )
        except PaperReject as exc:
            return self._write(token, "DENY", oid, {**p, "reason": str(exc)})
        return self._write(token, "INTENT", oid, p)

    def lifecycle(
        self,
        token: str,
        kind: str,
        client_order_id: str,
        *,
        at_ms: int,
        quantity: str | None = None,
        price: str | None = None,
        reason: str | None = None,
    ) -> dict:
        if kind not in ("ACK", "FILL", "CANCEL_REQUEST", "CANCEL_ACK", "REJECT"):
            raise PaperReject("Not an allowed synthetic lifecycle event")
        validate_id(client_order_id, "client order id")
        p = {"at_ms": at_ms, "fixture_origin": FIXTURE}
        if kind == "FILL":
            p.update({"quantity": quantity, "price": price})
        if kind == "REJECT":
            p["reason"] = reason
        return self._write(token, kind, "PAPER:" + client_order_id, p)
