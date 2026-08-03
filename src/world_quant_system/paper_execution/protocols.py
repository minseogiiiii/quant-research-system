from __future__ import annotations

from typing import Protocol

from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    BrokerOrderSnapshot,
    PaperAccountSnapshot,
    PaperOrderIntent,
)


class ReadOnlyPaperBroker(Protocol):
    """Read-only broker account surface required for certification."""

    async def get_capability_profile(self) -> BrokerCapabilityProfile:
        """Return the certified sandbox or paper capability profile."""
        ...

    async def get_account_snapshot(self) -> PaperAccountSnapshot:
        """Return a point-in-time paper account snapshot."""
        ...

    async def find_order_by_client_order_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        """Look up a paper order by deterministic client order ID."""
        ...


class PaperOrderBroker(ReadOnlyPaperBroker, Protocol):
    """Narrow paper-only write surface used after every safety gate passes."""

    async def submit_limit_order(
        self,
        intent: PaperOrderIntent,
    ) -> BrokerOrderSnapshot:
        """Submit one paper limit order with a deterministic client ID."""
        ...

    async def cancel_order(
        self,
        broker_order_id: str,
    ) -> BrokerOrderSnapshot:
        """Cancel one known paper order."""
        ...
