"""Approval feedback is persisted while the agent is still running."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.patching import _apply_feishu_adapter_patches
from hermes_lark_streaming.patching import adapter as patch_adapter
from hermes_lark_streaming.patching.adapter import _wrap_feishu_resolve_approval


@pytest.fixture
def approval_adapter(monkeypatch):
    settlement = Mock(return_value=1)
    monkeypatch.setitem(sys.modules, "tools.approval", SimpleNamespace(resolve_gateway_approval_card=settlement))
    translate = Mock(return_value="Approval expired")
    monkeypatch.setitem(sys.modules, "agent.i18n", SimpleNamespace(t=translate))

    class Adapter:
        def __init__(self):
            self._approval_state = {}
            self.config = SimpleNamespace(extra={"scope": "this-bot"})
            self._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(patch=Mock()))))
            self._run_blocking = AsyncMock(return_value=SimpleNamespace(success=lambda: True))
            self.send = AsyncMock()
            self.original_resolve = AsyncMock()
            self.queued = []
            self.allowed = True
            self.requester_allowed = True

        def _on_card_action_trigger(self, data):
            return self._handle_approval_card_action(event=data.event, action_value=data.event.action.value, loop=None)

        def _handle_approval_card_action(self, *, event, action_value, loop):
            ident = action_value["approval_id"]
            state = self._approval_state.get(ident)
            if not state or not self.allowed or event.context.open_chat_id != state["chat_id"]:
                return None
            if not self.requester_allowed:
                return SimpleNamespace(card=None, toast="Ask the requester")
            choice = "deny" if action_value["hermes_action"] == "deny" else "once"
            self.queued.append(self._resolve_approval(
                ident, choice, "Operator", open_id="ou_user", user_id="tenant", union_id="on_user", chat_id=state["chat_id"],
            ))
            return SimpleNamespace(
                card=self._build_resolved_approval_card(choice=choice, user_name="Operator", body=state["body"]),
                toast=None,
            )

        def _pop_validated_prompt_state(self, *, states, ident, chat_id, **kwargs):
            state = states.get(ident)
            if not state or not self.allowed or state["chat_id"] != chat_id:
                return None
            return states.pop(ident, None)

        @staticmethod
        def _build_resolved_approval_card(*, choice, user_name, body):
            return {"header": {"title": {"content": choice}}, "elements": [{"tag": "markdown", "content": body}]}

        async def _send_interactive_card(self, chat_id, card, *, state_map):
            return card

        async def _resolve_approval(self, *args, **kwargs):
            await self.original_resolve(*args, **kwargs)

        async def send(self, *args, **kwargs):
            pass

    adapter = Adapter()
    callback = adapter._on_card_action_trigger  # SDK captured before class patching
    assert _apply_feishu_adapter_patches(Adapter)
    adapter._approval_state[1] = {"chat_id": "chat", "message_id": "approval-card", "session_key": "session",
                                  "request_id": "request", "body": "Protected write request"}
    yield adapter, callback, settlement
    for coro in adapter.queued:
        coro.close()


def click(callback, *, action="approve_once", chat="chat"):
    return callback(SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value={"hermes_action": action, "approval_id": 1}),
        context=SimpleNamespace(open_chat_id=chat),
    )))


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "choice", "title"), [
    ("approve_once", "once", "✅ 已批准本次操作"),
    ("deny", "deny", "❌ 已拒绝"),
])
async def test_result_is_patched_before_agent_completes(approval_adapter, action, choice, title):
    adapter, callback, settlement = approval_adapter
    agent_started, agent_finished = asyncio.Event(), asyncio.Event()

    async def agent():
        await agent_started.wait()
        await agent_finished.wait()

    worker = asyncio.create_task(agent())
    settlement.side_effect = lambda *args, **kwargs: agent_started.set() or 1
    try:
        assert click(callback, action=action).card is None
        adapter._run_blocking.assert_not_awaited()
        await adapter.queued.pop()
        assert agent_started.is_set()
        assert not worker.done()
        adapter._run_blocking.assert_awaited_once()
        api_method, request = adapter._run_blocking.await_args.args
        assert api_method is adapter._client.im.v1.message.patch
        assert request.message_id == "approval-card"
        card = json.loads(request.request_body.content)
        assert card["header"]["title"]["content"] == title
        assert card["config"]["update_multi"] is True
        assert "Protected write request\n\n" in card["elements"][0]["content"]
        assert "操作人：Operator" in card["elements"][0]["content"]
        assert "button" not in request.request_body.content
        settlement.assert_called_once_with("session", choice, "request", actor_id="tenant", extra={"scope": "this-bot"})
        assert not adapter._approval_state
        adapter.original_resolve.assert_not_awaited()
        assert click(callback, action=action) is None
        assert not adapter.queued
        adapter._run_blocking.assert_awaited_once()
    finally:
        agent_started.set()
        agent_finished.set()
        await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve_once", "deny"])
async def test_expired_or_racing_answer_does_not_patch_success(approval_adapter, action):
    adapter, callback, settlement = approval_adapter
    settlement.return_value = 0
    assert click(callback, action=action).card is None
    await adapter.queued.pop()
    adapter._run_blocking.assert_not_awaited()
    if action == "approve_once":
        adapter.send.assert_awaited_once_with("chat", "Approval expired")
    else:
        adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["allowed", "requester_allowed", "chat"])
async def test_rejected_click_never_patches_or_settles(approval_adapter, restriction):
    adapter, callback, settlement = approval_adapter
    if restriction != "chat":
        setattr(adapter, restriction, False)
    response = click(callback, chat="wrong-chat" if restriction == "chat" else "chat")
    if restriction == "requester_allowed":
        assert response.card is None
        assert response.toast == "Ask the requester"
    else:
        assert response is None
    assert not adapter.queued
    adapter._run_blocking.assert_not_awaited()
    settlement.assert_not_called()
    assert 1 in adapter._approval_state


@pytest.mark.asyncio
async def test_authorization_revoked_before_async_resolution_does_not_patch(approval_adapter):
    adapter, callback, settlement = approval_adapter
    click(callback)
    adapter.allowed = False
    await adapter.queued.pop()
    adapter._run_blocking.assert_not_awaited()
    settlement.assert_not_called()
    assert 1 in adapter._approval_state


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["api", "network"])
async def test_display_failure_does_not_repeat_decision_or_block_agent(approval_adapter, failure):
    adapter, callback, settlement = approval_adapter
    adapter.delete_message = AsyncMock()
    if failure == "api":
        adapter._run_blocking.return_value = SimpleNamespace(success=lambda: False, code=230001)
    else:
        adapter._run_blocking.side_effect = OSError("connection lost")
    click(callback)
    await adapter.queued.pop()
    settlement.assert_called_once()
    assert not adapter._approval_state
    assert not adapter.queued
    assert not patch_adapter._approval_recall_tasks
    adapter.delete_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [230020, 230429])
async def test_frequency_limit_retries_only_the_card_update(approval_adapter, monkeypatch, code):
    adapter, callback, settlement = approval_adapter
    monkeypatch.setattr("hermes_lark_streaming.patching.adapter.asyncio.sleep", AsyncMock())
    adapter._run_blocking.side_effect = [
        SimpleNamespace(success=lambda: False, code=code),
        SimpleNamespace(success=lambda: True),
    ]
    click(callback)
    await adapter.queued.pop()
    settlement.assert_called_once()
    assert not adapter._approval_state
    assert adapter._run_blocking.await_count == 2
    first, second = adapter._run_blocking.await_args_list
    assert first.args[1] is second.args[1]
    assert json.loads(second.args[1].request_body.content)["header"]["title"]["content"] == "✅ 已批准本次操作"


@pytest.mark.asyncio
async def test_missing_message_id_does_not_send_another_card(approval_adapter):
    adapter, callback, settlement = approval_adapter
    adapter._approval_state[1].pop("message_id")
    assert click(callback).card is not None
    await adapter.queued.pop()
    settlement.assert_called_once()
    adapter._run_blocking.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_clicks_only_display_the_settled_decision(approval_adapter):
    adapter, callback, settlement = approval_adapter

    assert click(callback, action="approve_once").card is None
    assert click(callback, action="deny").card is None
    adapter._run_blocking.assert_not_awaited()

    await adapter.queued.pop(0)
    await adapter.queued.pop(0)

    settlement.assert_called_once_with("session", "once", "request", actor_id="tenant", extra={"scope": "this-bot"})
    adapter._run_blocking.assert_awaited_once()
    card = json.loads(adapter._run_blocking.await_args.args[1].request_body.content)
    assert card["header"]["title"]["content"] == "✅ 已批准本次操作"


@pytest.mark.asyncio
async def test_older_host_retains_native_resolver():
    original = AsyncMock(return_value="legacy")
    wrapped = _wrap_feishu_resolve_approval(original)
    adapter = SimpleNamespace()
    assert await wrapped(adapter, 1, "once", "Operator", open_id="ou_user") == "legacy"
    original.assert_awaited_once_with(adapter, 1, "once", "Operator", open_id="ou_user")


@pytest.mark.asyncio
async def test_host_without_patch_support_retains_its_inline_card(approval_adapter):
    adapter, callback, settlement = approval_adapter
    adapter._run_blocking = None

    assert click(callback).card["header"]["title"]["content"] == "✅ 已批准本次操作"
    await adapter.queued.pop()

    adapter.original_resolve.assert_awaited_once()
    settlement.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("action_ids", "labels"), [
    (["approve_once", "approve_session", "approve_always", "deny"],
     ["仅本次允许", "本会话允许", "始终允许", "拒绝"]),
    (["deny"], ["拒绝"]),
])
async def test_chinese_prompt_preserves_native_actions_and_body(approval_adapter, action_ids, labels):
    adapter, _, _ = approval_adapter
    body = "```text\nhermes_action: deny\n```\n\nFlagged reason: protected file"
    buttons = [
        {"tag": "button", "text": {"tag": "plain_text", "content": "English"}, "type": "default",
         "value": {"hermes_action": action, "approval_id": 1}}
        for action in action_ids
    ]
    card = {"header": {"title": {"content": "Approval required"}}, "elements": [
        {"tag": "markdown", "content": body}, {"tag": "action", "actions": buttons},
    ]}

    result = await adapter._send_interactive_card("chat", card, state_map=adapter._approval_state)
    assert result["header"]["title"]["content"] == "⚠️ 操作审批"
    assert result["config"]["update_multi"] is True
    assert result["elements"][0]["content"] == body
    localized = result["elements"][1]["actions"]
    assert [b["text"]["content"] for b in localized] == labels
    assert [b["value"] for b in localized] == [b["value"] for b in buttons]
    assert [b["type"] for b in localized] == [b["type"] for b in buttons]
    assert card["header"]["title"]["content"] == "Approval required"
    assert "config" not in card
    assert await adapter._send_interactive_card("chat", card, state_map={}) is card


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve_once", "deny"])
async def test_only_resolved_original_card_is_recalled_after_five_seconds(approval_adapter, monkeypatch, action):
    adapter, callback, settlement = approval_adapter
    adapter.delete_message = AsyncMock(return_value=True)
    patch_started, patch_acknowledged = asyncio.Event(), asyncio.Event()
    timer_started, timer_elapsed = asyncio.Event(), asyncio.Event()

    async def hold_patch(*args):
        patch_started.set()
        await patch_acknowledged.wait()
        return SimpleNamespace(success=lambda: True)

    async def wait_for_timer(seconds):
        assert seconds == 5
        timer_started.set()
        await timer_elapsed.wait()

    monkeypatch.setattr(patch_adapter.asyncio, "sleep", wait_for_timer)
    adapter._run_blocking.side_effect = hold_patch
    click(callback, action=action)
    resolve = asyncio.create_task(adapter.queued.pop())
    try:
        await patch_started.wait()
        assert not patch_adapter._approval_recall_tasks
    finally:
        patch_acknowledged.set()
        await resolve
        (recall_task,) = patch_adapter._approval_recall_tasks
    try:
        await timer_started.wait()
        adapter._run_blocking.assert_awaited_once()
        adapter.delete_message.assert_not_awaited()
        assert click(callback, action=action) is None
        settlement.assert_called_once()
    finally:
        timer_elapsed.set()
        await recall_task
    adapter.delete_message.assert_awaited_once_with("chat", "approval-card")
    assert recall_task not in patch_adapter._approval_recall_tasks


@pytest.mark.asyncio
async def test_expired_approval_does_not_schedule_recall(approval_adapter):
    adapter, callback, settlement = approval_adapter
    adapter.delete_message = AsyncMock(return_value=True)
    settlement.return_value = 0
    click(callback)
    await adapter.queued.pop()
    assert not patch_adapter._approval_recall_tasks
    adapter.delete_message.assert_not_awaited()
