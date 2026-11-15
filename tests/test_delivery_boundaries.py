"""Card delivery across final snapshots, internal IDs and background tasks."""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.patching import _msg_ctx, _patched_feishu_classes
from hermes_lark_streaming.patching.adapter import _wrap_feishu_adapter_send
from hermes_lark_streaming.patching.callbacks import _maybe_wrap_callbacks
from hermes_lark_streaming.patching.gateway import _wrap_run_background_task
from hermes_lark_streaming.state.linear import UnifiedLinearState


async def settle(ctrl):
    while ctrl._pending_tasks:
        await asyncio.gather(*tuple(ctrl._pending_tasks))


@pytest.fixture
async def delivery_card(tmp_path):
    ctrl = StreamCardController(tmp_path)
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "mock-app", "app_secret": "mock-secret"},
    }
    ctrl._initialized = True
    ctrl._client = AsyncMock(spec=FeishuClient)
    ctrl._client.reply_card.return_value = "om_fallback"
    ctrl._client.send_card_to_chat.return_value = "om_notification"
    ctrl._client.send_card_by_id_to_chat.return_value = "om_task_card"
    ctrl._client.cardkit_create.return_value = "entity_task_card"
    session = CardSession("om_question", "test-chat", asyncio.get_running_loop())
    session.linear = True
    session.interactive_mode = True
    session.unified_state = UnifiedLinearState()
    session.unified_state.on_answer_delta("final visible answer")
    session.card_msg_id = "om_original_card"
    session.card_id = "im:" + session.card_msg_id
    session.state = "streaming"
    session._card_ready.set()
    session.flush.set_card_message_ready(True)
    ctrl._sess_put(session.message_id, session)
    yield ctrl, session
    await settle(ctrl)
    ctrl._sess_clear()


@pytest.mark.asyncio
async def test_review_after_final_snapshot_is_a_separate_card(delivery_card):
    ctrl, session = delivery_card
    entered, release = asyncio.Event(), asyncio.Event()
    sender = Mock()

    async def patch_card(message_id, card, **kwargs):
        entered.set()
        await release.wait()

    ctrl._client.update_card.side_effect = patch_card
    assert ctrl.on_completed(message_id=session.message_id, answer="final visible answer")
    await entered.wait()
    try:
        assert ctrl.defer_background_review(
            message_id=session.message_id, text="late review notice", sender=sender,
        )
    finally:
        release.set()
    await settle(ctrl)
    assert session.state == "completed"
    assert "late review notice" not in json.dumps(ctrl._client.update_card.await_args.args[1])
    ctrl._client.update_card.assert_awaited_once()
    ctrl._client.send_card_to_chat.assert_awaited_once()
    chat_id, notice = ctrl._client.send_card_to_chat.await_args.args
    assert chat_id == session.chat_id
    assert "late review notice" in json.dumps(notice)
    assert "hls_loading" not in json.dumps(notice)
    sender.assert_not_called()
    ctrl._client.send_text_to_chat.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_before_sealing_stays_in_original_card(delivery_card, monkeypatch):
    ctrl, session = delivery_card
    sender = Mock()
    schedule = Mock()
    monkeypatch.setattr(ctrl, "_schedule_linear_flush", schedule)
    assert ctrl.defer_background_review(
        message_id=session.message_id, text="early review notice", sender=sender,
    )
    schedule.assert_called_once_with(session)
    assert ctrl.on_completed(message_id=session.message_id, answer="final visible answer")
    await settle(ctrl)
    assert "early review notice" in json.dumps(ctrl._client.update_card.await_args.args[1])
    ctrl._client.send_card_to_chat.assert_not_awaited()
    sender.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["completed", "creation_failed", "missing"])
async def test_review_after_session_finishes_keeps_card_transport(delivery_card, phase):
    ctrl, session = delivery_card
    session.state = phase
    session.unified_state = None
    if phase == "missing":
        ctrl._cleanup(session.message_id)
    assert ctrl.defer_background_review(
        message_id=session.message_id, chat_id=session.chat_id,
        text="post-turn notice", sender=Mock(),
    )
    await settle(ctrl)
    assert "post-turn notice" in json.dumps(ctrl._client.send_card_to_chat.await_args.args[1])
    ctrl._client.update_card.assert_not_awaited()
    ctrl._client.send_text_to_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_text_only_after_card_failure_and_without_reasoning(delivery_card):
    ctrl, _ = delivery_card
    ctrl._client.send_card_to_chat.side_effect = OSError("card unavailable")
    await ctrl._send_background_review_card("test-chat", "<think>private reasoning</think>visible notice")
    ctrl._client.send_card_to_chat.assert_awaited_once()
    ctrl._client.send_text_to_chat.assert_awaited_once_with("test-chat", "visible notice")
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", ["hls-recovery-internal-turn", "background-task-id"])
@pytest.mark.parametrize("card_fails", [False, True])
async def test_internal_seal_fallback_sends_to_chat(delivery_card, mid, card_fails):
    ctrl, session = delivery_card
    ctrl._sessions.clear()
    session.message_id = mid
    session.standalone = True
    ctrl._sess_put(mid, session)
    ctrl._client.update_card.side_effect = FeishuAPIError("seal rejected", code=300312)
    if card_fails:
        ctrl._client.send_card_to_chat.side_effect = OSError("static card unavailable")
    assert ctrl.on_completed(message_id=mid, answer="final visible answer")
    await settle(ctrl)
    ctrl._client.send_card_to_chat.assert_awaited_once()
    assert ctrl._client.send_card_to_chat.await_args.args[0] == session.chat_id
    assert "final visible answer" in json.dumps(ctrl._client.send_card_to_chat.await_args.args[1])
    ctrl._client.reply_card.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()
    if card_fails:
        ctrl._client.send_text_to_chat.assert_awaited_once_with(session.chat_id, "final visible answer")
    else:
        ctrl._client.send_text_to_chat.assert_not_awaited()


