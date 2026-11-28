"""Native delivery must recognize cards owned by busy-turn continuations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming import patching as P
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import CREATION_FAILED, STREAMING
from hermes_lark_streaming.patching import adapter as adapter_patch
from hermes_lark_streaming.patching.callbacks import _maybe_wrap_callbacks
from hermes_lark_streaming.patching.gateway import (
    _wrap_handle_message_with_agent,
    _wrap_run_agent,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState


@pytest.fixture
def delivery(monkeypatch):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    original = CardSession("om_original", "chat1", asyncio.get_event_loop())
    original.state = STREAMING
    original.card_msg_id = "om_original_card"
    original.linear = True
    original.unified_state = UnifiedLinearState()
    ctrl._sess_put(original.message_id, original)

    # Exercise the real mapping and hooks without creating or sealing live cards.
    monkeypatch.setattr(ctrl, "_fire_and_forget", lambda coro, _loop: coro.close())
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl)
    monkeypatch.setattr(adapter_patch, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))
    receipt = SimpleNamespace(success=True, message_id="om_native")
    native_send = AsyncMock(return_value=receipt)

    class Adapter:
        send = adapter_patch._wrap_feishu_adapter_send(native_send)

    P._patched_feishu_classes.add(Adapter)
    context = {"message_id": "om_followup_1", "event_message_id": "om_followup_1", "card_sent": False}
    token = P._msg_ctx.set(context)
    previous_thread_context = getattr(P._thread_local_ctx, "data", None)
    try:
        yield SimpleNamespace(
            ctrl=ctrl, adapter=Adapter(), native_send=native_send,
            receipt=receipt, context=context,
        )
    finally:
        P._msg_ctx.reset(token)
        P._thread_local_ctx.data = previous_thread_context
        P._patched_feishu_classes.discard(Adapter)


def _interrupt(delivery, count=1, *, start=1):
    for index in range(start, start + count):
        message_id = f"om_followup_{index}"
        delivery.ctrl.on_busy_superseded(message_id=message_id, chat_id="chat1")
        delivery.ctrl.on_message_started(message_id=message_id, chat_id="chat1")
        assert delivery.ctrl._sess_get(message_id) is None
        tail = delivery.ctrl._continuation_session_for(message_id)
        assert tail is not None
        tail.state = STREAMING
        tail.card_msg_id = f"om_continuation_card_{index}"
    return tail


@pytest.mark.asyncio
@pytest.mark.parametrize("interruptions", [1, 3])
@pytest.mark.parametrize("content", [
    "Intermediate answer after interruption",
    "Working — iteration 17/150, waiting for non-streaming API response",
])
async def test_native_answer_and_heartbeat_are_suppressed_for_continuation(delivery, interruptions, content):
    tail = _interrupt(delivery, interruptions)
    assert delivery.ctrl._get_active_session("om_followup_1") is tail

    result = await delivery.adapter.send("chat1", content)

    assert result.success is True
    delivery.native_send.assert_not_awaited()
    assert delivery.context["card_sent"] is False


@pytest.mark.asyncio
async def test_direct_card_still_suppresses_native_answer(delivery):
    delivery.context["event_message_id"] = "om_original"

    result = await delivery.adapter.send("chat1", "Already on the direct card")

    assert result.success is True
    delivery.native_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["pending", "missing", "failed"])
async def test_undelivered_continuation_keeps_native_fallback(delivery, condition):
    tail = _interrupt(delivery)
    if condition == "pending":
        tail.card_msg_id = None
    elif condition == "missing":
        delivery.ctrl._sess_pop(tail.message_id)
    else:
        tail.state = CREATION_FAILED
    metadata = {"thread_id": "topic"}

    result = await delivery.adapter.send("chat1", "Fallback answer", reply_to="om_followup_1", metadata=metadata)

    assert result is delivery.receipt
    delivery.native_send.assert_awaited_once_with(
        delivery.adapter, "chat1", "Fallback answer", reply_to="om_followup_1", metadata=metadata,
    )
    assert delivery.context["card_sent"] is False


@pytest.mark.asyncio
async def test_memory_status_keeps_native_receipt_with_continuation(delivery):
    _interrupt(delivery, 3)
    token = P._gateway_status_ctx.set(True)
    try:
        result = await delivery.adapter.send("chat1", "Hindsight — recalled 3 memories")
    finally:
        P._gateway_status_ctx.reset(token)

    assert result is delivery.receipt
    delivery.native_send.assert_awaited_once()
    assert delivery.context["card_sent"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("already_streamed", [False, True])
async def test_interim_callback_dedup_does_not_leak_native_messages(delivery, already_streamed):
    tail = _interrupt(delivery, 3)
    sends = []
    text = "An intermediate answer already routed to the continuation"

    def interim(content, **_kwargs):
        sends.append(asyncio.create_task(delivery.adapter.send("chat1", content)))

    agent = SimpleNamespace(session_id="session1", interim_assistant_callback=interim)
    _maybe_wrap_callbacks(agent)
    agent.stream_delta_callback(text)
    agent.interim_assistant_callback(text, already_streamed=already_streamed)
    await asyncio.gather(*sends)

    assert sends
    assert tail.unified_state.answer_text == text
    delivery.native_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_card", [False, True])
async def test_gateway_final_reply_recognizes_busy_continuation(delivery, visible_card):
    tail = _interrupt(delivery, 3)
    if not visible_card:
        tail.card_msg_id = None
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat1")
    event = SimpleNamespace(message_id="om_followup_1", source=source)
    runner = SimpleNamespace(_reply_anchor_for_event=lambda event: event.message_id)

    async def run(_self, _event, _source):
        P._msg_ctx.get()["event_message_id"] = event.message_id
        return "Final reply"

    result = await _wrap_handle_message_with_agent(run)(runner, event, source)

    assert result == (None if visible_card else "Final reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["om_original", "om_followup_1"])
@pytest.mark.parametrize("condition", ["pending", "missing", "failed"])
async def test_second_interruption_preserves_fallback_after_earlier_suppression(delivery, message_id, condition):
    _interrupt(delivery)
    delivery.context.update(message_id=message_id, event_message_id=message_id)
    await delivery.adapter.send("chat1", "Working on the first continuation")
    delivery.native_send.assert_not_awaited()
    assert delivery.context["card_sent"] is False

    tail = _interrupt(delivery, start=2)
    if condition == "pending":
        tail.card_msg_id = None
    elif condition == "missing":
        delivery.ctrl._sess_pop(tail.message_id)
    else:
        tail.state = CREATION_FAILED

    result = await delivery.adapter.send("chat1", "The replacement card did not land")

    assert result is delivery.receipt
    delivery.native_send.assert_awaited_once()
    assert delivery.context["card_sent"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["om_original", "om_followup_1"])
@pytest.mark.parametrize("condition", ["missing", "failed"])
async def test_failed_complete_does_not_swallow_final_fallback(delivery, message_id, condition):
    _interrupt(delivery)
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat1")
    event = SimpleNamespace(message_id=message_id, source=source)
    runner = SimpleNamespace(_reply_anchor_for_event=lambda event: event.message_id)

    async def agent_run(*_args, **_kwargs):
        await delivery.adapter.send("chat1", "Working on the first continuation")
        tail = _interrupt(delivery, start=2)
        if condition == "missing":
            delivery.ctrl._sess_pop(tail.message_id)
        else:
            tail.state = CREATION_FAILED
        return {"final_response": "Final fallback body"}

    async def handle(_self, _event, _source):
        result = await _wrap_run_agent(agent_run)(
            runner, "message", "", [], source, "session1", event_message_id=message_id,
        )
        assert not result.get("already_sent", False)
        assert P._msg_ctx.get()["card_sent"] is False
        assert delivery.ctrl._continuation_map == {}
        return result["final_response"]

    result = await _wrap_handle_message_with_agent(handle)(runner, event, source)

    assert result == "Final fallback body"
    delivery.native_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_complete_still_suppresses_native_final(delivery):
    tail = _interrupt(delivery, 3)
    runner = SimpleNamespace()
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat1")

    async def agent_run(*_args, **_kwargs):
        return {"final_response": "Final answer on the continuation"}

    result = await _wrap_run_agent(agent_run)(
        runner, "message", "", [], source, "session1", event_message_id="om_followup_1",
    )
    await delivery.adapter.send("chat1", result["final_response"])

    assert result["already_sent"] is True
    assert delivery.context["card_sent"] is True
    assert tail.unified_state.answer_text == result["final_response"]
    delivery.native_send.assert_not_awaited()
