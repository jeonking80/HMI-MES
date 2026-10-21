"""Real secure OPC UA sessions against a separate loopback server process."""

import os

import pytest
from asyncua import Client, ua

from contracts.models import (
    CommandNotFound,
    CommandResultRequest,
    EventPage,
    EventReadError,
    ReadEventsRequest,
    RunNotFound,
    RunSummaryRequest,
)
from scripts.opcua_poc import (
    launch_server,
    read_tags,
    roundtrip_methods,
    tag_contract,
    trusted_validator,
)


@pytest.fixture(scope="module")
def probe():
    with launch_server() as running:
        assert running.process.pid != os.getpid()
        yield running
    assert running.process.poll() is not None
    assert not running.directory.exists()


async def test_trusted_client_reads_all_48_typed_tags(probe):
    assert len(tag_contract()["tags"]) == 48
    async with await probe.client() as client:
        await read_tags(client)


async def test_unknown_client_certificate_is_rejected(probe):
    client = await probe.client(identity="unknown")
    with pytest.raises(ua.UaStatusCodeError) as rejected:
        async with client:
            pytest.fail("unknown application certificate established a session")
    assert rejected.value.code == ua.StatusCodes.BadCertificateUntrusted


async def test_wrong_server_certificate_is_rejected(probe):
    client = await probe.client(server_pin="wrong-server")
    with pytest.raises((ua.UaError, ConnectionError, TimeoutError)):
        async with client:
            pytest.fail("server with an unexpected certificate established a session")
    # Prove that the rejection did not hide a dead/unreachable server.
    async with await probe.client() as trusted:
        await read_tags(trusted)


async def test_feedback_tag_write_is_rejected(probe):
    async with await probe.client() as client:
        index = await client.get_namespace_index(tag_contract()["namespace_uri"])
        tag = client.get_node(ua.NodeId("Line.State", index))
        previous = await tag.read_value()
        with pytest.raises(ua.UaStatusCodeError) as rejected:
            await tag.write_value(ua.Variant(2, ua.VariantType.Int32))
        assert rejected.value.code in {
            ua.StatusCodes.BadUserAccessDenied,
            ua.StatusCodes.BadNotWritable,
        }
        assert await tag.read_value() == previous


async def test_four_application_contracts_roundtrip(probe):
    async with await probe.client() as client:
        await roundtrip_methods(client)


async def test_reconnect_establishes_new_secure_session(probe):
    async with await probe.client() as first:
        first_id = first.uaclient.session.authentication_token
        await read_tags(first)
    async with await probe.client() as second:
        assert second.uaclient.session.authentication_token != first_id
        await read_tags(second)


async def test_malformed_method_json_returns_bad_invalid_argument(probe):
    async with await probe.client() as client:
        index = await client.get_namespace_index(tag_contract()["namespace_uri"])
        root = client.get_node(ua.NodeId("PoC", index))
        with pytest.raises(ua.UaStatusCodeError) as rejected:
            await root.call_method(ua.NodeId("SubmitCommand", index), "{broken")
        assert rejected.value.code == ua.StatusCodes.BadInvalidArgument


async def test_server_identity_validator_rejects_untrusted_server(probe):
    client = await probe.client()
    client.certificate_validator = trusted_validator(
        probe.directory / "wrong-server.der",
        peer_server=True,
    )
    with pytest.raises(ua.UaStatusCodeError) as rejected:
        async with client:
            pytest.fail("server identity bypassed the configured trust validator")
    assert rejected.value.code == ua.StatusCodes.BadCertificateUntrusted


async def test_unencrypted_application_session_is_rejected(probe):
    with pytest.raises(ua.UaError):
        async with Client(probe.endpoint, timeout=2):
            pytest.fail("unsecured session established")


async def test_method_errors_and_event_cursor_use_shared_contracts(probe):
    async with await probe.client() as client:
        index = await client.get_namespace_index(tag_contract()["namespace_uri"])
        root = client.get_node(ua.NodeId("PoC", index))
        for name, request, response_model, expected_code in (
            (
                "GetCommandResult",
                CommandResultRequest(command_id="absent"),
                CommandNotFound,
                "COMMAND_NOT_FOUND",
            ),
            (
                "GetRunSummary",
                RunSummaryRequest(execution_id="absent"),
                RunNotFound,
                "RUN_NOT_FOUND",
            ),
            (
                "ReadEvents",
                ReadEventsRequest(source_id="simulator-1", stream_id="stream-1", after_seq=2),
                EventReadError,
                "CURSOR_AHEAD",
            ),
            (
                "ReadEvents",
                ReadEventsRequest(source_id="absent", stream_id="absent", after_seq=0),
                EventReadError,
                "STREAM_MISMATCH",
            ),
        ):
            result = await root.call_method(ua.NodeId(name, index), request.model_dump_json())
            assert response_model.model_validate_json(result).code == expected_code
        request = ReadEventsRequest(source_id="simulator-1", stream_id="stream-1", after_seq=1)
        result = await root.call_method(ua.NodeId("ReadEvents", index), request.model_dump_json())
        page = EventPage.model_validate_json(result)
        assert page.after_seq == 1 and page.page_last_seq == 1
        assert page.events == () and not page.has_more
