from __future__ import annotations

from urllib.parse import urlsplit

from world_quant_system.disabled_write_transport.models import (
    DisabledWriteTransportPolicy,
    TransportIntegrationCheck,
)
from world_quant_system.order_write_certification.models import (
    OFFICIAL_ORDER_CREATE_METHOD,
    OFFICIAL_ORDER_CREATE_PATH,
    OFFICIAL_TOSS_BASE_URL,
    CompiledTossOrderRequest,
)


class PinnedTossWriteEndpointPolicy:
    """Validate the exact Toss order-create target without opening a socket."""

    def evaluate(
        self,
        request: CompiledTossOrderRequest,
        policy: DisabledWriteTransportPolicy,
    ) -> tuple[TransportIntegrationCheck, ...]:
        parsed = urlsplit(request.contract.base_url)
        host_pinned = (
            request.contract.base_url == OFFICIAL_TOSS_BASE_URL
            and parsed.scheme == "https"
            and parsed.hostname == "openapi.tossinvest.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.query == ""
            and parsed.fragment == ""
        )
        path_pinned = (
            request.contract.path == OFFICIAL_ORDER_CREATE_PATH
            and request.contract.path == policy.path
            and "?" not in request.contract.path
            and "#" not in request.contract.path
        )
        method_pinned = (
            request.contract.method == OFFICIAL_ORDER_CREATE_METHOD
            and request.contract.method == policy.method
        )
        return (
            TransportIntegrationCheck(
                name="official_https_host_pinned",
                passed=host_pinned,
                detail=(
                    "Official HTTPS host is pinned without userinfo, port, "
                    "query, or fragment."
                    if host_pinned
                    else "Write target is not the pinned official HTTPS host."
                ),
            ),
            TransportIntegrationCheck(
                name="order_create_path_pinned",
                passed=path_pinned,
                detail=(
                    "Order-create path is pinned and contains no query or fragment."
                    if path_pinned
                    else "Order-create path is not pinned."
                ),
            ),
            TransportIntegrationCheck(
                name="post_method_pinned",
                passed=method_pinned,
                detail=(
                    "Write method is pinned to POST."
                    if method_pinned
                    else "Write method is not pinned to POST."
                ),
            ),
        )
