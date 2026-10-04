import asyncio
import gzip
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from world_quant_system.data import (
    FileRawMarketDataStore,
    RawMarketDataCapture,
    RawMarketDataConfigurationError,
    RawMarketDataConflictError,
    RawMarketDataIntegrityError,
    RawMarketDataNotFoundError,
    RawMarketDataSerializationError,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def capture(
    *,
    endpoint: str = "/api/v1/prices",
    captured_at: datetime = NOW,
    json_body: dict[str, object] | None = None,
    response_headers: dict[str, str] | None = None,
    idempotency_key: str | None = "capture-1",
) -> RawMarketDataCapture:
    return RawMarketDataCapture(
        provider="toss",
        endpoint=endpoint,
        request_params={"symbols": "005930"},
        captured_at=captured_at,
        status_code=200,
        response_headers=response_headers or {"X-Request-Id": "request-1"},
        json_body=json_body
        or {
            "result": [
                {
                    "symbol": "005930",
                    "lastPrice": "95000",
                    "timestamp": "2026-07-21T20:59:59+09:00",
                    "currency": "KRW",
                }
            ]
        },
        request_id="request-1",
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_record_read_query_and_verify_round_trip(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    metadata = await store.record(capture())
    record = await store.read(metadata.record_id)
    verified = await store.verify(metadata.record_id)
    queried = await store.query(provider="toss", endpoint="/api/v1/prices")

    assert verified == metadata
    assert queried == (metadata,)
    assert record.metadata == metadata
    assert record.request_params == {"symbols": "005930"}
    assert record.json_body is not None
    expected_body = capture().json_body
    assert expected_body is not None
    stored_result = record.json_body["result"]
    expected_result = expected_body["result"]
    assert isinstance(stored_result, list)
    assert isinstance(expected_result, tuple)
    assert stored_result[0] == dict(expected_result[0])
    assert (store.root / metadata.relative_path).is_file()
    assert metadata.relative_path.startswith("blobs/toss/2026/07/21/")


@pytest.mark.asyncio
async def test_same_idempotency_key_returns_existing_record(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    first = await store.record(capture())
    second = await store.record(capture(captured_at=NOW + timedelta(seconds=30)))

    assert second == first
    assert len(await store.query()) == 1
    assert len(list((store.root / "blobs").rglob("*.json.gz"))) == 1


@pytest.mark.asyncio
async def test_idempotency_key_rejects_different_content(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")
    await store.record(capture())
    changed = capture(json_body={"result": []})

    with pytest.raises(RawMarketDataConflictError, match="different content"):
        await store.record(changed)


@pytest.mark.asyncio
async def test_concurrent_idempotent_writers_create_one_record(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    results = await asyncio.gather(*(store.record(capture()) for _ in range(100)))

    assert len({item.record_id for item in results}) == 1
    assert len(await store.query()) == 1
    assert len(list((store.root / "blobs").rglob("*.json.gz"))) == 1


@pytest.mark.asyncio
async def test_sqlite_connections_are_closed_after_each_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_connection_ids: set[int] = set()
    closed_connection_ids: set[int] = set()
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed_connection_ids.add(id(self))
            super().close()

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = cast(
            sqlite3.Connection,
            original_connect(*args, **kwargs),
        )
        opened_connection_ids.add(id(connection))
        return connection

    monkeypatch.setattr(
        sqlite3,
        "connect",
        tracking_connect,
    )

    store = FileRawMarketDataStore(tmp_path / "raw")
    await store.record(capture())
    await store.query()

    assert opened_connection_ids
    assert closed_connection_ids == opened_connection_ids


@pytest.mark.asyncio
async def test_records_without_idempotency_key_are_append_only(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    first = await store.record(capture(idempotency_key=None))
    second = await store.record(capture(idempotency_key=None))

    assert first.record_id != second.record_id
    assert len(await store.query()) == 2


@pytest.mark.asyncio
async def test_sensitive_headers_and_parameters_are_redacted(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")
    raw_capture = RawMarketDataCapture(
        provider="toss",
        endpoint="/api/v1/prices",
        request_params={"symbols": "005930", "api_key": "do-not-store"},
        captured_at=NOW,
        status_code=200,
        response_headers={
            "Set-Cookie": "do-not-store",
            "Authorization": "Bearer do-not-store",
            "X-Request-Id": "safe-id",
        },
        json_body={"result": []},
    )

    metadata = await store.record(raw_capture)
    record = await store.read(metadata.record_id)

    assert record.request_params["api_key"] == "[REDACTED]"
    assert record.response_headers["Set-Cookie"] == "[REDACTED]"
    assert record.response_headers["Authorization"] == "[REDACTED]"
    assert record.response_headers["X-Request-Id"] == "safe-id"
    raw_bytes = gzip.decompress((store.root / metadata.relative_path).read_bytes())
    assert b"do-not-store" not in raw_bytes


@pytest.mark.asyncio
async def test_sensitive_json_payload_is_rejected(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    with pytest.raises(RawMarketDataSerializationError, match="Sensitive field"):
        await store.record(capture(json_body={"access_token": "secret"}))

    assert await store.query() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), object()],
)
async def test_unsupported_json_value_is_rejected(
    tmp_path: Path,
    bad_value: object,
) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    with pytest.raises(RawMarketDataSerializationError):
        await store.record(capture(json_body={"result": bad_value}))


@pytest.mark.asyncio
async def test_size_limit_is_enforced_before_persistence(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(
        tmp_path / "raw",
        max_uncompressed_bytes=256,
    )

    with pytest.raises(RawMarketDataSerializationError, match="size limit"):
        await store.record(capture(json_body={"result": "x" * 1_000}))

    assert await store.query() == ()


@pytest.mark.asyncio
async def test_tampered_file_is_detected(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")
    metadata = await store.record(capture())
    path = store.root / metadata.relative_path
    path.write_bytes(gzip.compress(b'{"tampered":true}', mtime=0))

    with pytest.raises(RawMarketDataIntegrityError):
        await store.read(metadata.record_id)


@pytest.mark.asyncio
async def test_missing_file_is_detected(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")
    metadata = await store.record(capture())
    (store.root / metadata.relative_path).unlink()

    with pytest.raises(RawMarketDataIntegrityError, match="missing"):
        await store.verify(metadata.record_id)


@pytest.mark.asyncio
async def test_unknown_record_is_reported(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")

    with pytest.raises(RawMarketDataNotFoundError):
        await store.read("00000000-0000-4000-8000-000000000000")


@pytest.mark.asyncio
async def test_query_filters_and_orders_by_capture_time(tmp_path: Path) -> None:
    store = FileRawMarketDataStore(tmp_path / "raw")
    later = await store.record(
        capture(
            endpoint="/api/v1/candles",
            captured_at=NOW + timedelta(minutes=1),
            idempotency_key="later",
        )
    )
    earlier = await store.record(capture(captured_at=NOW, idempotency_key="earlier"))

    all_rows = await store.query()
    prices = await store.query(endpoint="/api/v1/prices")
    window = await store.query(
        captured_from=NOW + timedelta(seconds=30),
        captured_to=NOW + timedelta(minutes=2),
    )

    assert all_rows == (earlier, later)
    assert prices == (earlier,)
    assert window == (later,)


@pytest.mark.asyncio
async def test_interrupted_pending_record_is_recovered_on_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    store = FileRawMarketDataStore(root)
    metadata = await store.record(capture())
    final_path = root / metadata.relative_path
    pending_path = root / ".pending" / f"{metadata.record_id}.json.gz"

    os.replace(final_path, pending_path)
    with sqlite3.connect(root / "catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE raw_records SET state = 'pending' WHERE record_id = ?",
            (metadata.record_id,),
        )

    restarted = FileRawMarketDataStore(root)
    recovered = await restarted.read(metadata.record_id)

    assert recovered.metadata == metadata
    assert final_path.is_file()
    assert not pending_path.exists()


def test_invalid_compression_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RawMarketDataConfigurationError):
        FileRawMarketDataStore(tmp_path / "raw", compression_level=-1)

    with pytest.raises(RawMarketDataConfigurationError):
        FileRawMarketDataStore(tmp_path / "raw", compression_level=10)


def test_invalid_size_limit_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RawMarketDataConfigurationError):
        FileRawMarketDataStore(tmp_path / "raw", max_uncompressed_bytes=0)


def test_capture_snapshots_mutable_input() -> None:
    request_params = {"symbols": "005930"}
    response_headers = {"X-Request-Id": "request-1"}
    json_body: dict[str, object] = {"result": [{"symbol": "005930"}]}

    item = RawMarketDataCapture(
        provider="toss",
        endpoint="/api/v1/prices",
        request_params=request_params,
        captured_at=NOW,
        status_code=200,
        response_headers=response_headers,
        json_body=json_body,
    )
    request_params["symbols"] = "CHANGED"
    response_headers["X-Request-Id"] = "CHANGED"
    json_body["result"] = []

    assert item.request_params == {"symbols": "005930"}
    assert item.response_headers == {"X-Request-Id": "request-1"}
    assert item.json_body is not None
    assert item.json_body["result"] != ()


@pytest.mark.parametrize(
    "endpoint",
    [
        "api/v1/prices",
        "/api/v1/prices?token=secret",
        "/api/v1/prices#fragment",
        "https://example.test/api/v1/prices",
    ],
)
def test_unsafe_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(RawMarketDataConfigurationError):
        capture(endpoint=endpoint)


def test_sensitive_response_text_is_rejected() -> None:
    with pytest.raises(RawMarketDataSerializationError, match="Sensitive content"):
        RawMarketDataCapture(
            provider="toss",
            endpoint="/api/v1/prices",
            request_params={},
            captured_at=NOW,
            status_code=200,
            response_headers={},
            json_body=None,
            text='{"access_token":"do-not-store"}',
        )


@pytest.mark.asyncio
async def test_catalog_metadata_tampering_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    store = FileRawMarketDataStore(root)
    metadata = await store.record(capture())

    with sqlite3.connect(root / "catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE raw_records SET endpoint = ? WHERE record_id = ?",
            ("/api/v1/candles", metadata.record_id),
        )

    with pytest.raises(RawMarketDataIntegrityError, match="endpoint"):
        await store.read(metadata.record_id)


def test_capture_and_record_representations_do_not_expose_content(
    tmp_path: Path,
) -> None:
    item = RawMarketDataCapture(
        provider="toss",
        endpoint="/api/v1/prices",
        request_params={"api_key": "TOP-SECRET"},
        captured_at=NOW,
        status_code=200,
        response_headers={"Authorization": "Bearer TOP-SECRET"},
        json_body={"result": [{"symbol": "005930"}]},
        text="PRIVATE-BODY",
    )

    capture_repr = repr(item)
    assert "TOP-SECRET" not in capture_repr
    assert "PRIVATE-BODY" not in capture_repr

    async def verify_record_repr() -> None:
        store = FileRawMarketDataStore(tmp_path / "raw")
        metadata = await store.record(item)
        record = await store.read(metadata.record_id)
        record_repr = repr(record)
        assert "TOP-SECRET" not in record_repr
        assert "PRIVATE-BODY" not in record_repr
        assert "005930" not in record_repr

    asyncio.run(verify_record_repr())
