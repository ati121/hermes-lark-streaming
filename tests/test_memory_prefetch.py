"""Automatic recall reaches card updates without becoming a model tool call."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.cardkit import _LOADING_ELEMENT_ID, _LOADING_HINT_ELEMENT_ID
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.feishu import FeishuClient
from hermes_lark_streaming.patching import (
    _maybe_wrap_callbacks,
    _msg_ctx,
    _session_contexts,
    _session_contexts_lock,
    _thread_local_ctx,
)
from hermes_lark_streaming.patching.memory import _maybe_wrap_memory_prefetch
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import CardPhase, TerminalReason

pytestmark = pytest.mark.asyncio

_RECALL = "📖 OpenViking · 自动检索记忆"
_WAITING = "等待上游模型响应"


class MemoryManager:
    """A bound per-provider wait, matching the Hermes manager entry point."""

    def __init__(self, retrieve):
        self.retrieve = retrieve

    def _prefetch_provider(self, provider, query, *, session_id=""):
        return self.retrieve(provider, query, session_id=session_id)


@pytest.fixture
def make_pipeline(monkeypatch):
    sessions = []

    def make(retrieve=None, *, ready=True):
        ctrl = StreamCardController()
        ctrl._cfg._raw = {
            "hermes_lark_streaming": {"enabled": True, "show_reasoning": False},
            "feishu": {"app_id": "app", "app_secret": "secret"},
        }
        ctrl._initialized = True
        ctrl._client = AsyncMock(spec=FeishuClient)
        ctrl._client.cardkit_create.return_value = "card-created"
        ctrl._client.reply_card_by_id.return_value = "card-message"
        ctrl._client.reply_card.return_value = "card-message"
        # Drive actual flushes explicitly, without racing scheduled duplicates.
        monkeypatch.setattr(ctrl, "_fire_and_forget", lambda coro, loop: coro.close())
        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl,
        )

        session = CardSession("message-1", "chat", asyncio.get_running_loop())
        session.linear = True
        session.unified_state = UnifiedLinearState()
        if ready:
            session.state = CardPhase.STREAMING
            session.card_id = "card-existing"
            session.card_msg_id = "card-message"
            session.existing_elements = {_LOADING_ELEMENT_ID, _LOADING_HINT_ELEMENT_ID}
            session.flush.set_card_message_ready(True)
        ctrl._sessions[session.message_id] = session
        sessions.append(session)
        _msg_ctx.set({"event_message_id": session.message_id})
        _thread_local_ctx.data = None

        manager = MemoryManager(retrieve or (lambda *args, **kwargs: "recalled context"))
        agent = SimpleNamespace(
            _memory_manager=manager,
            session_id="hermes-memory-test",
            tool_progress_callback=lambda *args, **kwargs: None,
        )
        _maybe_wrap_callbacks(agent)
        return SimpleNamespace(
            ctrl=ctrl, session=session, manager=manager, agent=agent,
            provider=SimpleNamespace(name="openviking"), client=ctrl._client,
        )

    yield make
    for session in sessions:
        session.flush.mark_completed()
    _msg_ctx.set(None)
    _thread_local_ctx.data = None
    with _session_contexts_lock:
        _session_contexts.pop("hermes-memory-test", None)


def hint_updates(client):
    return [
        action["params"]["partial_element"]["text"]
        for call in client.cardkit_batch_update.await_args_list
        for action in call.args[1]
        if action["action"] == "partial_update_element"
        and action["params"]["element_id"] == _LOADING_HINT_ELEMENT_ID
    ]


def element(card, element_id):
    return next(item for item in card["body"]["elements"] if item.get("element_id") == element_id)


async def test_worker_prefetch_updates_card_and_restores_waiting(make_pipeline):
    entered = threading.Event()
    release = threading.Event()
    result = "private recalled context"
    received = []

    def retrieve(provider, query, *, session_id):
        received.append((provider.name, query, session_id))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release retrieval")
        return result

    pipeline = make_pipeline(retrieve)
    task = asyncio.create_task(asyncio.to_thread(
        pipeline.manager._prefetch_provider, pipeline.provider, "question",
        session_id=pipeline.agent.session_id,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await pipeline.ctrl._do_unified_flush(pipeline.session)
        update = hint_updates(pipeline.client)[-1]
        assert "tag" not in update  # Preserve the existing lark_md tag on partial updates.
        assert update["i18n_content"]["zh_cn"] == _RECALL
        assert update["i18n_content"]["en_us"] == "📖 OpenViking · Recalling memory"
        assert pipeline.session.tool_use.build_display_steps() == []
        assert not pipeline.session.unified_state.panel_visible
    finally:
        release.set()
        assert await task == result

    await pipeline.ctrl._do_unified_flush(pipeline.session)
    assert hint_updates(pipeline.client)[-1]["i18n_content"]["zh_cn"] == _WAITING
    assert pipeline.session._response_phase == "waiting"
    assert not pipeline.session._memory_prefetch_requests
    assert received == [("openviking", "question", "hermes-memory-test")]
    assert result not in str(pipeline.client.cardkit_batch_update.await_args_list)


async def test_provider_failure_preserves_exception_and_clears_status(make_pipeline):
    failure = RuntimeError("provider unavailable")

    def retrieve(*args, **kwargs):
        assert pipeline.session._memory_prefetch_requests
        raise failure

    pipeline = make_pipeline(retrieve)
    with pytest.raises(RuntimeError) as raised:
        pipeline.manager._prefetch_provider(pipeline.provider, "question")

    assert raised.value is failure
    assert not pipeline.session._memory_prefetch_requests
    assert pipeline.ctrl._loading_hint_status(pipeline.session) == "loading_context"


async def test_status_ends_at_manager_timeout_before_provider_thread_returns(make_pipeline, monkeypatch):
    release = threading.Event()
    worker = None

    def bounded_wait(*args, **kwargs):
        nonlocal worker
        assert pipeline.session._memory_prefetch_requests
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        worker.join(0.01)
        return ""  # Hermes gives up while the provider daemon is still alive.

    pipeline = make_pipeline(bounded_wait)
    events = Mock(wraps=pipeline.ctrl.on_memory_prefetch_update)
    monkeypatch.setattr(pipeline.ctrl, "on_memory_prefetch_update", events)
    try:
        assert pipeline.manager._prefetch_provider(pipeline.provider, "question") == ""
        assert worker.is_alive()
        assert not pipeline.session._memory_prefetch_requests
        assert [call.kwargs["active"] for call in events.call_args_list] == [True, False]
    finally:
        release.set()
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)
    assert events.call_count == 2


@pytest.mark.parametrize("provider_name", ["builtin", "hindsight"])
async def test_other_providers_do_not_change_card_status(make_pipeline, monkeypatch, provider_name):
    original = Mock(return_value="other provider context")
    pipeline = make_pipeline(original)
    pipeline.provider.name = provider_name
    events = Mock(wraps=pipeline.ctrl.on_memory_prefetch_update)
    monkeypatch.setattr(pipeline.ctrl, "on_memory_prefetch_update", events)

    assert pipeline.manager._prefetch_provider(pipeline.provider, "question") == "other provider context"
    original.assert_called_once()
    events.assert_not_called()


async def test_cached_agent_uses_live_context_without_double_wrapping(make_pipeline, monkeypatch):
    pipeline = make_pipeline()
    original_wrapper = pipeline.manager._prefetch_provider
    _maybe_wrap_callbacks(pipeline.agent)
    assert pipeline.manager._prefetch_provider is original_wrapper

    _msg_ctx.set(None)
    with _session_contexts_lock:
        _session_contexts[pipeline.agent.session_id] = {"event_message_id": "message-2"}
    events = Mock()
    monkeypatch.setattr(pipeline.ctrl, "on_memory_prefetch_update", events)
    pipeline.manager._prefetch_provider(pipeline.provider, "question")

    assert events.call_count == 2
    start, finish = [call.kwargs for call in events.call_args_list]
    assert start["message_id"] == finish["message_id"] == "message-2"
    assert start["request_id"] is finish["request_id"]
    assert start["active"] is True and finish["active"] is False


async def test_completion_stays_with_original_message_if_context_changes(make_pipeline, monkeypatch):
    def retrieve(*args, **kwargs):
        _msg_ctx.set({"event_message_id": "replacement-message"})
        return "context"

    pipeline = make_pipeline(retrieve)
    events = Mock()
    monkeypatch.setattr(pipeline.ctrl, "on_memory_prefetch_update", events)
    pipeline.manager._prefetch_provider(pipeline.provider, "question")
    assert [call.kwargs["message_id"] for call in events.call_args_list] == ["message-1", "message-1"]


async def test_late_manager_is_wrapped_even_after_stream_callbacks(make_pipeline):
    pipeline = make_pipeline()
    replacement = MemoryManager(lambda *args, **kwargs: "replacement context")
    pipeline.agent._memory_manager = replacement
    _maybe_wrap_callbacks(pipeline.agent)

    assert getattr(replacement._prefetch_provider, "_hls_prefetch_wrapper", False)
    assert replacement._prefetch_provider(pipeline.provider, "question") == "replacement context"


async def test_no_live_context_leaves_retrieval_untouched(make_pipeline, monkeypatch):
    pipeline = make_pipeline()
    _msg_ctx.set(None)
    _thread_local_ctx.data = None
    events = Mock()
    monkeypatch.setattr(pipeline.ctrl, "on_memory_prefetch_update", events)

    assert pipeline.manager._prefetch_provider(pipeline.provider, "question") == "recalled context"
    events.assert_not_called()


async def test_display_hook_failure_does_not_lose_retrieved_context(make_pipeline, monkeypatch):
    pipeline = make_pipeline()
    monkeypatch.setattr(
        pipeline.ctrl, "on_memory_prefetch_update", Mock(side_effect=RuntimeError("card unavailable")),
    )
    assert pipeline.manager._prefetch_provider(pipeline.provider, "question") == "recalled context"


async def test_context_lookup_failure_does_not_skip_provider():
    manager = MemoryManager(lambda *args, **kwargs: "context")
    resolver = Mock(side_effect=RuntimeError("context unavailable"))
    _maybe_wrap_memory_prefetch(SimpleNamespace(_memory_manager=manager), resolver)
    assert manager._prefetch_provider(SimpleNamespace(name="openviking"), "question") == "context"


@pytest.mark.parametrize("interactive", [False, True])
async def test_initial_card_shows_active_recall_once(make_pipeline, interactive):
    pipeline = make_pipeline(ready=False)
    if interactive:
        pipeline.ctrl._cfg._raw["hermes_lark_streaming"]["text_sizes"] = {"body": "normal"}
    pipeline.ctrl.on_memory_prefetch_update(
        message_id=pipeline.session.message_id, request_id=object(), active=True,
    )
    assert pipeline.session._pending_flush

    await pipeline.ctrl._do_create_linear_card(pipeline.session)

    card = (
        pipeline.client.reply_card.await_args.args[1] if interactive
        else pipeline.client.cardkit_create.await_args.args[0]
    )
    assert element(card, _LOADING_HINT_ELEMENT_ID)["text"]["i18n_content"]["zh_cn"] == _RECALL
    assert element(card, _LOADING_ELEMENT_ID)["text"]["content"] == " "
    assert _WAITING not in json.dumps(card, ensure_ascii=False)


async def test_recall_finishing_before_card_creation_does_not_flash_stale_status(make_pipeline):
    pipeline = make_pipeline(ready=False)
    pipeline.manager._prefetch_provider(pipeline.provider, "question")
    await pipeline.ctrl._do_create_linear_card(pipeline.session)

    card = pipeline.client.cardkit_create.await_args.args[0]
    assert element(card, _LOADING_HINT_ELEMENT_ID)["text"]["i18n_content"]["zh_cn"] == _WAITING
    assert _RECALL not in json.dumps(card, ensure_ascii=False)


async def test_completion_during_slow_card_creation_is_flushed(make_pipeline, monkeypatch):
    pipeline = make_pipeline(ready=False)
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    scheduled = []

    async def create_card(card):
        create_started.set()
        await release_create.wait()
        return "created"

    pipeline.client.cardkit_create.side_effect = create_card
    monkeypatch.setattr(pipeline.ctrl, "_fire_and_forget", lambda coro, loop: scheduled.append(coro))
    request = object()
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=request, active=True)
    creation = asyncio.create_task(pipeline.ctrl._do_create_linear_card(pipeline.session))
    try:
        await asyncio.wait_for(create_started.wait(), 5)
        pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=request, active=False)
        assert pipeline.session._pending_flush
    finally:
        release_create.set()
        await creation
        await asyncio.gather(*scheduled)

    assert hint_updates(pipeline.client)[-1]["i18n_content"]["zh_cn"] == _WAITING
    assert not pipeline.session._first_flush_done


async def test_completion_while_start_patch_is_in_flight_reflushes_waiting(make_pipeline, monkeypatch):
    pipeline = make_pipeline()
    start_patch = asyncio.Event()
    release_patch = asyncio.Event()
    waiting_patch = asyncio.Event()
    scheduled = []

    def schedule(coro, loop):
        scheduled.append(loop.create_task(coro))

    async def patch_card(card_id, actions, **kwargs):
        for action in actions:
            if action["params"].get("element_id") != _LOADING_HINT_ELEMENT_ID:
                continue
            text = action["params"]["partial_element"]["text"]["i18n_content"]["zh_cn"]
            if text == _RECALL:
                start_patch.set()
                await release_patch.wait()
            elif text == _WAITING:
                waiting_patch.set()

    monkeypatch.setattr(pipeline.ctrl, "_fire_and_forget", schedule)
    pipeline.client.cardkit_batch_update.side_effect = patch_card
    request = object()
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=request, active=True)
    try:
        await asyncio.wait_for(start_patch.wait(), 5)
        pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=request, active=False)
        await scheduled[-1]  # The finish flush queues a repeat behind the in-flight patch.
        assert pipeline.session.flush._needs_reflush
        release_patch.set()
        await asyncio.wait_for(waiting_patch.wait(), 5)
        await pipeline.session.flush.wait_for_flush()
    finally:
        release_patch.set()
        await asyncio.gather(*scheduled)

    assert pipeline.session._loading_hint_state == "loading_context"


@pytest.mark.parametrize("later_event", ["model", "tool", "answer", "compression"])
async def test_later_activity_wins_over_recall_completion(make_pipeline, later_event):
    pipeline = make_pipeline()
    token = object()
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=token, active=True)
    if later_event == "model":
        pipeline.agent.tool_gen_callback("viking_search")
    elif later_event == "tool":
        pipeline.agent.tool_progress_callback("tool.started", "viking_search", "")
    elif later_event == "answer":
        pipeline.agent.stream_delta_callback("answer")
    else:
        pipeline.ctrl.on_compression_started(message_id="message-1")
    expected_phase = pipeline.session._response_phase
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=token, active=False)

    await pipeline.ctrl._do_unified_flush(pipeline.session)

    assert pipeline.session._response_phase == expected_phase != "waiting"
    assert not pipeline.session._memory_prefetch_requests
    assert _RECALL not in str(pipeline.client.cardkit_batch_update.await_args_list)
    if later_event == "tool":
        assert pipeline.session.tool_use.last_tool_names[1] == "OpenViking · 检索记忆"
        assert len(pipeline.session.tool_use.build_display_steps()) == 1


async def test_overlapping_requests_and_unknown_completions_keep_active_status(make_pipeline):
    pipeline = make_pipeline()
    first, second = object(), object()
    for token in (first, first, second):
        pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=token, active=True)
    for token in (first, object(), first):
        pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=token, active=False)
    await pipeline.ctrl._do_unified_flush(pipeline.session)
    assert hint_updates(pipeline.client)[-1]["i18n_content"]["zh_cn"] == _RECALL

    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=second, active=False)
    await pipeline.ctrl._do_unified_flush(pipeline.session)
    assert hint_updates(pipeline.client)[-1]["i18n_content"]["zh_cn"] == _WAITING


async def test_old_completion_cannot_clear_replacement_card(make_pipeline):
    pipeline = make_pipeline()
    old_request, new_request = object(), object()
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=old_request, active=True)
    replacement = CardSession("replacement", "chat", asyncio.get_running_loop())
    pipeline.ctrl._sessions["message-1"] = replacement
    pipeline.ctrl._sessions["replacement"] = replacement
    pipeline.ctrl.on_memory_prefetch_update(message_id="replacement", request_id=new_request, active=True)

    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=old_request, active=False)

    assert replacement._memory_prefetch_requests == {new_request}
    assert pipeline.ctrl._loading_hint_status(replacement) == "openviking_prefetch"


async def test_terminal_card_rejects_late_recall_events(make_pipeline):
    pipeline = make_pipeline()
    pipeline.session.state = CardPhase.COMPLETING
    pipeline.session.enter_terminal(reason=TerminalReason.NORMAL, source="test")
    pipeline.ctrl.on_memory_prefetch_update(message_id="message-1", request_id=object(), active=True)
    assert not pipeline.session._memory_prefetch_requests
    pipeline.client.cardkit_batch_update.assert_not_awaited()