class Platform(Enum):
    FEISHU = "feishu"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_background_tasks_keep_cards_and_do_not_replace_shared_send(delivery_card, monkeypatch, interactive):
    ctrl, foreground = delivery_card
    if interactive:
        ctrl._cfg._raw["hermes_lark_streaming"]["text_sizes"] = {"body": "normal"}
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl)
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    original_send = AsyncMock()

    class Adapter:
        send = _wrap_feishu_adapter_send(original_send)

    adapter = Adapter()
    shared_send = Adapter.send
    _patched_feishu_classes.add(Adapter)
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id=foreground.chat_id)
    runner = SimpleNamespace(adapters={source.platform: adapter})
    all_started, release = asyncio.Event(), asyncio.Event()
    started = []

    async def host_run(self, prompt, source, task_id):
        session = ctrl._sess_get(task_id)
        await session._card_ready.wait()
        started.append(task_id)
        if len(started) == 2:
            all_started.set()
        await release.wait()
        agent = SimpleNamespace(session_id=task_id, stream_delta_callback=None)
        _maybe_wrap_callbacks(agent)
        agent.stream_delta_callback("task visible answer")
        await adapter.send(source.chat_id, "task visible answer")
        return {"final_response": "task visible answer"}

    context_token = _msg_ctx.set(None)
    try:
        wrapped = _wrap_run_background_task(host_run)
        tasks = [asyncio.create_task(wrapped(runner, "prompt", source, task_id)) for task_id in ("task-A", "task-B")]
        await all_started.wait()
        assert Adapter.send is shared_send
        assert "send" not in adapter.__dict__
        assert foreground.state == "streaming"
        release.set()
        results = await asyncio.gather(*tasks)
        await settle(ctrl)
        assert all(result["_hls_card_sent"] for result in results)
        assert _msg_ctx.get() is None
        assert foreground.state == "streaming"
        assert ctrl._sess_get("task-A").state == "completed"
        assert ctrl._sess_get("task-B").state == "completed"
        assert "send" not in adapter.__dict__
        original_send.assert_not_awaited()
        ctrl._client.reply_card.assert_not_awaited()
        ctrl._client.reply_card_by_id.assert_not_awaited()
        ctrl._client.reply_text.assert_not_awaited()
        if interactive:
            assert ctrl._client.send_card_to_chat.await_count == 2
        else:
            assert ctrl._client.send_card_by_id_to_chat.await_count == 2
    finally:
        release.set()
        _msg_ctx.reset(context_token)
        _patched_feishu_classes.discard(Adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gateway", "clarify"])
async def test_gateway_reactions_preserve_original_payload(delivery_card, monkeypatch, kind):
    from hermes_lark_streaming.cardkit import build_clarify_card
    from hermes_lark_streaming.patching.adapter import (
        _register_gateway_card,
        _unregister_gateway_card,
        _wrap_feishu_adapter_add_reaction,
        _wrap_feishu_adapter_delete_reaction,
    )

    ctrl, session = delivery_card
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    monkeypatch.setattr("hermes_lark_streaming.patching.adapter._get_config", lambda: ctrl._cfg)
    message_id = "om_status_" + kind
    if kind == "gateway":
        ctrl._client.send_card_to_chat.return_value = message_id
        await ctrl._do_gateway_deliver(session.chat_id, "original message", category="system")
        original = ctrl._client.send_card_to_chat.await_args.args[1]
        # The send wrapper registers metadata again after the controller sent it.
        _register_gateway_card(message_id, chat_id=session.chat_id, card_id=None, category="system")
    else:
        original = build_clarify_card(question="choose one", choices=["first", "second"], clarify_id="test-clarify")
        _register_gateway_card(message_id, chat_id=session.chat_id, card_id=None, category="clarify", card=original)
    original_send = AsyncMock()
    try:
        await _wrap_feishu_adapter_add_reaction(original_send)(object(), message_id, "👀")
        after_add = ctrl._client.update_card.await_args.args[1]
        assert after_add["body"]["elements"][1:] == original["body"]["elements"]
        assert after_add["config"] == original["config"]
        await _wrap_feishu_adapter_delete_reaction(original_send)(object(), message_id, "👀")
        assert ctrl._client.update_card.await_args.args[1] == original
        original_send.assert_not_awaited()
    finally:
        _unregister_gateway_card(message_id)


@pytest.mark.asyncio
async def test_gateway_status_uses_latest_edited_content(delivery_card):
    from hermes_lark_streaming.patching.adapter import _unregister_gateway_card

    ctrl, session = delivery_card
    ctrl._client.send_card_to_chat.return_value = "om_edited_status"
    message_id, _ = await ctrl._do_gateway_deliver(session.chat_id, "old content")
    try:
        assert await ctrl._do_gateway_card_update(
            chat_id=session.chat_id, card_msg_id=message_id, content="new content",
        )
        assert await ctrl._do_gateway_card_status(card_msg_id=message_id, status_label="Done", emoji="👍")
        payload = json.dumps(ctrl._client.update_card.await_args.args[1])
        assert "new content" in payload
        assert "old content" not in payload
    finally:
        _unregister_gateway_card(message_id)
