"""Incoming-image preprocessing must report progress before the main agent runs."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hermes_lark_streaming import patching as P
from hermes_lark_streaming.cardkit import _LOADING_ELEMENT_ID, _LOADING_HINT_ELEMENT_ID
from hermes_lark_streaming.controller import CardSession
from hermes_lark_streaming.controller.mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    STREAMING,
)
from hermes_lark_streaming.patching.gateway import _wrap_enrich_message_with_vision
from hermes_lark_streaming.state.linear import UnifiedLinearState

from tests.test_controller import _all_batch_actions, _setup_ctrl


def _session(ctrl, message_id="image-turn"):
    session = CardSession(message_id, "image-chat", asyncio.get_running_loop())
    ctrl._sessions[message_id] = session
    return session


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("late_card", [False, True])
@pytest.mark.asyncio
async def test_image_only_input_shows_progress_while_vision_is_running(interactive, late_card):
    ctrl = _setup_ctrl(linear=True)
    if interactive:
        ctrl._cfg._raw["hermes_lark_streaming"]["text_sizes"] = {
            "body": {"default": "normal", "pc": "normal", "mobile": "large"},
        }
    session = _session(ctrl)
    entered, release, shown, resumed = (asyncio.Event() for _ in range(4))

    def observe(*args, **kwargs):
        payload = json.dumps(args, ensure_ascii=False)
        if "正在识别图片..." in payload:
            assert "等待上游模型响应" not in payload
            shown.set()
        if shown.is_set() and "等待上游模型响应" in payload:
            resumed.set()

    async def create(*args, **kwargs):
        observe(*args, **kwargs)
        return "image-card"

    ctrl._client.cardkit_create = AsyncMock(side_effect=create)
    ctrl._client.reply_card = AsyncMock(side_effect=create)
    ctrl._client.cardkit_batch_update = AsyncMock(side_effect=observe)
    ctrl._client.update_card = AsyncMock(side_effect=observe)
    if not late_card:
        await ctrl._do_create_linear_card(session)

    async def enrich(self, user_text, image_paths):
        assert user_text == ""
        assert image_paths == ["first.png", "second.png"]
        entered.set()
        await release.wait()
        return "vision description"

    # At this stage START has a message_id; _run_agent has not supplied an eid.
    token = P._msg_ctx.set({"message_id": session.message_id, "event_message_id": ""})
    with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
        task = asyncio.create_task(
            _wrap_enrich_message_with_vision(enrich)(None, "", ["first.png", "second.png"])
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if late_card:
                assert session._pending_flush
                await ctrl._do_create_linear_card(session)
            await asyncio.wait_for(shown.wait(), 1)
            assert not task.done(), "progress must arrive before vision returns"
            assert session._response_phase == "image_analysis"
            assert session.unified_state.answer_text == ""
            assert not session.unified_state.panel_visible
            assert session._first_answer_time == 0

            release.set()
            assert await asyncio.wait_for(task, 1) == "vision description"
            await asyncio.wait_for(resumed.wait(), 1)
            assert session._response_phase == "waiting"
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            P._msg_ctx.reset(token)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.asyncio
async def test_vision_failure_and_cancellation_restore_status(cancelled):
    ctrl = _setup_ctrl(linear=True)
    session = _session(ctrl)
    entered = asyncio.Event()

    async def enrich(*args, **kwargs):
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise RuntimeError("vision failed")

    token = P._msg_ctx.set({"message_id": session.message_id})
    try:
        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            task = asyncio.create_task(_wrap_enrich_message_with_vision(enrich)(None, "", ["image.png"]))
            await asyncio.wait_for(entered.wait(), 1)
            if cancelled:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
                await task
            assert session._response_phase == "waiting"
    finally:
        P._msg_ctx.reset(token)


@pytest.mark.parametrize("activity, expected", [
    ("model", "thinking"), ("answer", "answer"), ("reasoning", "thinking"), ("tool", "tool"),
])
@pytest.mark.asyncio
async def test_late_image_completion_does_not_override_model_or_tool(activity, expected):
    ctrl = _setup_ctrl(linear=True)
    session = _session(ctrl)
    session.linear = True
    session.unified_state = UnifiedLinearState()
    ctrl.on_image_analysis_started(message_id=session.message_id)
    if activity == "model":
        await asyncio.to_thread(ctrl.on_model_activity, message_id=session.message_id)
    elif activity == "answer":
        ctrl.on_answer(message_id=session.message_id, text="answer")
    elif activity == "reasoning":
        ctrl.on_reasoning(message_id=session.message_id, text="reasoning")
    else:
        ctrl.on_tool_update(message_id=session.message_id, tool_name="terminal", status="running")
    ctrl.on_image_analysis_completed(message_id=session.message_id)
    ctrl.on_image_analysis_started(message_id=session.message_id)
    assert session._response_phase == expected


@pytest.mark.parametrize("image_finishes_first", [False, True])
@pytest.mark.asyncio
async def test_image_and_compression_completion_do_not_restore_stale_status(image_finishes_first):
    ctrl = _setup_ctrl(linear=True)
    session = _session(ctrl)
    ctrl.on_image_analysis_started(message_id=session.message_id)
    ctrl.on_compression_started(message_id=session.message_id)
    if image_finishes_first:
        ctrl.on_image_analysis_completed(message_id=session.message_id)
        assert session._response_phase == "compression"
        ctrl.on_compression_completed(message_id=session.message_id)
    else:
        ctrl.on_compression_completed(message_id=session.message_id)
        assert session._response_phase == "image_analysis"
        ctrl.on_image_analysis_completed(message_id=session.message_id)
    assert session._response_phase == "waiting"


@pytest.mark.parametrize("state", [COMPLETING, COMPLETED, ABORTED])
@pytest.mark.asyncio
async def test_image_callbacks_leave_sealed_sessions_untouched(state):
    ctrl = _setup_ctrl(linear=True)
    session = _session(ctrl)
    ctrl.on_image_analysis_started(message_id=session.message_id)
    session.state = state
    with patch.object(ctrl, "_schedule_linear_flush") as schedule:
        ctrl.on_image_analysis_completed(message_id=session.message_id)
        ctrl.on_image_analysis_started(message_id=session.message_id)
    assert session._response_phase == "image_analysis"
    schedule.assert_not_called()


@pytest.mark.parametrize("hint_present", [False, True])
@pytest.mark.asyncio
async def test_image_status_uses_spinner_or_retained_fallback_hint(hint_present):
    ctrl = _setup_ctrl(linear=True)
    session = _session(ctrl)
    ctrl.on_image_analysis_started(message_id=session.message_id)
    session.state = STREAMING
    session.linear = True
    session.unified_state = UnifiedLinearState()
    session.card_id = "image-card"
    session.existing_elements = {_LOADING_ELEMENT_ID}
    if hint_present:
        session.existing_elements.add(_LOADING_HINT_ELEMENT_ID)
        session._loading_label_supported = False
    await ctrl._do_unified_flush(session)
    target = _LOADING_HINT_ELEMENT_ID if hint_present else _LOADING_ELEMENT_ID
    update = next(
        action for action in _all_batch_actions(ctrl)
        if action["action"] == "partial_update_element" and action["params"]["element_id"] == target
    )
    assert update["params"]["partial_element"]["text"]["i18n_content"]["zh_cn"].endswith("正在识别图片...")
    assert all(action["action"] != "delete_elements" for action in _all_batch_actions(ctrl))


@pytest.mark.parametrize("context, images", [(None, ["image.png"]), ({"message_id": "turn"}, [])])
@pytest.mark.asyncio
async def test_no_image_or_no_live_context_passes_through(context, images, monkeypatch):
    token = P._msg_ctx.set(context)
    monkeypatch.setattr(P._thread_local_ctx, "data", {"message_id": "stale-worker"})
    original = AsyncMock(return_value="unchanged")
    try:
        with (
            patch("hermes_lark_streaming.patching.hooks.on_image_analysis_started") as start,
            patch("hermes_lark_streaming.patching.hooks.on_image_analysis_completed") as end,
        ):
            result = await _wrap_enrich_message_with_vision(original)(None, "text", images, option=True)
        assert result == "unchanged"
        original.assert_awaited_once_with(None, "text", images, option=True)
        start.assert_not_called()
        end.assert_not_called()
    finally:
        P._msg_ctx.reset(token)


@pytest.mark.asyncio
async def test_gateway_installs_inherited_vision_hook_once_and_keeps_original_turn_id(monkeypatch):
    async def enrich(self, user_text, image_paths):
        P._msg_ctx.set({"message_id": "next-turn"})
        return "description"

    class InboundMixin:
        _enrich_message_with_vision = enrich

    class Gateway(InboundMixin):
        pass

    monkeypatch.setattr(P, "_gw_runner_patched", False)
    compat = SimpleNamespace(gateway_runner_class=Gateway)
    assert P._apply_gateway_runner_patches(compat)
    installed = Gateway._enrich_message_with_vision
    assert P._apply_gateway_runner_patches(compat)
    assert Gateway._enrich_message_with_vision is installed
    assert _wrap_enrich_message_with_vision(installed) is installed
    token = P._msg_ctx.set({"message_id": "original-turn"})
    try:
        with (
            patch("hermes_lark_streaming.patching.hooks.on_image_analysis_started") as start,
            patch("hermes_lark_streaming.patching.hooks.on_image_analysis_completed") as end,
        ):
            assert await Gateway()._enrich_message_with_vision("", ["image.png"]) == "description"
        start.assert_called_once_with(message_id="original-turn")
        end.assert_called_once_with(message_id="original-turn")
    finally:
        P._msg_ctx.reset(token)
