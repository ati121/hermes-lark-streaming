"""v1.47 回归测试 — 封卡补建答案元素、Phase 2 不重放、重复建卡、封卡异常不卡死."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hermes_lark_streaming.cardkit import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
)
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import (
    COMPLETED,
    CREATING,
    CREATION_FAILED,
    STREAMING,
)
from hermes_lark_streaming.feishu import (
    CARDKIT_SEQUENCE_CONFLICT,
    FeishuAPIError,
    FeishuClient,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState


def _setup_ctrl() -> StreamCardController:
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    ctrl._initialized = True
    client = AsyncMock(spec=FeishuClient)
    client.cardkit_create = AsyncMock(return_value="card_id_abc")
    client.reply_card_by_id = AsyncMock(return_value="msg_id_reply")
    client.reply_card = AsyncMock(return_value="msg_id_reply")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    ctrl._client = client
    return ctrl


def _make_session(msg_id: str) -> CardSession:
    session = CardSession(msg_id, "chat_456", asyncio.get_running_loop())
    session.linear = True
    session.unified_state = UnifiedLinearState()
    session._card_ready.set()
    session.state = STREAMING
    session.card_id = f"card_{msg_id}"
    session.card_msg_id = f"reply_{msg_id}"
    return session


def _batch_actions(ctrl: StreamCardController) -> list[dict]:
    return [
        action
        for call_ in ctrl._client.cardkit_batch_update.await_args_list
        for action in call_.args[1]
    ]


def _inserted_ids(action: dict) -> list[str | None]:
    return [e.get("element_id") for e in action["params"]["elements"]]


class TestSealCreatesMissingElements:
    """答案只在 Phase 2 创建；COMPLETING 后 flush 不再运行，封卡必须补建."""

    @pytest.mark.asyncio
    async def test_short_reply_answer_inserted_at_seal(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("short")
        session.existing_elements = {_LOADING_ELEMENT_ID}
        session.unified_state.on_answer_delta("short reply")
        ctrl._sessions["short"] = session

        assert await ctrl._do_linear_complete(session) is True

        adds = [a for a in _batch_actions(ctrl) if a["action"] == "add_elements"]
        answer_add = next(a for a in adds if ANSWER_ELEMENT_ID in _inserted_ids(a))
        assert answer_add["params"]["type"] == "insert_before"
        assert answer_add["params"]["target_element_id"] == _LOADING_ELEMENT_ID
        assert "short reply" in answer_add["params"]["elements"][0]["content"]
        # The answer goes in before the footer rows inserted at the same anchor.
        assert adds.index(answer_add) == 0
        assert session.state == COMPLETED

    @pytest.mark.asyncio
    async def test_missing_panel_inserted_above_missing_answer(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("panel")
        session.existing_elements = {_LOADING_ELEMENT_ID}
        session.unified_state.on_reasoning_delta("think")
        session.unified_state.on_answer_delta("answer")
        ctrl._sessions["panel"] = session

        assert await ctrl._do_linear_complete(session) is True

        adds = [a for a in _batch_actions(ctrl) if a["action"] == "add_elements"]
        assert UNIFIED_PANEL_ELEMENT_ID in _inserted_ids(adds[0])
        assert ANSWER_ELEMENT_ID in _inserted_ids(adds[1])
        assert not any(a["action"] == "partial_update_element" for a in _batch_actions(ctrl))

    @pytest.mark.asyncio
    async def test_existing_answer_still_updated_not_inserted(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("existing")
        session.existing_elements = {_LOADING_ELEMENT_ID, ANSWER_ELEMENT_ID}
        session._creation_stages.update({"answer", "hint_removed"})
        session.unified_state.on_answer_delta("done")
        session.unified_state.answer_dirty = False
        ctrl._sessions["existing"] = session

        assert await ctrl._do_linear_complete(session) is True

        for action in _batch_actions(ctrl):
            if action["action"] == "add_elements":
                assert ANSWER_ELEMENT_ID not in _inserted_ids(action)


class TestPhase2NotReplayed:
    @pytest.mark.asyncio
    async def test_delta_during_phase2_does_not_resend_add_elements(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("replay")
        session.existing_elements = {_LOADING_ELEMENT_ID}
        state = session.unified_state
        state.on_reasoning_delta("think")
        state.on_answer_delta("hello")
        ctrl._sessions["replay"] = session

        async def _batch(*args, **kwargs):
            # A worker-thread delta lands while Phase 2 is on the wire.
            state.on_reasoning_delta(" more")

        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=_batch)

        await ctrl._do_unified_flush(session)

        answer_adds = [
            a for a in _batch_actions(ctrl)
            if a["action"] == "add_elements" and ANSWER_ELEMENT_ID in _inserted_ids(a)
        ]
        assert len(answer_adds) == 1

    @pytest.mark.asyncio
    async def test_phase3_network_error_restores_panel_dirty(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("p3net")
        session._creation_stages.update({"answer", "panel", "hint_removed"})
        state = session.unified_state
        state.on_reasoning_delta("think")
        state.answer_dirty = False
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=ConnectionError("reset"))

        await ctrl._do_unified_flush(session)

        assert state.panel_dirty is True


class TestDuplicateCreate:
    @pytest.mark.asyncio
    async def test_create_while_creating_leaves_ready_to_owner(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("dup")
        session.state = CREATING
        session.card_id = None
        session._card_ready.clear()

        await ctrl._do_create_linear_card(session)

        assert not session._card_ready.is_set()
        assert session.state == CREATING
        ctrl._client.cardkit_create.assert_not_called()


class TestSealFailureIsTerminal:
    @pytest.mark.asyncio
    async def test_retry_does_not_replay_delivered_batch(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("noreplay")
        session.existing_elements = {_LOADING_ELEMENT_ID}
        ctrl._sessions["noreplay"] = session
        ctrl._client.cardkit_close_streaming = AsyncMock(side_effect=[
            FeishuAPIError("conflict", code=CARDKIT_SEQUENCE_CONFLICT),
            None,
        ])

        assert await ctrl._do_linear_complete(session) is True
        assert ctrl._client.cardkit_batch_update.await_count == 1
        assert ctrl._client.cardkit_close_streaming.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_other_error_fails_instead_of_raising(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("retryfail")
        ctrl._sessions["retryfail"] = session
        ctrl._client.cardkit_close_streaming = AsyncMock(side_effect=[
            FeishuAPIError("conflict", code=CARDKIT_SEQUENCE_CONFLICT),
            FeishuAPIError("boom", code=99999),
        ])

        assert await ctrl._do_linear_complete(session) is False
        assert session.state == CREATION_FAILED

    @pytest.mark.asyncio
    async def test_drain_network_error_still_seals(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("drainnet")
        session._creation_stages.update({"answer", "hint_removed"})
        session.unified_state.on_answer_delta("tail")
        ctrl._sessions["drainnet"] = session
        ctrl._client.cardkit_stream_element = AsyncMock(side_effect=ConnectionError("reset"))

        assert await ctrl._do_linear_complete(session) is True
        ctrl._client.cardkit_close_streaming.assert_awaited_once()
        assert session.state == COMPLETED

    @pytest.mark.asyncio
    async def test_complete_exception_marks_session_terminal(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("raise")
        session.unified_state.on_answer_delta("answer")
        ctrl._sessions["raise"] = session
        ctrl._do_linear_complete = AsyncMock(side_effect=RuntimeError("unexpected"))

        await ctrl._do_linear_complete_with_fallback(session)

        assert session.state == CREATION_FAILED
        assert session.is_terminal_phase
        # The static fallback card still carries the snapshotted answer.
        ctrl._client.reply_card.assert_awaited_once()
