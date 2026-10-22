"""Synthetic gateway wakes must use the normal card lifecycle without a reply id."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuClient
from hermes_lark_streaming.patching import (
    _msg_ctx,
    _session_contexts,
    _started_msg_ids,
    _thread_local_ctx,
)
from hermes_lark_streaming.patching.callbacks import _maybe_wrap_callbacks
from hermes_lark_streaming.patching.gateway import (
    _wrap_handle_message_with_agent,
    _wrap_run_agent,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("event_id", ['["completion","proc_test",123.0]', "om_user", None])
async def test_internal_wake_streams_and_seals_standalone_card(monkeypatch, interactive, event_id):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {
            "enabled": True,
            "text_sizes": {"body": "normal"} if interactive else {},
        },
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    ctrl._initialized = True
    ctrl._client = AsyncMock(spec=FeishuClient)
    ctrl._client.cardkit_create.return_value = "card"
    ctrl._client.send_card_to_chat.return_value = "om_card"
    ctrl._client.send_card_by_id_to_chat.return_value = "om_card"
    monkeypatch.setattr(ctrl, "_fire_and_forget", lambda coro, loop: coro.close())
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl)

    source = SimpleNamespace(chat_id="chat", platform=SimpleNamespace(value="feishu"))
    event = SimpleNamespace(internal=True, message_id=event_id)
    agent = SimpleNamespace(session_id="internal-session", stream_delta_callback=None)
    observed = {}

    async def run(self, *args, **kwargs):
        _maybe_wrap_callbacks(agent)
        agent.stream_delta_callback("background result")
        return {"final_response": "background result"}

    async def handle(self, event, source):
        ctx = _msg_ctx.get()
        observed["ctx"] = ctx
        session = ctrl._sess_get(ctx["message_id"])
        await ctrl._do_create_linear_card(session)
        result = await _wrap_run_agent(run)(
            self, "wake", "", [], source, agent.session_id, event_message_id=None,
        )
        assert result["already_sent"] is True
        assert session.unified_state.answer_text == "background result"
        await ctrl._do_linear_complete_with_fallback(session)
        assert session.state == "completed"
        return result["final_response"]

    runner = SimpleNamespace(_reply_anchor_for_event=lambda event: None)
    token = _msg_ctx.set(None)
    previous_local = getattr(_thread_local_ctx, "data", None)
    try:
        result = await _wrap_handle_message_with_agent(handle)(runner, event, source)
        ctx = observed["ctx"]
        assert result is None  # No second gateway reply containing raw reasoning.
        assert ctx["message_id"].startswith("hls-recovery-")
        assert ctx["event_message_id"] == ctx["message_id"]
        assert ctx["anchor_id"] is None
        assert ctx["card_sent"] is True
        assert _msg_ctx.get() is None
        assert ctx["message_id"] not in _started_msg_ids
        assert agent.session_id not in _session_contexts
        ctrl._client.reply_card.assert_not_awaited()
        ctrl._client.reply_card_by_id.assert_not_awaited()
        if interactive:
            ctrl._client.send_card_to_chat.assert_awaited_once()
        else:
            ctrl._client.send_card_by_id_to_chat.assert_awaited_once()
    finally:
        _msg_ctx.reset(token)
        _thread_local_ctx.data = previous_local
        ctrl._sess_clear()


@pytest.mark.asyncio
async def test_human_message_keeps_its_reply_anchor(monkeypatch):
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.on_message_started", Mock())
    observed = {}

    async def handle(self, event, source):
        observed.update(_msg_ctx.get())
        return "answer"

    runner = SimpleNamespace(_reply_anchor_for_event=lambda event: "om_quoted")
    event = SimpleNamespace(internal=False, message_id="om_inbound")
    source = SimpleNamespace(chat_id="chat")
    await _wrap_handle_message_with_agent(handle)(runner, event, source)

    assert observed["message_id"] == "om_inbound"
    assert observed["anchor_id"] == "om_quoted"
