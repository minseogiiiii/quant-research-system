"""Fail-closed BTC/ETH L2 connection recovery state machine.

Only protects downstream *research data admission*. Not an order router,
trading system, exchange-gap recovery implementation, or source authenticator.
A new connection requires a fresh snapshot for BOTH products. Every channel's
connection-level sequence number must remain continuous once started.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PRODUCTS = frozenset(('BTC-USD', 'ETH-USD'))


class RecoveryRequired(ValueError):
    """Untrusted connection cannot be promoted without an entirely new session."""


@dataclass
class BookAdmissionGate:
    state: str = 'DISCONNECTED'
    last_sequence: int | None = None
    snapshots: set[str] = field(default_factory=set)
    updates: dict[str, int] = field(default_factory=lambda: {p: 0 for p in PRODUCTS})
    accepted_messages: int = 0
    completed_connections: int = 0
    invalidated_connections: int = 0

    @property
    def ready(self) -> bool:
        return self.state == 'READY'

    def disconnected(self) -> None:
        if self.state in ('WAITING', 'READY'):
            self.invalidated_connections += 1
        self.state = 'DISCONNECTED'
        self.last_sequence = None
        self.snapshots.clear()
        self.updates = {p: 0 for p in PRODUCTS}

    def connected(self) -> None:
        if self.state != 'DISCONNECTED':
            raise RecoveryRequired('Close and invalidate the previous session first')
        self.state = 'WAITING'
        self.last_sequence = None
        self.snapshots.clear()
        self.updates = {p: 0 for p in PRODUCTS}
        self.accepted_messages = 0

    def _reject(self, message: str) -> None:
        self.state = 'RESYNC_REQUIRED'
        self.snapshots.clear()
        self.last_sequence = None
        raise RecoveryRequired(message)

    def consume(self, msg: object) -> bool:
        """Returns True only after both fresh snapshots and no connection gap.

        Caller must discard any candidate features produced before ready=True.
        A data error requires disconnect(), then connected() and new snapshots.
        """
        if self.state in ('DISCONNECTED', 'RESYNC_REQUIRED'):
            raise RecoveryRequired('Cannot admit messages without a new connection')
        if not isinstance(msg, dict):
            self._reject('Bad WebSocket message')
        seq = msg.get('sequence_num')
        if type(seq) is not int or seq < 0:
            self._reject('Invalid WebSocket connection sequence')
        if self.last_sequence is not None and seq != self.last_sequence + 1:
            self._reject('Connection sequence gap or replay')
        channel = msg.get('channel')
        if channel not in ('l2_data', 'market_trades', 'heartbeats', 'subscriptions'):
            self._reject('Unexpected channel')
        events = msg.get('events')
        if not isinstance(events, list):
            self._reject('Missing events')
        if channel == 'l2_data':
            for item in events:
                if not isinstance(item, dict):
                    self._reject('Malformed level2 event')
                product, kind = item.get('product_id'), item.get('type')
                if product not in PRODUCTS or kind not in ('snapshot', 'update'):
                    self._reject('Unrecognized product or event type')
                if not isinstance(item.get('updates'), list):
                    self._reject('Malformed book change list')
                if kind == 'snapshot':
                    if product in self.snapshots:
                        self._reject('Unexpected additional snapshot on same session')
                    self.snapshots.add(product)
                elif product not in self.snapshots:
                    self._reject('Book delta received before initial snapshot')
                else:
                    self.updates[product] += 1
        self.last_sequence = seq
        self.accepted_messages += 1
        if self.state == 'WAITING' and self.snapshots == PRODUCTS:
            self.state = 'READY'
            self.completed_connections += 1
        return self.ready
