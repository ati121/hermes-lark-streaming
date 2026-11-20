"""IM writes distinguish confirmed delivery from lost acknowledgements."""

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
    FeishuClientConfig,
    MessageDeliveryUncertain,
)
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
from lark_oapi.api.im.v1.resource import message as sdk_message


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
    ("send_text_to_chat", ("chat", "visible fallback"), "create"),
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
async def test_standalone_text_uses_chat_recipient_and_text_payload(delivery):
    await delivery.client.send_text_to_chat("target-chat", "visible answer")
    request = delivery.create.await_args.args[0]
    assert request.receive_id_type == "chat_id"
    assert request.body.receive_id == "target-chat"
    assert request.body.msg_type == "text"
    assert json.loads(request.body.content) == {"text": "visible answer"}
    delivery.reply.assert_not_awaited()


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
async def test_im_read_timeout_and_unacknowledged_retry_expose_uncertain_delivery(delivery):
    delivery.reply.side_effect = [httpx.ReadTimeout("response lost"), response(code=230001)]

    with pytest.raises(MessageDeliveryUncertain) as error:
        await delivery.client.reply_card("anchor", {})

    assert error.value.code == 230001
    first, second = (call.args[0] for call in delivery.reply.await_args_list)
    assert first.body.uuid and first.body.uuid == second.body.uuid


@pytest.mark.asyncio
async def test_im_response_body_cannot_be_decoded(delivery):
    delivery.create.side_effect = json.JSONDecodeError("Invalid response", "", 0)
    with pytest.raises(MessageDeliveryUncertain):
        await delivery.client.send_card_to_chat("target-chat", {})


@pytest.mark.asyncio
async def test_im_gateway_error_without_api_code_is_uncertain(delivery):
    gateway_error = response(code=None)
    gateway_error.raw = SimpleNamespace(status_code=502)
    delivery.create.return_value = gateway_error
    with pytest.raises(MessageDeliveryUncertain):
        await delivery.client.send_card_to_chat("target-chat", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_code", [0, 230020, None], ids=["confirmed", "rejected", "superseded"])
async def test_im_patch_after_lost_acknowledgement(monkeypatch, retry_code):
    card = {"schema": "2.0", "body": {"elements": []}}
    attempts = []

    async def wire(request):
        assert request.method == "PATCH"
        assert request.url.path == "/open-apis/im/v1/messages/om_test"
        assert json.loads(json.loads(request.content)["content"]) == card
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            400 if retry_code else 200,
            json={"code": retry_code, "msg": "rejected" if retry_code else "ok", "error": {"log_id": "mock-feishu-log"}},
        )

    http_client = httpx.AsyncClient
    transport = httpx.MockTransport(wire)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: http_client(transport=transport))
    monkeypatch.setattr(sdk_message, "verify", lambda *_: None)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = FeishuClient(FeishuClientConfig("test-app", "test-secret"))

    def should_update():
        return retry_code is not None or not attempts

    if retry_code == 0:
        await client.update_card("om_test", card, should_update=should_update)
    else:
        with pytest.raises(MessageDeliveryUncertain) as error:
            await client.update_card("om_test", card, should_update=should_update)
        assert error.value.code == (retry_code or 0)
        assert error.value.log_id == ("mock-feishu-log" if retry_code else "")
    assert len(attempts) == (1 if retry_code is None else 2)


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
    with pytest.raises(MessageDeliveryUncertain, match="response missing message_id"):
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
