from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
from datetime import date
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit

from world_quant_system.toss_live_read.models import (
    ACCOUNTS_PATH,
    HOLDINGS_PATH,
    OFFICIAL_TOKEN_PATH,
    OFFICIAL_TOSS_HOST,
    OFFICIAL_TOSS_PORT,
    ORDERS_PATH,
    TossHttpResponse,
    TossLiveReadSafetyError,
    TossLiveReadTransportError,
)

_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "x-request-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "retry-after",
    }
)


class TossTransport(Protocol):
    call_count: int
    broker_write_count: int
    redirect_count: int

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> TossHttpResponse:
        """Perform one already-authorized Toss request."""
        ...


class StrictTossHttpsTransport:
    """Narrow HTTPS transport with no proxy or redirect support."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._maximum_response_bytes = maximum_response_bytes
        self._tls_context = ssl.create_default_context()
        if (
            not self._tls_context.check_hostname
            or self._tls_context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise TossLiveReadSafetyError(
                "TLS certificate and hostname verification are required."
            )
        self.call_count = 0
        self.broker_write_count = 0
        self.redirect_count = 0

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> TossHttpResponse:
        normalized_method = method.upper()
        _validate_request_boundary(normalized_method, path)
        self.call_count += 1
        connection = http.client.HTTPSConnection(
            OFFICIAL_TOSS_HOST,
            OFFICIAL_TOSS_PORT,
            timeout=self._timeout_seconds,
            context=self._tls_context,
        )
        try:
            connection.request(
                normalized_method,
                path,
                body=body,
                headers=dict(headers),
            )
            response = connection.getresponse()
            payload = response.read(self._maximum_response_bytes + 1)
            if len(payload) > self._maximum_response_bytes:
                raise TossLiveReadTransportError(
                    "Toss response exceeded the configured byte limit."
                )
            if 300 <= response.status <= 399:
                self.redirect_count += 1
                raise TossLiveReadSafetyError("HTTP redirects are prohibited.")
            safe_headers = tuple(
                sorted(
                    (
                        name.casefold(),
                        value,
                    )
                    for name, value in response.getheaders()
                    if name.casefold() in _SAFE_RESPONSE_HEADERS
                )
            )
            try:
                parsed = cast(object, json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TossLiveReadTransportError(
                    "Toss returned an invalid JSON response."
                ) from error
            return TossHttpResponse(
                status_code=response.status,
                headers=safe_headers,
                json_body=parsed,
            )
        except (OSError, http.client.HTTPException) as error:
            raise TossLiveReadTransportError(
                "Toss HTTPS request failed before a valid response was read."
            ) from error
        finally:
            connection.close()


def _validate_request_boundary(method: str, path: str) -> None:
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "@" in path
    ):
        raise TossLiveReadSafetyError(
            "Request target contains a forbidden URL component."
        )
    if method == "POST":
        if parsed.path != OFFICIAL_TOKEN_PATH or parsed.query:
            raise TossLiveReadSafetyError(
                "Only the OAuth token endpoint may receive POST requests."
            )
        return
    if method != "GET":
        raise TossLiveReadSafetyError("Only GET and OAuth POST are permitted.")
    if parsed.path == ACCOUNTS_PATH:
        if parsed.query:
            raise TossLiveReadSafetyError("Accounts query parameters are prohibited.")
        return
    if parsed.path == HOLDINGS_PATH:
        if parsed.query:
            raise TossLiveReadSafetyError("Holdings query parameters are prohibited.")
        return
    if parsed.path != ORDERS_PATH:
        raise TossLiveReadSafetyError("GET path is outside the read-only allowlist.")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) - {"status", "from", "to", "limit", "cursor"}:
        raise TossLiveReadSafetyError("Orders query contains a forbidden parameter.")
    statuses = query.get("status")
    if statuses not in (["OPEN"], ["CLOSED"]):
        raise TossLiveReadSafetyError("Orders status must be OPEN or CLOSED.")
    if statuses == ["OPEN"] and set(query) != {"status"}:
        raise TossLiveReadSafetyError("OPEN orders cannot use pagination parameters.")
    if statuses == ["CLOSED"]:
        from_values = query.get("from")
        to_values = query.get("to")
        if (
            from_values is None
            or len(from_values) != 1
            or to_values is None
            or len(to_values) != 1
        ):
            raise TossLiveReadSafetyError(
                "CLOSED orders require one from and one to date."
            )
        try:
            start_date = date.fromisoformat(from_values[0])
            end_date = date.fromisoformat(to_values[0])
        except ValueError as error:
            raise TossLiveReadSafetyError(
                "Closed-order dates must use ISO format."
            ) from error
        if start_date > end_date:
            raise TossLiveReadSafetyError(
                "Closed-order from date cannot follow to date."
            )
        limits = query.get("limit")
        if limits is None or len(limits) != 1:
            raise TossLiveReadSafetyError("CLOSED orders require one limit value.")
        try:
            limit = int(limits[0])
        except ValueError as error:
            raise TossLiveReadSafetyError(
                "Closed-order limit must be an integer."
            ) from error
        if not 1 <= limit <= 100:
            raise TossLiveReadSafetyError(
                "Closed-order limit must be between 1 and 100."
            )
        cursors = query.get("cursor")
        if cursors is not None and (len(cursors) != 1 or not cursors[0]):
            raise TossLiveReadSafetyError(
                "Closed-order cursor must be one nonblank value."
            )
