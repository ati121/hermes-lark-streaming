"""Bound IM streaming traffic to changed frames and the current live card."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import COMPLETED, COMPLETING, STREAMING
from hermes_lark_streaming.feishu import (
    IM_FREQUENCY_LIMIT,
    FeishuAPIError,
    FeishuClient,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState


@pytest.fixture
async def live_card(request):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    ctrl._initialized = True
    ctrl._client = AsyncMock(spec=FeishuClient)
    session = CardSession(request.node.name, "test-chat", asyncio.get_running_loop())
    session.linear = True
    session.interactive_mode = True
    session.unified_state = UnifiedLinearState()
    session.unified_state.on_answer_delta("first answer")
    session.text_sizes = {"body": {"mobile": "large"}}
    session.card_msg_id = "card-" + request.node.name
    session.card_id = "im:" + session.card_msg_id
    session.state = STREAMING
    session._card_ready.set()
    session.flush.set_card_message_ready(True)
    ctrl._sess_put(session.message_id, session)
    yield ctrl, session
    ctrl._sess_clear()


async def test_unchanged_render_is_deduplicated_after_ack(live_card):
    ctrl, session = live_card
    state = session.unified_state
    await ctrl._do_unified_flush(session)
    acknowledged = session._interactive_last_ack
    assert acknowledged is not None

    # A tool/reasoning event can dirty the source without changing the frame.
    state.answer_dirty = state.panel_dirty = state.tool_steps_dirty = True
    await ctrl._do_unified_flush(session)
    assert ctrl._client.update_card.await_count == 1
    assert not state.has_dirty
    assert session._interactive_last_ack == acknowledged
    assert session._interactive_update_attempt == 1


async def test_changed_render_sends_new_frame(live_card):
    ctrl, session = live_card
    await ctrl._do_unified_flush(session)
    acknowledged = session._interactive_last_ack
    session.unified_state.on_answer_delta(" plus changed content")
    await ctrl._do_unified_flush(session)
    assert ctrl._client.update_card.await_count == 2
    assert session._interactive_last_ack != acknowledged
    assert "plus changed content" in session._interactive_last_ack[2]


async def test_hidden_reasoning_deltas_do_not_repeat_the_same_card(live_card):
    ctrl, session = live_card
    ctrl._cfg._reload_cached = lambda: {
        "display": {"platforms": {"feishu": {"show_reasoning": False}}},
    }
    session._response_phase = "thinking"
    await ctrl._do_unified_flush(session)
    acknowledged = session._interactive_last_ack

    for index in range(50):
        session.unified_state.on_reasoning_delta(f"hidden reasoning chunk {index} ")
        assert session.unified_state.panel_dirty
        await ctrl._do_unified_flush(session)

    assert ctrl._client.update_card.await_count == 1
    assert session._interactive_last_ack == acknowledged
    assert "hidden reasoning" not in acknowledged[2]
    assert not session.unified_state.has_dirty


async def test_failed_frame_does_not_suppress_retry(live_card):
    ctrl, session = live_card
    ctrl._client.update_card.side_effect = [OSError("offline"), None]
    with patch.object(ctrl, "_schedule_linear_flush"):
        await ctrl._do_unified_flush(session)
    assert session._interactive_last_ack is None
    assert session.unified_state.answer_dirty
    await ctrl._do_unified_flush(session)
    assert ctrl._client.update_card.await_count == 2
    assert session._interactive_last_ack is not None
    assert not session.unified_state.has_dirty


@pytest.mark.parametrize(
    "invalidated_by", ["completing", "completed", "card", "epoch", "session", "disposed"],
)
async def test_waiting_stream_patch_cannot_replace_newer_card(live_card, invalidated_by):
    ctrl, session = live_card
    queued = asyncio.Event()
    release = asyncio.Event()
    delivered = []

    async def send(message_id, card, *, should_update):
        queued.set()
        await release.wait()
        if should_update():
            delivered.append((message_id, card))

    ctrl._client.update_card.side_effect = send
    task = asyncio.create_task(ctrl._do_unified_flush(session))
    await queued.wait()
    if invalidated_by == "completing":
        session.state = COMPLETING
    elif invalidated_by == "completed":
        session.state = COMPLETED
    elif invalidated_by == "card":
        session.card_msg_id = "replacement-card"
    elif invalidated_by == "epoch":
        session.create_epoch += 1
    elif invalidated_by == "session":
        replacement = CardSession(session.message_id, session.chat_id, asyncio.get_running_loop())
        ctrl._sess_put(session.message_id, replacement)
    else:
        session._disposed = True
    release.set()
    await task
    assert delivered == []
    assert session._interactive_last_ack is None
    assert session.unified_state.answer_dirty
    # The replaced registry entry does not own the old flush controller.
    session.flush.mark_completed()


async def test_cancellation_restores_dirty_snapshot(live_card):
    ctrl, session = live_card
    queued = asyncio.Event()

    async def send(*args, **kwargs):
        queued.set()
        await asyncio.Future()

    ctrl._client.update_card.side_effect = send
    task = asyncio.create_task(ctrl._do_unified_flush(session))
    await queued.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session._interactive_last_ack is None
    assert session.unified_state.answer_dirty


@pytest.mark.parametrize("retry_after, expected", [(None, 12.0), (3.0, 12.0), (45.0, 45.0)])
async def test_frequency_limit_respects_provider_cooldown(live_card, retry_after, expected):
    ctrl, session = live_card
    ctrl._client.update_card.side_effect = FeishuAPIError(
        "single message updated too frequently", code=IM_FREQUENCY_LIMIT,
        retry_after=retry_after,
    )
    with patch.object(ctrl, "_schedule_linear_flush"):
        await ctrl._do_unified_flush(session)
    remaining = session.flush.cooldown_until - time.monotonic()
    assert expected - 0.5 <= remaining <= expected
    assert session.unified_state.answer_dirty
    assert session._interactive_last_ack is None


async def test_first_content_remains_immediate_with_two_second_floor(live_card):
    ctrl, session = live_card
    delivered = asyncio.Event()

    async def send(*args, **kwargs):
        delivered.set()

    ctrl._client.update_card.side_effect = send
    ctrl._schedule_linear_flush(session)
    await asyncio.wait_for(delivered.wait(), timeout=0.5)
    assert session.flush.throttle_ms == 2.0
    assert session._first_flush_done
    assert ctrl._client.update_card.await_count == 1


async def test_bursty_updates_coalesce_into_latest_effective_frame(live_card):
    ctrl, session = live_card
    await session.flush.flush_now(lambda: ctrl._do_unified_flush(session))
    session._first_flush_done = True
    for _ in range(50):
        session.unified_state.on_answer_delta(" next")
        ctrl._schedule_linear_flush(session, force=True)
    await session.flush.flush_now(lambda: ctrl._do_unified_flush(session))
    assert ctrl._client.update_card.await_count == 1
    session.flush._cooldown_until = 0
    await session.flush.flush_now(lambda: ctrl._do_unified_flush(session))
    assert ctrl._client.update_card.await_count == 2
    card = ctrl._client.update_card.await_args.args[1]
    assert " next" * 50 in json.dumps(card)
    assert not session.unified_state.has_dirty
