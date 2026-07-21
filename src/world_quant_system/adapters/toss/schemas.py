from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class TossRequest:
    method: HttpMethod
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TossResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, object] | None = None
    text: str = ""