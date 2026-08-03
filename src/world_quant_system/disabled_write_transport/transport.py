from __future__ import annotations

from datetime import datetime
from typing import Protocol

from world_quant_system.disabled_write_transport.models import (
    AuthorizedWriteEnvelope,
    TransportBlockReceipt,
)


class TossWriteTransport(Protocol):
    async def submit(
        self,
        envelope: AuthorizedWriteEnvelope,
        *,
        evaluated_at: datetime,
    ) -> TransportBlockReceipt: ...


class DisabledTossWriteTransport:
    """A terminal transport boundary that cannot perform I/O or writes."""

    async def submit(
        self,
        envelope: AuthorizedWriteEnvelope,
        *,
        evaluated_at: datetime,
    ) -> TransportBlockReceipt:
        return TransportBlockReceipt(
            envelope_digest=envelope.envelope_digest,
            request_digest=envelope.compiled_request.request_digest,
            evaluated_at=evaluated_at,
        )
