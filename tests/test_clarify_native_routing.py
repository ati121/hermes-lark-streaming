"""Regressions for Hermes' native synchronous clarify dispatch (0.23+)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.patching import _apply_feishu_adapter_patches
from hermes_lark_streaming.patching import adapter as patch_adapter


@pytest.fixture
def native_adapter(monkeypatch):
    class Adapter:
        _CARD_ACTION_ROUTES = (("hermes_clarify_action", "_handle_clarify_card_action"),)

        def __init__(self):
            self._clarify_state = {}
            self._loop = object()
            self.native = Mock(return_value="native")
            self._validate_card_action = Mock(return_value=("user", "chat", "name", "uid", "union"))

        def _on_card_action_trigger(self, data):
            # The SDK can retain this bound method before the plugin patches the class.
            for key, handler in self._CARD_ACTION_ROUTES:
                if data.event.action.value.get(key):
                    return getattr(self, handler)(
                        event=data.event, action_value=data.event.action.value, loop=self._loop,
                    )

        def _handle_clarify_card_action(self, **kwargs):
            return self.native(**kwargs)

        def _card_response(self, card=None):
            return SimpleNamespace(card=card)

        send = AsyncMock()
        send_clarify = AsyncMock()

        async def _handle_card_action_event(self, data):
            raise AssertionError("Native routing bypasses this old interception point")

    adapter = Adapter()
    stale_callback = adapter._on_card_action_trigger
    assert _apply_feishu_adapter_patches(Adapter)
    adapter._clarify_state["cid"] = {
        "chat_id": "chat", "session_key": "image-session", "question": "Restart?",
        "message_id": "card",
    }
    pending = SimpleNamespace(session_key="image-session")
    gateway = SimpleNamespace(get_pending=Mock(return_value=pending), resolve_gateway_clarify=Mock(return_value=True))
    import sys
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", gateway)
    monkeypatch.setitem(patch_adapter._clarify_questions, "cid", "Restart?")
    monkeypatch.setitem(patch_adapter._clarify_choices, "cid", ["Yes", "No"])
    monkeypatch.setitem(patch_adapter._clarify_card_msg_ids, "cid", "card")
    yield adapter, stale_callback, gateway
    patch_adapter._clarify_selections.pop("cid", None)


def click(callback, action="select", **kwargs):
    value = {"hermes_clarify_action": action, "clarify_id": "cid"}
    event = SimpleNamespace(
        action=SimpleNamespace(value=value, **kwargs),
        operator=SimpleNamespace(open_id="user", user_id="uid", union_id="union"),
        context=SimpleNamespace(open_chat_id="chat"),
    )
    return callback(SimpleNamespace(event=event))


@pytest.mark.parametrize("option, expected", [("0", "Yes"), ("1", "No")])
def test_native_dispatch_resolves_dropdown_and_returns_confirmed_card(native_adapter, option, expected):
    adapter, callback, gateway = native_adapter
    result = click(callback, option=option)
    gateway.resolve_gateway_clarify.assert_called_once_with("cid", expected)
    adapter.native.assert_not_called()
    adapter._validate_card_action.assert_called_once()
    assert expected in str(result.card)
    assert "select_static" not in str(result.card)
    assert "cid" not in adapter._clarify_state


@pytest.mark.parametrize("action, values", [
    ("input_submit", {"input_value": "  Wait, please  "}),
    ("button_submit", {"form_value": {"clarify_input": "Wait, please"}}),
])
def test_native_dispatch_resolves_input(native_adapter, action, values):
    adapter, callback, gateway = native_adapter
    result = click(callback, action, **values)
    gateway.resolve_gateway_clarify.assert_called_once_with("cid", "Wait, please")
    assert "Wait, please" in str(result.card)
    adapter.native.assert_not_called()


@pytest.mark.parametrize("action", ["0", "other", "future-native-action"])
def test_native_buttons_still_use_host_handler(native_adapter, action):
    adapter, callback, gateway = native_adapter
    assert click(callback, action) == "native"
    adapter.native.assert_called_once()
    gateway.resolve_gateway_clarify.assert_not_called()


def test_unauthorized_or_wrong_chat_is_rejected(native_adapter):
    adapter, callback, gateway = native_adapter
    adapter._validate_card_action.return_value = None
    click(callback, option="0")
    gateway.resolve_gateway_clarify.assert_not_called()
    assert "cid" in adapter._clarify_state


@pytest.mark.parametrize("invalid", ["-1", "2", "garbage", ""])
def test_invalid_option_does_not_answer(native_adapter, invalid):
    _, callback, gateway = native_adapter
    click(callback, option=invalid)
    gateway.resolve_gateway_clarify.assert_not_called()


def test_expired_card_does_not_report_success(native_adapter):
    _, callback, gateway = native_adapter
    gateway.get_pending.return_value = None
    result = click(callback, option="0")
    gateway.resolve_gateway_clarify.assert_not_called()
    assert "resolve_filled" not in str(result.card)


def test_other_session_does_not_answer(native_adapter):
    _, callback, gateway = native_adapter
    gateway.get_pending.return_value = SimpleNamespace(session_key="other-profile")
    click(callback, option="0")
    gateway.resolve_gateway_clarify.assert_not_called()


def test_racing_answer_does_not_report_false_success(native_adapter):
    _, callback, gateway = native_adapter
    gateway.resolve_gateway_clarify.return_value = False
    result = click(callback, option="0")
    assert "resolve_filled" not in str(result.card)


def test_duplicate_click_only_resolves_once(native_adapter):
    _, callback, gateway = native_adapter
    click(callback, option="0")
    click(callback, option="1")
    gateway.resolve_gateway_clarify.assert_called_once_with("cid", "Yes")


def test_state_is_retired_before_waking_waiter(native_adapter):
    adapter, callback, gateway = native_adapter

    def resolve(cid, text):
        assert cid not in adapter._clarify_state
        return True

    gateway.resolve_gateway_clarify.side_effect = resolve
    assert "resolve_filled" in str(click(callback, option="0").card)


@pytest.mark.asyncio
async def test_sent_card_registers_native_retirement_and_authorization_state(monkeypatch):
    adapter = SimpleNamespace(_clarify_state={})
    client = SimpleNamespace(send_card_to_chat=AsyncMock(return_value="sent-card"))
    ctrl = SimpleNamespace(enabled=True, _client_ok=lambda: True, _client=client, _sess_items_snapshot=list)
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    import sys
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", SimpleNamespace(mark_awaiting_text=Mock()))
    send = patch_adapter._wrap_feishu_adapter_send_clarify(AsyncMock())
    await send(adapter, "chat", "Question", ["Yes", "No"], "sent-cid", "image-session")
    assert adapter._clarify_state["sent-cid"] == {
        "chat_id": "chat", "session_key": "image-session", "question": "Question", "message_id": "sent-card",
    }
