from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "password",
        "proxy-authorization",
        "refresh_token",
        "x-api-key",
    }
)


def _masked_value(
    value: object,
    *,
    key: str | None = None,
) -> object:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "********"

    if isinstance(value, Mapping):
        return {
            str(nested_key): _masked_value(
                nested_value,
                key=str(nested_key),
            )
            for nested_key, nested_value in value.items()
        }

    if isinstance(value, list):
        return [_masked_value(item) for item in value]

    if isinstance(value, tuple):
        return tuple(_masked_value(item) for item in value)

    return value


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True, repr=False)
class TossRequest:
    method: HttpMethod
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, object] | None = None

    def __repr__(self) -> str:
        return (
            "TossRequest("
            f"method={self.method!r}, "
            f"url={self.url!r}, "
            f"headers={_masked_value(self.headers)!r}, "
            f"params={_masked_value(self.params)!r}, "
            f"json_body={_masked_value(self.json_body)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TossResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, object] | None = None
    text: str = ""

    def __repr__(self) -> str:
        masked_text = "********" if self.text else ""
        return (
            "TossResponse("
            f"status_code={self.status_code!r}, "
            f"headers={_masked_value(self.headers)!r}, "
            f"json_body={_masked_value(self.json_body)!r}, "
            f"text={masked_text!r})"
        )
