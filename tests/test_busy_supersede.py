"""agent 忙时收到新消息（Hermes busy 路径）→ 封口旧卡、后续输出开新卡。

Hermes 的 ``_handle_active_session_busy_message`` 绕过 inbound 入口
（源码注释：busy callbacks bypass the message handler），插件拿不到新的
message_id；不开新卡的话，打断后的输出会继续写在被打断的那张卡上，
用户得往上翻才找得到。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from hermes_lark_streaming.config.reader import Config
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import (
    COMPLETED,
    COMPLETING,
    STREAMING,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState


def _enable(ctrl: StreamCardController, **plugin_cfg) -> None:
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True, **plugin_cfg},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }


def _add_session(
    ctrl: StreamCardController,
    message_id: str,
    *,
    chat_id: str = "chat1",
    card_msg_id: str = "om_card1",
    state: str = STREAMING,
    created_at: float = 100.0,
    linear: bool = True,
) -> CardSession:
    session = CardSession(message_id, chat_id, asyncio.get_running_loop())
    session.state = state
    session.card_msg_id = card_msg_id
    session.created_at = created_at
    session.linear = linear
    if linear:
        session.unified_state = UnifiedLinearState()
    ctrl._sess_put(message_id, session)
    return session


def _patch_side_effects(ctrl: StreamCardController):
    """拦住真实的建卡/封口网络调用（fire-and-forget 的那两个）。

    ``_fire_and_forget`` 被换掉后没人 await 那两个协程，直接关掉以免
    "coroutine was never awaited" 的告警。
    """

    def _drop(coro, _loop=None):
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    return (
        patch.object(ctrl, "_fire_and_forget", side_effect=_drop),
        patch.object(ctrl, "_do_create_linear_card", new=AsyncMock()),
    )


class TestBusySupersede:
    @pytest.mark.asyncio
    async def test_seals_old_card_and_continues_on_a_new_one(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        old = _add_session(ctrl, "om_old", card_msg_id="om_card_old")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_new_inbound", chat_id="chat1")

        # 旧卡被封口，并标明是被新消息打断 —— 不能显示成正常完成
        assert old._was_aborted is True
        assert old.error_message == "Superseded by a newer message"
        assert old.state == COMPLETING

        # 开了一张新卡，锚在新的用户消息上
        new_id = "om_new_inbound-cont-1"
        new_session = ctrl._sess_get(new_id)
        assert new_session is not None
        assert new_session._is_continuation is True
        assert new_session.chat_id == "chat1"
        assert ctrl._continuation_map["om_old"] == new_id

        # 关键：带着旧 message_id 的回调必须落到新卡上
        assert ctrl._get_active_session("om_old") is new_session

    @pytest.mark.asyncio
    async def test_is_idempotent_for_the_same_interrupted_turn(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        _add_session(ctrl, "om_old", card_msg_id="om_card_old")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_a", chat_id="chat1")
            ctrl.on_busy_superseded(message_id="om_b", chat_id="chat1")

        continuations = [
            mid for mid, s in ctrl._sess_items_snapshot() if s._is_continuation
        ]
        assert continuations == ["om_a-cont-1"]

    @pytest.mark.asyncio
    async def test_respects_config_switch(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        old = _add_session(ctrl, "om_old", card_msg_id="om_card_old")

        fire, create = _patch_side_effects(ctrl)
        with fire, create, patch.object(
            Config, "busy_supersede_new_card", new_callable=PropertyMock, return_value=False,
        ):
            ctrl.on_busy_superseded(message_id="om_new", chat_id="chat1")

        assert old._was_aborted is False
        assert old.state == STREAMING
        assert not [s for _m, s in ctrl._sess_items_snapshot() if s._is_continuation]

    @pytest.mark.asyncio
    async def test_ignores_a_different_chat(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        other = _add_session(ctrl, "om_other", chat_id="chat_other", card_msg_id="om_card_x")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_new", chat_id="chat1")

        assert other._was_aborted is False
        assert other.state == STREAMING
        assert ctrl._continuation_map == {}

    @pytest.mark.asyncio
    async def test_skips_a_session_that_is_already_finished(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        done = _add_session(ctrl, "om_done", state=COMPLETED, card_msg_id="om_card_done")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_new", chat_id="chat1")

        assert done._was_aborted is False
        assert ctrl._continuation_map == {}

    @pytest.mark.asyncio
    async def test_skips_a_session_whose_card_never_landed(self) -> None:
        """卡片还没建出来时没得封 —— 让它走自己的创建流程，别丢内容。"""
        ctrl = StreamCardController()
        _enable(ctrl)
        _add_session(ctrl, "om_pending", card_msg_id="")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_new", chat_id="chat1")

        assert ctrl._continuation_map == {}

    @pytest.mark.asyncio
    async def test_targets_the_newest_active_session(self) -> None:
        ctrl = StreamCardController()
        _enable(ctrl)
        older = _add_session(ctrl, "om_older", card_msg_id="om_card_a", created_at=1.0)
        newer = _add_session(ctrl, "om_newer", card_msg_id="om_card_b", created_at=9.0)

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_inbound", chat_id="chat1")

        assert newer._was_aborted is True
        assert older._was_aborted is False
        assert ctrl._continuation_map == {"om_newer": "om_inbound-cont-1"}

    @pytest.mark.asyncio
    async def test_seals_a_continuation_card_too(self) -> None:
        """第二次打断：上一轮开出来的续写卡同样是合法目标（v1.6.39）。

        线上第一次打断建出来的续写卡带着真实 card_msg_id，第二次打断必须能封它、
        再开一张；早先按 ``_is_continuation`` 把它排除，第二次就静默失效了
        （现象：第一次生效，第二次毫无反应）。
        """
        ctrl = StreamCardController()
        _enable(ctrl)
        _add_session(ctrl, "om_m1", card_msg_id="om_card_m1", created_at=1.0)

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_m2", chat_id="chat1")
            first = ctrl._sess_get("om_m2-cont-1")
            assert first is not None
            # 线上到这一步卡片已经真的建出来了
            first.card_msg_id = "om_card_c1"

            ctrl.on_busy_superseded(message_id="om_m3", chat_id="chat1")

        assert first._was_aborted is True
        assert first.state == COMPLETING
        second = ctrl._sess_get("om_m3-cont-1")
        assert second is not None
        assert second._is_continuation is True
        assert ctrl._continuation_map["om_m2-cont-1"] == "om_m3-cont-1"

        # 链式解析：带着最初 message_id 的回调要一路走到最后那张卡
        assert ctrl._get_active_session("om_m1") is second

    @pytest.mark.asyncio
    async def test_same_trigger_message_is_claimed_once(self) -> None:
        """同一个事件被通知两遍时不能连开两张卡。"""
        ctrl = StreamCardController()
        _enable(ctrl)
        _add_session(ctrl, "om_old", card_msg_id="om_card_old")

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_a", chat_id="chat1")
            ctrl.on_busy_superseded(message_id="om_a", chat_id="chat1")

        continuations = [
            mid for mid, s in ctrl._sess_items_snapshot() if s._is_continuation
        ]
        assert continuations == ["om_a-cont-1"]

    @pytest.mark.asyncio
    async def test_chain_is_consumed_at_the_tail_when_the_turn_finishes(self) -> None:
        """收尾消费整条链 —— 封的是最后那张卡，映射不留残渣。"""
        ctrl = StreamCardController()
        _enable(ctrl)
        _add_session(ctrl, "om_m1", card_msg_id="om_card_m1", created_at=1.0)

        fire, create = _patch_side_effects(ctrl)
        with fire, create:
            ctrl.on_busy_superseded(message_id="om_m2", chat_id="chat1")
            ctrl._sess_get("om_m2-cont-1").card_msg_id = "om_card_c1"
            ctrl.on_busy_superseded(message_id="om_m3", chat_id="chat1")

        assert ctrl._pop_continuation_id("om_m1") == "om_m3-cont-1"
        assert ctrl._continuation_map == {}


class TestBusySupersedeNotify:
    """gateway 侧的通知过滤 —— 内部事件绝不能封用户的卡。"""

    def _event(self, *, internal: bool, platform: str = "feishu", message_id: str = "om_in"):
        return SimpleNamespace(
            internal=internal,
            message_id=message_id,
            source=SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id="chat1"),
        )

    def test_notifies_for_a_real_feishu_message(self) -> None:
        from hermes_lark_streaming.patching import gateway as gw

        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            gw._notify_busy_supersede(self._event(internal=False))

        hook.assert_called_once_with(message_id="om_in", chat_id="chat1")

    def test_ignores_internal_events(self) -> None:
        """后台委派完成 / 心跳走同一个 busy 入口，且非常频繁。"""
        from hermes_lark_streaming.patching import gateway as gw

        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            gw._notify_busy_supersede(self._event(internal=True))

        hook.assert_not_called()

    def test_ignores_other_platforms(self) -> None:
        from hermes_lark_streaming.patching import gateway as gw

        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            gw._notify_busy_supersede(self._event(internal=False, platform="discord"))

        hook.assert_not_called()

    def test_ignores_events_without_ids(self) -> None:
        from hermes_lark_streaming.patching import gateway as gw

        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            gw._notify_busy_supersede(self._event(internal=False, message_id=""))

        hook.assert_not_called()


class TestBusyWrapper:
    """包装 ``_handle_active_session_busy_message``：只有被 busy 路径消化才通知。"""

    @pytest.mark.asyncio
    async def test_notifies_when_busy_path_consumed_the_event(self) -> None:
        from hermes_lark_streaming.patching import gateway as gw

        async def orig(self, event, session_key):
            return True

        wrapped = gw._wrap_handle_active_session_busy_message(orig)
        event = SimpleNamespace(
            internal=False,
            message_id="om_in",
            source=SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat1"),
        )
        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            result = await wrapped(object(), event, "session-key")

        assert result is True
        hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_notify_when_default_path_handles_it(self) -> None:
        """返回 False 表示消息会走正常 inbound 路径 —— 那时不该抢着开卡。"""
        from hermes_lark_streaming.patching import gateway as gw

        async def orig(self, event, session_key):
            return False

        wrapped = gw._wrap_handle_active_session_busy_message(orig)
        event = SimpleNamespace(
            internal=False,
            message_id="om_in",
            source=SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="chat1"),
        )
        with patch("hermes_lark_streaming.patching.hooks.on_busy_superseded") as hook:
            result = await wrapped(object(), event, "session-key")

        assert result is False
        hook.assert_not_called()
