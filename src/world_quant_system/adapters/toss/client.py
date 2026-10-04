from collections.abc import Mapping
from typing import Protocol

from world_quant_system.adapters.toss.errors import (
    TossAdapterError,
    TossAuthenticationError,
    TossAuthorizationError,
    TossClientResponseError,
    TossConfigurationError,
    TossInvalidResponseError,
    TossRateLimitError,
    TossServerResponseError,
    TossTransportError,
)
from world_quant_system.adapters.toss.schemas import (
    HttpMethod,
    TossRequest,
    TossResponse,
)
from world_quant_system.adapters.toss.token import AccessToken
from world_quant_system.adapters.toss.token_manager import TossTokenManager


class TossTransport(Protocol):
    async def send(
        self,
        request: TossRequest,
    ) -> TossResponse:
        """Send one prepared request and return its response."""
        ...


class NoNetworkTransport:
    """Fail closed until a real network transport is configured."""

    async def send(
        self,
        request: TossRequest,
    ) -> TossResponse:
        del request

        raise TossTransportError("Network transport is not configured.")


class TossHttpClient:
    """Prepare Toss HTTP requests with optional managed authentication.

    Supplying a token manager enables automatic Bearer authentication. A 401
    response invalidates only the token used by that request, obtains a shared
    replacement through ``TossTokenManager``, and retries exactly once.
    """

    def __init__(
        self,
        base_url: str,
        *,
        transport: TossTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
        token_manager: TossTokenManager | None = None,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")

        if not normalized_base_url.startswith("https://"):
            raise TossConfigurationError("Toss base URL must use HTTPS.")

        copied_default_headers = dict(default_headers or {})
        if token_manager is not None:
            self._reject_managed_authorization(copied_default_headers)

        self._base_url = normalized_base_url
        self._transport = transport or NoNetworkTransport()
        self._default_headers = copied_default_headers
        self._token_manager = token_manager

    async def get(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> TossResponse:
        return await self.request(
            HttpMethod.GET,
            path,
            headers=headers,
            params=params,
        )

    async def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> TossResponse:
        return await self.request(
            HttpMethod.POST,
            path,
            headers=headers,
            json_body=json_body,
        )

    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> TossResponse:
        normalized_path = self._normalize_path(path)
        copied_headers = dict(headers or {})
        token_manager = self._token_manager

        if token_manager is None:
            return await self._send_once(
                method,
                normalized_path,
                headers=copied_headers,
                params=params,
                json_body=json_body,
            )

        self._reject_managed_authorization(copied_headers)
        token = await token_manager.get_token()

        try:
            return await self._send_authenticated_once(
                method,
                normalized_path,
                token=token,
                headers=copied_headers,
                params=params,
                json_body=json_body,
            )
        except TossAuthenticationError:
            await token_manager.invalidate_if_current(token)

        replacement = await token_manager.get_token()
        try:
            return await self._send_authenticated_once(
                method,
                normalized_path,
                token=replacement,
                headers=copied_headers,
                params=params,
                json_body=json_body,
            )
        except TossAuthenticationError:
            await token_manager.invalidate_if_current(replacement)
            raise

    async def _send_authenticated_once(
        self,
        method: HttpMethod,
        normalized_path: str,
        *,
        token: AccessToken,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None,
        json_body: Mapping[str, object] | None,
    ) -> TossResponse:
        authenticated_headers = {
            **headers,
            "Authorization": token.authorization_header(),
        }
        return await self._send_once(
            method,
            normalized_path,
            headers=authenticated_headers,
            params=params,
            json_body=json_body,
        )

    async def _send_once(
        self,
        method: HttpMethod,
        normalized_path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None,
        json_body: Mapping[str, object] | None,
    ) -> TossResponse:
        request = TossRequest(
            method=method,
            url=f"{self._base_url}{normalized_path}",
            headers=self._merge_headers(
                self._default_headers,
                headers,
            ),
            params=dict(params or {}),
            json_body=(None if json_body is None else dict(json_body)),
        )

        try:
            response = await self._transport.send(request)
        except TossAdapterError:
            raise
        except Exception as error:
            raise TossTransportError("Transport failed unexpectedly.") from error

        self._raise_for_status(response)
        return response

    @staticmethod
    def _merge_headers(
        defaults: Mapping[str, str],
        overrides: Mapping[str, str],
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        original_names: dict[str, str] = {}

        for source in (defaults, overrides):
            for name, value in source.items():
                normalized_name = name.casefold()
                previous_name = original_names.get(normalized_name)
                if previous_name is not None:
                    del merged[previous_name]

                merged[name] = value
                original_names[normalized_name] = name

        return merged

    @staticmethod
    def _reject_managed_authorization(
        headers: Mapping[str, str],
    ) -> None:
        if any(name.casefold() == "authorization" for name in headers):
            raise TossConfigurationError(
                "Authorization header is managed by the token manager."
            )

    @staticmethod
    def _normalize_path(path: str) -> str:
        stripped_path = path.strip()

        if not stripped_path:
            raise TossConfigurationError("Request path cannot be empty.")

        if "://" in stripped_path:
            raise TossConfigurationError("Request path must not contain a full URL.")

        return f"/{stripped_path.lstrip('/')}"

    @classmethod
    def _raise_for_status(
        cls,
        response: TossResponse,
    ) -> None:
        status_code = response.status_code

        if 200 <= status_code < 300:
            return

        request_id = cls._get_header(
            response.headers,
            "X-Request-Id",
        )

        if status_code == 401:
            raise TossAuthenticationError(
                "Toss authentication failed.",
                status_code=status_code,
                request_id=request_id,
            )

        if status_code == 403:
            raise TossAuthorizationError(
                "Toss API access was forbidden.",
                status_code=status_code,
                request_id=request_id,
            )

        if status_code == 429:
            retry_after_seconds = cls._parse_retry_after(
                cls._get_header(
                    response.headers,
                    "Retry-After",
                )
            )

            raise TossRateLimitError(
                "Toss API rate limit was exceeded.",
                status_code=status_code,
                request_id=request_id,
                retry_after_seconds=retry_after_seconds,
            )

        if 400 <= status_code < 500:
            raise TossClientResponseError(
                "Toss API rejected the request.",
                status_code=status_code,
                request_id=request_id,
            )

        if 500 <= status_code < 600:
            raise TossServerResponseError(
                "Toss API server failed.",
                status_code=status_code,
                request_id=request_id,
            )

        raise TossInvalidResponseError(
            "Toss API returned an invalid HTTP status.",
            status_code=status_code,
            request_id=request_id,
        )

    @staticmethod
    def _get_header(
        headers: Mapping[str, str],
        expected_name: str,
    ) -> str | None:
        normalized_expected_name = expected_name.casefold()

        for name, value in headers.items():
            if name.casefold() == normalized_expected_name:
                return value

        return None

    @staticmethod
    def _parse_retry_after(
        value: str | None,
    ) -> int | None:
        if value is None:
            return None

        try:
            retry_after_seconds = int(value)
        except ValueError:
            return None

        if retry_after_seconds < 0:
            return None

        return retry_after_seconds
