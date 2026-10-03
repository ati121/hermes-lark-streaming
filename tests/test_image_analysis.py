"""Gateway vision preprocessing is visible before main-model callbacks exist."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.cardkit import _LOADING_ELEMENT_ID, _LOADING_HINT_ELEMENT_ID
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.feishu import FeishuClient
from hermes_lark_streaming.patching import (
    _msg_ctx,
    _thread_local_ctx,
    _wrap_method_once,
)
from hermes_lark_streaming.patching.gateway import (
    _wrap_enrich_inbound_images,
    _wrap_enrich_message_with_vision,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import CardPhase

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pipeline(monkeypatch):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    ctrl._initialized = True
    ctrl._client = AsyncMock(spec=FeishuClient)
    ctrl._client.cardkit_create.return_value = "card"
    ctrl._client.reply_card_by_id.return_value = "reply"
    ctrl._client.reply_card.return_value = "reply"
    monkeypatch.setattr(ctrl, "_fire_and_forget", lambda coro, loop: coro.close())
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl)
    session = CardSession("image-message", "chat", asyncio.get_running_loop())
    session.linear = True
    session.unified_state = UnifiedLinearState()
    ctrl._sessions[session.message_id] = session
    token = _msg_ctx.set({"message_id": session.message_id})
    yield SimpleNamespace(ctrl=ctrl, session=session, client=ctrl._client)
    session.flush.mark_completed()
    _msg_ctx.reset(token)


def ready(p, interactive):
    p.session.state = CardPhase.STREAMING
    p.session.card_id = "card"
    p.session.card_msg_id = "reply"
    p.session.interactive_mode = interactive
    p.session.existing_elements = {_LOADING_ELEMENT_ID, _LOADING_HINT_ELEMENT_ID}
    p.session.flush.set_card_message_ready(True)


@pytest.mark.parametrize("interactive", [False, True])
async def test_preprocessing_start_finish_and_tool_takeover(pipeline, interactive):
    p = pipeline
    ready(p, interactive)

    async def original(self, user_text, image_paths):
        await p.ctrl._do_unified_flush(p.session)
        calls = str(p.client.mock_calls)
        assert "图像分析中" in calls
        assert p.session.tool_use.build_display_steps() == []
        assert user_text == "" and image_paths == ["photo.jpg"]
        return "image description"

    wrapped = _wrap_enrich_message_with_vision(original)
    assert await wrapped(None, "", ["photo.jpg"]) == "image description"
    assert not p.session._image_analysis_requests
    p.client.reset_mock()
    await p.ctrl._do_unified_flush(p.session)
    assert "等待上游模型响应" in str(p.client.mock_calls)
    p.ctrl.on_tool_update(message_id=p.session.message_id, tool_name="vision_analyze", status="started")
    await p.ctrl._do_unified_flush(p.session)
    assert "图像分析" in str(p.client.mock_calls)


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("finish_during_create", [False, True])
async def test_preprocessing_before_card_creation(pipeline, interactive, finish_during_create):
    p = pipeline
    if interactive:
        p.ctrl._cfg._raw["hermes_lark_streaming"]["text_sizes"] = {"body": "normal"}
    request = object()
    update = p.ctrl.on_image_analysis_update
    update(message_id=p.session.message_id, request_id=request, active=True)
    assert p.session._pending_flush

    async def send(*args, **kwargs):
        assert "图像分析中" in str(args)
        if finish_during_create:
            update(message_id=p.session.message_id, request_id=request, active=False)
        return "reply"

    if interactive:
        p.client.reply_card.side_effect = send
    else:
        p.client.cardkit_create.side_effect = send
    await p.ctrl._do_create_linear_card(p.session)
    await p.ctrl._do_unified_flush(p.session)
    expected = "loading_context" if finish_during_create else "image_analyzing"
    assert p.ctrl._loading_hint_status(p.session) == expected
    if finish_during_create:
        assert "等待上游模型响应" in str(p.client.mock_calls)


@pytest.mark.parametrize("failure", [RuntimeError("vision failed"), asyncio.CancelledError()])
async def test_failure_or_cancel_preserves_exception_and_clears_status(pipeline, failure):
    p = pipeline
    async def original(*args):
        assert p.session._image_analysis_requests
        raise failure
    with pytest.raises(type(failure)) as raised:
        await _wrap_enrich_message_with_vision(original)(None, "question", ["image"])
    assert raised.value is failure
    assert not p.session._image_analysis_requests


@pytest.mark.parametrize("phase", ["thinking", "answer", "tool", "compression"])
async def test_late_finish_does_not_reset_newer_activity(pipeline, phase):
    p = pipeline
    request = object()
    p.ctrl.on_image_analysis_update(message_id=p.session.message_id, request_id=request, active=True)
    p.session._response_phase = phase
    p.ctrl.on_image_analysis_update(message_id=p.session.message_id, request_id=request, active=False)
    assert p.session._response_phase == phase
    assert not p.session._image_analysis_requests


async def test_old_completion_cannot_clear_replacement_request(pipeline):
    p = pipeline
    old, new = object(), object()
    p.ctrl.on_image_analysis_update(message_id=p.session.message_id, request_id=old, active=True)
    replacement = CardSession("replacement", "chat", asyncio.get_running_loop())
    p.ctrl._sessions[p.session.message_id] = replacement
    p.ctrl._sessions[replacement.message_id] = replacement
    p.ctrl.on_image_analysis_update(message_id="replacement", request_id=new, active=True)
    p.ctrl.on_image_analysis_update(message_id=p.session.message_id, request_id=old, active=False)
    assert replacement._image_analysis_requests == {new}


async def test_wrapper_is_idempotent_and_ignores_stale_thread_context(pipeline, monkeypatch):
    async def original(*args):
        return "unchanged"
    class Gateway:
        _enrich_message_with_vision = original
    assert _wrap_method_once(Gateway, "_enrich_message_with_vision", _wrap_enrich_message_with_vision) == "patched"
    assert _wrap_method_once(Gateway, "_enrich_message_with_vision", _wrap_enrich_message_with_vision) == "adopted"
    _msg_ctx.set(None)
    monkeypatch.setattr(_thread_local_ctx, "data", {"message_id": pipeline.session.message_id}, raising=False)
    assert await Gateway()._enrich_message_with_vision("text", ["image"]) == "unchanged"
    assert not pipeline.session._image_analysis_requests


async def test_terminal_session_ignores_new_preprocessing(pipeline):
    pipeline.session.state = CardPhase.COMPLETED
    pipeline.ctrl.on_image_analysis_update(message_id=pipeline.session.message_id, request_id=object(), active=True)
    assert not pipeline.session._image_analysis_requests


async def test_overlapping_requests_and_pinned_turn_context(pipeline):
    p = pipeline
    update = p.ctrl.on_image_analysis_update
    newer = object()

    async def original(*args):
        update(message_id=p.session.message_id, request_id=newer, active=True)
        _msg_ctx.set({"message_id": "another-turn"})
        return "description"

    assert await _wrap_enrich_message_with_vision(original)(None, "", ["image"]) == "description"
    assert p.session._image_analysis_requests == {newer}
    update(message_id=p.session.message_id, request_id=newer, active=False)
    assert p.ctrl._loading_hint_status(p.session) == "loading_context"


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("already_created", [False, True])
async def test_native_images_are_visible_until_model_activity(pipeline, interactive, already_created):
    p = pipeline
    if already_created:
        ready(p, interactive)
    elif interactive:
        p.ctrl._cfg._raw["hermes_lark_streaming"]["text_sizes"] = {"body": "normal"}
    persistent = SimpleNamespace(native_image_paths=[])
    gateway = SimpleNamespace(_session_state=lambda key: SimpleNamespace(persistent=persistent))

    async def native(self, source, session_key, message_text, image_paths):
        persistent.native_image_paths = list(image_paths)
        return message_text

    assert await _wrap_enrich_inbound_images(native)(gateway, None, "session", "question", ["image"]) == "question"
    if not already_created:
        await p.ctrl._do_create_linear_card(p.session)
    await p.ctrl._do_unified_flush(p.session)
    assert "图像分析中" in str(p.client.mock_calls)
    assert p.session._has_native_image_input
    assert not p.session._image_analysis_requests
    recall = object()
    p.ctrl.on_memory_prefetch_update(message_id=p.session.message_id, request_id=recall, active=True)
    assert p.ctrl._loading_hint_status(p.session) == "openviking_recall"
    p.ctrl.on_memory_prefetch_update(message_id=p.session.message_id, request_id=recall, active=False)
    assert p.ctrl._loading_hint_status(p.session) == "image_processing"
    p.client.reset_mock()
    p.ctrl.on_answer(message_id=p.session.message_id, text="I see the image")
    await p.ctrl._do_unified_flush(p.session)
    assert "图像分析中" not in str(p.client.mock_calls)
    assert "I see the image" in str(p.client.mock_calls)


async def test_text_route_does_not_reuse_old_native_attachments(pipeline):
    persistent = SimpleNamespace(native_image_paths=["image"])
    gateway = SimpleNamespace(_session_state=lambda key: SimpleNamespace(persistent=persistent))
    async def text(self, source, session_key, message_text, image_paths):
        return "description"
    assert await _wrap_enrich_inbound_images(text)(gateway, None, "session", "", ["image"]) == "description"
    assert not pipeline.session._has_native_image_input
