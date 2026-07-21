from world_quant_system.adapters.toss.schemas import (
    HttpMethod,
    TossRequest,
    TossResponse,
)


def test_request_defaults_are_isolated() -> None:
    first = TossRequest(
        method=HttpMethod.GET,
        url="https://example.test/one",
    )
    second = TossRequest(
        method=HttpMethod.GET,
        url="https://example.test/two",
    )

    assert first.headers == {}
    assert first.params == {}
    assert first.headers is not second.headers
    assert first.params is not second.params


def test_response_defaults_are_isolated() -> None:
    first = TossResponse(status_code=200)
    second = TossResponse(status_code=204)

    assert first.headers == {}
    assert first.headers is not second.headers
    assert first.json_body is None
    assert first.text == ""
