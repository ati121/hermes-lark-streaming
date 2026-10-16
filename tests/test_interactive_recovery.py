"""IM card throttling, terminal recovery and static fallback regressions."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hermes_lark_streaming.cardkit import _LOADING_ELEMENT_ID
from hermes_lark_streaming.controller import (
    CardSession,
    StreamCardController,
    linear_mixin,
)
from hermes_lark_streaming.controller.mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    STREAMING,
)
from hermes_lark_streaming.feishu import (
    IM_FREQUENCY_LIMIT,
    FeishuAPIError,
    FeishuClient,
)
from hermes_lark_streaming.flush import FlushController
from hermes_lark_streaming.state.linear import UnifiedLinearState


@pytest.fixture
async def card_session(request):
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
    session.unified_state.on_answer_delta("**final answer**")
    session.text_sizes = {"body": {"mobile": "large"}}
    session.card_msg_id = "card-" + request.node.name
    session.card_id = "im:" + session.card_msg_id
    session.state = COMPLETING
    session._card_ready.set()
    session.flush.set_card_message_ready(True)
    ctrl._sess_put(session.message_id, session)
    yield ctrl, session
    ctrl._sess_clear()


def frequency_error():
    return FeishuAPIError("single message updated too frequently", code=IM_FREQUENCY_LIMIT)


def assert_static(card):
    serialized = json.dumps(card)
    assert "final answer" in serialized
    assert "streaming_mode" not in card["config"]
    assert all(
        item.get("element_id") != _LOADING_ELEMENT_ID
        for item in card["body"]["elements"]
    )


async def drain_recovery(session):
    task = session.seal_task
    assert task is not None
    await task
    assert session.seal_task is None


@pytest.mark.asyncio
async def test_interactive_throttle_has_independent_floor(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    session._first_flush_done = True
    with patch.object(session.flush, "schedule_update") as schedule:
        ctrl._schedule_linear_flush(session)
    assert session.flush.throttle_ms >= 1.5
    schedule.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limit_coalesces_dirty_and_forced_flushes(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    session._first_flush_done = True
    ctrl._client.update_card.side_effect = [frequency_error(), None]
    await session.flush.flush_now(lambda: ctrl._do_unified_flush(session))
    assert session._first_flush_done is True
    assert session.unified_state.answer_dirty is True
    assert session.flush.cooldown_until >= time.monotonic() + 11

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
    assert session.unified_state.answer_dirty is False


@pytest.mark.asyncio
async def test_network_failure_keeps_first_flush_and_dirty(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    session._first_flush_done = True
    ctrl._client.update_card.side_effect = OSError("offline")
    with patch.object(ctrl, "_schedule_linear_flush") as schedule:
        await ctrl._do_unified_flush(session)
    assert session._first_flush_done is True
    assert session.unified_state.answer_dirty is True
    schedule.assert_called_once_with(session, force=True)


@pytest.mark.asyncio
async def test_terminal_failure_sends_static_card_then_repairs_original(card_session, monkeypatch):
    ctrl, session = card_session
    original_id = session.card_msg_id
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_RECOVERY_DELAYS", (0,))
    ctrl._client.update_card.side_effect = [frequency_error(), None]
    await ctrl._do_linear_complete_with_fallback(session)
    assert session.state == CREATION_FAILED
    assert session.unified_state is None
    assert session.fallback_card is None
    ctrl._client.reply_card.assert_awaited_once()
    ctrl._client.reply_text.assert_not_awaited()
    fallback_card = ctrl._client.reply_card.await_args.args[1]
    assert_static(fallback_card)
    assert "hls_body" in json.dumps(fallback_card)

    # A newer independent turn is allowed, but repair must target only the old card.
    newer = CardSession("new-turn", session.chat_id, asyncio.get_running_loop())
    newer.state = STREAMING
    newer.card_msg_id = "new-card"
    ctrl._sess_put(newer.message_id, newer)
    await drain_recovery(session)
    assert session.state == COMPLETED
    assert newer.state == STREAMING
    args = ctrl._client.update_card.await_args.args
    assert args[0] == original_id
    assert args[1] == fallback_card
    assert ctrl._client.reply_card.await_count == 1


@pytest.mark.asyncio
async def test_recovery_is_bounded_and_never_resends_fallback(card_session, monkeypatch):
    ctrl, session = card_session
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_RECOVERY_DELAYS", (0, 0, 0))
    ctrl._client.update_card.side_effect = frequency_error()
    await ctrl._do_linear_complete_with_fallback(session)
    await drain_recovery(session)
    assert session.state == CREATION_FAILED
    assert ctrl._client.update_card.await_count == 4
    assert ctrl._client.reply_card.await_count == 1
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace_session", "replace_card", "abort", "delete", "epoch"])
async def test_recovery_skips_stale_snapshot(card_session, monkeypatch, change):
    ctrl, session = card_session
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_RECOVERY_DELAYS", (0,))
    ctrl._client.update_card.side_effect = frequency_error()
    await ctrl._do_linear_complete_with_fallback(session)
    if change == "replace_session":
        ctrl._sess_put(session.message_id, CardSession(
            session.message_id, session.chat_id, asyncio.get_running_loop(),
        ))
    elif change == "replace_card":
        session.card_msg_id = "replacement-card"
    elif change == "abort":
        session._was_aborted = True
    elif change == "delete":
        monkeypatch.setattr(linear_mixin, "is_unavailable", lambda _: True)
    else:
        session.create_epoch += 1
    await drain_recovery(session)
    assert ctrl._client.update_card.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_all", [False, True])
async def test_cleanup_cancels_pending_recovery(card_session, clear_all):
    ctrl, session = card_session
    ctrl._client.update_card.side_effect = frequency_error()
    await ctrl._do_linear_complete_with_fallback(session)
    task = session.seal_task
    assert task is not None
    if clear_all:
        ctrl._sess_clear()
    else:
        ctrl._cleanup(session.message_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._disposed is True
    assert session.seal_task is None
    assert task.cancelled()
    assert ctrl._client.update_card.await_count == 1


@pytest.mark.asyncio
async def test_static_card_failure_falls_back_to_text_once(card_session):
    ctrl, session = card_session
    session.anchor_id = "quoted-source"
    ctrl._client.update_card.side_effect = FeishuAPIError("invalid card", code=999999)
    ctrl._client.reply_card.side_effect = OSError("cannot send card")
    await asyncio.gather(
        ctrl._do_linear_complete_with_fallback(session),
        ctrl._do_linear_complete_with_fallback(session),
    )
    ctrl._client.reply_card.assert_awaited_once()
    ctrl._client.reply_text.assert_awaited_once()
    assert ctrl._client.reply_text.await_args.args[0] == "quoted-source"
    assert session.seal_task is None


@pytest.mark.asyncio
async def test_deleted_message_does_not_send_fallback_or_recover(card_session):
    ctrl, session = card_session
    ctrl._client.update_card.side_effect = FeishuAPIError("deleted", code=231003)
    await ctrl._do_linear_complete_with_fallback(session)
    ctrl._client.reply_card.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()
    assert session.seal_task is None
    assert session.guard.should_skip("test") is True


@pytest.mark.asyncio
async def test_stop_during_seal_rebuilds_aborted_snapshot(card_session):
    ctrl, session = card_session
    calls = []

    async def update(message_id, card, **kwargs):
        calls.append(card)
        if len(calls) == 1:
            ctrl.on_aborted(message_id=session.message_id)
            assert kwargs["should_update"]() is False
        else:
            assert kwargs["should_update"]() is True

    ctrl._client.update_card.side_effect = update
    with patch.object(ctrl, "_build_interactive_linear_card", wraps=ctrl._build_interactive_linear_card) as build:
        await ctrl._do_linear_complete_with_fallback(session)
    final_builds = [c.kwargs for c in build.call_args_list if c.kwargs.get("final")]
    assert [c["is_aborted"] for c in final_builds] == [False, True]
    assert session.state == ABORTED
    assert len(calls) == 2
    ctrl._client.reply_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_seals_do_not_patch_again(card_session):
    ctrl, session = card_session
    await asyncio.gather(
        ctrl._do_linear_complete_with_fallback(session),
        ctrl._do_linear_complete_with_fallback(session),
    )
    assert await ctrl._do_interactive_linear_complete(session) is True
    ctrl._client.update_card.assert_awaited_once()
    assert session.fallback_card is None


def sdk_client():
    client = object.__new__(FeishuClient)
    patch_request = AsyncMock()
    client._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(apatch=patch_request),
    )))
    client._card_json = AsyncMock(return_value="{}")
    client._check = lambda response, operation: None
    return client, patch_request


@pytest.mark.asyncio
async def test_seal_backoff_spans_long_window(monkeypatch):
    client, patch_request = sdk_client()
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def update(request):
        if sum(delays) < 15:
            raise frequency_error()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    patch_request.side_effect = update
    await client.update_card("test-card", {}, retry_frequency_limit=True)
    assert delays == [1, 2, 4, 8]
    assert patch_request.await_count == 5


@pytest.mark.asyncio
async def test_retry_rechecks_validity_after_backoff(monkeypatch):
    client, patch_request = sdk_client()
    valid = True
    patch_request.side_effect = frequency_error()

    async def sleep(delay):
        nonlocal valid
        valid = False

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await client.update_card(
        "test-card", {}, retry_frequency_limit=True, should_update=lambda: valid,
    )
    patch_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_rechecks_validity_after_asset_resolution():
    client, patch_request = sdk_client()
    valid = True

    async def serialize(card):
        nonlocal valid
        valid = False
        return "{}"

    client._card_json.side_effect = serialize
    await client.update_card("test-card", {}, should_update=lambda: valid)
    patch_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_automatically_flushes_latest_content(card_session, monkeypatch):
    ctrl, session = card_session
    session.state = STREAMING
    session._first_flush_done = True
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_COOLDOWN_SEC", 0.08)
    ctrl._cfg._raw["hermes_lark_streaming"]["flush_interval_ms"] = 1
    delivered = asyncio.Event()
    calls = 0

    async def update(message_id, card):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise frequency_error()
        assert "latest content" in json.dumps(card)
        delivered.set()

    ctrl._client.update_card.side_effect = update
    await session.flush.flush_now(lambda: ctrl._do_unified_flush(session))
    session.unified_state.on_answer_delta(" latest content")
    await asyncio.wait_for(delivered.wait(), timeout=2)
    assert calls == 2
    assert session.unified_state.has_dirty is False


@pytest.mark.asyncio
async def test_extended_cooldown_blocks_an_existing_timer():
    flush = FlushController(throttle_ms=0)
    flush.set_card_message_ready(True)
    delivered = asyncio.Event()

    async def update():
        delivered.set()

    try:
        flush.set_cooldown(0.03)
        flush.schedule_update(update)
        await asyncio.sleep(0.01)
        flush.set_cooldown(0.15)
        await flush.flush_now(update)
        await asyncio.sleep(0.06)
        assert not delivered.is_set()
        await asyncio.wait_for(delivered.wait(), timeout=2)
    finally:
        flush.mark_completed()


@pytest.mark.asyncio
async def test_completion_drains_old_patch_before_final_patch(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def update(message_id, card, **kwargs):
        calls.append(card)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    ctrl._client.update_card.side_effect = update
    streaming = asyncio.create_task(session.flush.flush_now(lambda: ctrl._do_unified_flush(session)))
    await entered.wait()
    session.state = COMPLETING
    completing = asyncio.create_task(ctrl._do_linear_complete_with_fallback(session))
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    await asyncio.gather(streaming, completing)
    assert len(calls) == 2
    assert_static(calls[-1])


@pytest.mark.asyncio
async def test_reasoning_button_cannot_race_pending_recovery(card_session):
    ctrl, session = card_session
    ctrl._client.update_card.side_effect = frequency_error()
    await ctrl._do_linear_complete_with_fallback(session)
    await ctrl.on_reasoning_toggle(card_msg_id=session.card_msg_id, expanded=True)
    assert ctrl._client.update_card.await_count == 1


@pytest.mark.asyncio
async def test_static_fallback_buttons_target_the_replacement_reply(card_session):
    ctrl, session = card_session
    ctrl._cfg._reload_cached = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}
    session.unified_state.on_reasoning_delta("reasoning example")
    ctrl._client.reply_card.return_value = "fallback-result-card"
    ctrl._client.update_card.side_effect = FeishuAPIError("invalid patch", code=999999)
    await ctrl._do_linear_complete_with_fallback(session)
    snapshot = ctrl._rsn_map()["fallback-result-card"]
    assert snapshot["card_msg_id"] == "fallback-result-card"
    assert snapshot["full"] == "reasoning example"
    assert ctrl._rsn_map()[session.card_msg_id]["card_msg_id"] == session.card_msg_id


@pytest.mark.asyncio
async def test_cardkit_failure_also_gets_a_static_result_card(card_session):
    ctrl, session = card_session
    session.interactive_mode = False
    ctrl._do_linear_complete = AsyncMock(return_value=False)
    await ctrl._do_linear_complete_with_fallback(session)
    ctrl._client.reply_card.assert_awaited_once()
    assert_static(ctrl._client.reply_card.await_args.args[1])
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_backoff_does_not_index_past_network_delays(monkeypatch):
    import httpx

    client, patch_request = sdk_client()
    patch_request.side_effect = [frequency_error()] * 4 + [httpx.ConnectError("offline")]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with pytest.raises(httpx.ConnectError):
        await client.update_card("test-card", {}, retry_frequency_limit=True)
    assert patch_request.await_count == 5


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fallback(card_session):
    ctrl, session = card_session
    ctrl._client.update_card.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await ctrl._do_linear_complete_with_fallback(session)
    ctrl._client.reply_card.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()
    assert session.seal_task is None


@pytest.mark.asyncio
async def test_stop_during_last_rejected_patch_updates_fallback_snapshot(card_session, monkeypatch):
    ctrl, session = card_session
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_RECOVERY_DELAYS", (0,))
    calls = 0

    async def update(message_id, card, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            ctrl.on_aborted(message_id=session.message_id)
            raise frequency_error()

    ctrl._client.update_card.side_effect = update
    with patch.object(ctrl, "_build_interactive_linear_card", wraps=ctrl._build_interactive_linear_card) as build:
        await ctrl._do_linear_complete_with_fallback(session)
    assert build.call_args.kwargs["is_aborted"] is True
    await drain_recovery(session)
    assert session.state == ABORTED
    assert ctrl._client.update_card.await_args.args[1] == ctrl._client.reply_card.await_args.args[1]


@pytest.mark.asyncio
async def test_live_reasoning_button_respects_stream_cooldown(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    session.flush.set_cooldown(12)
    await ctrl.on_reasoning_toggle(card_msg_id=session.card_msg_id, expanded=True)
    assert session.unified_state.reasoning_expanded is True
    assert session.unified_state.panel_dirty is True
    ctrl._client.update_card.assert_not_awaited()
    assert session.flush._pending_timer is not None


@pytest.mark.asyncio
async def test_completion_drains_in_flight_reasoning_toggle(card_session):
    ctrl, session = card_session
    session.state = STREAMING
    entered = asyncio.Event()
    release = asyncio.Event()
    cards = []

    async def update(message_id, card, **kwargs):
        cards.append(card)
        if len(cards) == 1:
            entered.set()
            await release.wait()

    ctrl._client.update_card.side_effect = update
    toggle = asyncio.create_task(ctrl.on_reasoning_toggle(
        card_msg_id=session.card_msg_id, expanded=True,
    ))
    await entered.wait()
    session.state = COMPLETING
    completion = asyncio.create_task(ctrl._do_linear_complete_with_fallback(session))
    await asyncio.sleep(0)
    assert len(cards) == 1
    release.set()
    await asyncio.gather(toggle, completion)
    assert len(cards) == 2
    assert_static(cards[-1])
