"""IM creation survives transient transport failures without duplicate messages."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import STREAMING
from hermes_lark_streaming.feishu.client import (
    _TRANSIENT_RETRY_DELAYS,
    FeishuAPIError,
    FeishuClient,
)
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody


def response(message_id="sent-message", code=0):
    return SimpleNamespace(
        success=lambda: code == 0, code=code, msg="rejected" if code else "ok",
        data=SimpleNamespace(message_id=message_id), get_log_id=lambda: "",
    )


@pytest.fixture
def delivery(monkeypatch):
    client = object.__new__(FeishuClient)
    create = AsyncMock(return_value=response())
    reply = AsyncMock(return_value=response())
    client._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(acreate=create, areply=reply),
    )))
    client._card_json = AsyncMock(side_effect=json.dumps)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return SimpleNamespace(client=client, create=create, reply=reply, sleep=sleep)


@pytest.mark.asyncio
@pytest.mark.parametrize("method, args, endpoint", [
    ("reply_card", ("anchor", {"schema": "2.0"}), "reply"),
    ("reply_card_by_id", ("anchor", "card-id"), "reply"),
    ("reply_text", ("anchor", "fallback"), "reply"),
    ("send_card_to_chat", ("chat", {"schema": "2.0"}), "create"),
    ("send_card_by_id_to_chat", ("chat", "card-id"), "create"),
])
async def test_connection_failure_retries_same_idempotent_request(delivery, method, args, endpoint):
    send = getattr(delivery, endpoint)
    send.side_effect = [httpx.ConnectError("TLS handshake failed"), response()]

    assert await getattr(delivery.client, method)(*args) == "sent-message"

    assert send.await_count == 2
    first, second = (call.args[0] for call in send.await_args_list)
    assert first is second
    assert first.request_body.uuid
    assert first.body.uuid == second.body.uuid
    delivery.sleep.assert_awaited_once_with(_TRANSIENT_RETRY_DELAYS[0])


@pytest.mark.asyncio
async def test_lost_response_reuses_server_deduplication_key(delivery):
    committed = {}
    attempts = []

    async def send(request):
        key = request.request_body.uuid
        assert key
        attempts.append(key)
        if key not in committed:
            committed[key] = response()
            raise httpx.ReadTimeout("Reply was committed but response was lost")
        return committed[key]

    delivery.reply.side_effect = send
    assert await delivery.client.reply_card("anchor", {}) == "sent-message"
    assert len(committed) == 1
    assert len(attempts) == 2 and attempts[0] == attempts[1]


@pytest.mark.asyncio
async def test_independent_messages_get_different_keys(delivery):
    await delivery.client.reply_card("anchor", {})
    await delivery.client.reply_card("anchor", {})
    keys = [call.args[0].request_body.uuid for call in delivery.reply.await_args_list]
    assert all(keys) and keys[0] != keys[1]


@pytest.mark.asyncio
async def test_caller_supplied_key_is_preserved(delivery):
    request = ReplyMessageRequest.builder().message_id("anchor").request_body(
        ReplyMessageRequestBody.builder().msg_type("text").content("{}").uuid("existing-key").build(),
    ).build()
    delivery.reply.side_effect = [httpx.ConnectError("TLS"), response()]
    await delivery.client._reply_message("test", request)
    assert request.body.uuid == "existing-key"


@pytest.mark.asyncio
async def test_persistent_connection_failure_has_bounded_retries(delivery):
    delivery.reply.side_effect = httpx.ConnectError("TLS handshake failed")
    with pytest.raises(httpx.ConnectError):
        await delivery.client.reply_card("anchor", {})
    assert delivery.reply.await_count == len(_TRANSIENT_RETRY_DELAYS) + 1
    assert [call.args[0] for call in delivery.sleep.await_args_list] == list(_TRANSIENT_RETRY_DELAYS)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [230001, 230002, 230020, 230429])
async def test_permanent_errors_and_im_throttling_are_not_retried(delivery, code):
    delivery.reply.return_value = response(code=code)
    with pytest.raises(FeishuAPIError) as error:
        await delivery.client.reply_card("anchor", {})
    assert error.value.code == code
    delivery.reply.assert_awaited_once()
    delivery.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_transient_api_error_is_retried(delivery):
    delivery.reply.side_effect = [response(code=2200), response()]
    assert await delivery.client.reply_card("anchor", {}) == "sent-message"
    assert delivery.reply.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry(delivery):
    delivery.reply.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await delivery.client.reply_card("anchor", {})
    delivery.reply.assert_awaited_once()
    delivery.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_message_id_is_not_retried(delivery):
    delivery.reply.return_value = response(message_id=None)
    with pytest.raises(FeishuAPIError, match="response missing message_id"):
        await delivery.client.reply_card("anchor", {})
    delivery.reply.assert_awaited_once()
    delivery.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_keeps_card_after_transient_initial_connection_failure(delivery):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True, "text_sizes": {"body": "normal"}},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    ctrl._initialized = True
    ctrl._client = delivery.client
    session = CardSession("retry-create", "chat", asyncio.get_running_loop())
    delivery.reply.side_effect = [httpx.ConnectError("proxy TLS"), response()]

    await ctrl._do_create_linear_card(session)

    assert session.state == STREAMING
    assert session.card_msg_id == "sent-message"
    assert session.card_id == "im:sent-message"
    assert session._card_ready.is_set()
    assert delivery.reply.await_count == 2
