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


def test_request_repr_masks_authorization_and_nested_secrets() -> None:
    request = TossRequest(
        method=HttpMethod.POST,
        url="https://example.test/resource",
        headers={"Authorization": "Bearer top-secret"},
        params={"access_token": "query-secret"},
        json_body={
            "client_secret": "body-secret",
            "nested": {"refresh_token": "nested-secret"},
        },
    )

    rendered = repr(request)

    assert "top-secret" not in rendered
    assert "query-secret" not in rendered
    assert "body-secret" not in rendered
    assert "nested-secret" not in rendered
    assert rendered.count("********") >= 4


def test_response_repr_masks_sensitive_body_and_raw_text() -> None:
    response = TossResponse(
        status_code=401,
        headers={"X-Api-Key": "header-secret"},
        json_body={"access_token": "response-secret"},
        text='{"access_token":"text-secret"}',
    )

    rendered = repr(response)

    assert "header-secret" not in rendered
    assert "response-secret" not in rendered
    assert "text-secret" not in rendered
    assert rendered.count("********") >= 3
