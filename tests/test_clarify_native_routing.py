"""Regressions for Hermes' native synchronous clarify dispatch (0.23+)."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.cardkit import build_clarify_card
from hermes_lark_streaming.feishu import MessageDeliveryUncertain
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

        def _card_toast(self, content):
            return SimpleNamespace(card=None, toast=content)

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
    gateway = SimpleNamespace(get_pending=Mock(return_value=pending), resolve_gateway_clarify=Mock(return_value=True),
                              clarify_refusal=Mock(return_value=None), refusal_text=Mock(return_value="Ask the requester"))
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.approval_actor", gateway)
    monkeypatch.setitem(patch_adapter._clarify_questions, "cid", "Restart?")
    monkeypatch.setitem(patch_adapter._clarify_choices, "cid", ["Yes", "No"])
    monkeypatch.setitem(patch_adapter._clarify_card_msg_ids, "cid", "card")
    yield adapter, stale_callback, gateway
    patch_adapter._clarify_selections.pop("cid", None)


def click(callback, action="select", *, value=None, message_id="card", chat_id="chat", **kwargs):
    if value is None:
        value = {"hermes_clarify_action": action, "clarify_id": "cid"}
    event = SimpleNamespace(
        action=SimpleNamespace(value=value, **kwargs),
        operator=SimpleNamespace(open_id="user", user_id="uid", union_id="union"),
        context=SimpleNamespace(open_chat_id=chat_id, open_message_id=message_id),
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


@pytest.mark.parametrize("option, expected", [(0, "Yes"), (1, "No")])
def test_native_dispatch_resolves_one_tap_choice(native_adapter, option, expected):
    adapter, callback, gateway = native_adapter
    prompt = build_clarify_card(question="Restart?", choices=["Yes", "No"], clarify_id="cid")
    row = next(e for e in prompt["body"]["elements"] if e.get("element_id") == "clarify_choices")
    button = row["columns"][option]["elements"][0]
    result = click(callback, value=button["behaviors"][0]["value"])
    gateway.resolve_gateway_clarify.assert_called_once_with("cid", expected)
    adapter.native.assert_not_called()
    assert result.card["body"]["elements"][1]["text"]["content"] == f"Selected: {expected}"
    assert "clarify_choices" not in str(result.card)
    assert "cid" not in adapter._clarify_state


@pytest.mark.parametrize("invalid", ["-1", "2", "garbage", ""])
def test_native_dispatch_rejects_invalid_one_tap_choice(native_adapter, invalid):
    _, callback, gateway = native_adapter
    click(callback, value={"hermes_clarify_action": "choice", "clarify_id": "cid", "choice_index": invalid})
    gateway.resolve_gateway_clarify.assert_not_called()


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


def test_prompt_refusal_leaves_card_and_waiter_unchanged(native_adapter):
    adapter, callback, gateway = native_adapter
    adapter.config = SimpleNamespace(extra={"scope": "profile"})
    gateway.clarify_refusal.return_value = "requester"

    result = click(callback, option="0")

    assert result.toast == "Ask the requester"
    assert result.card is None
    gateway.clarify_refusal.assert_called_once_with("cid", "uid", extra={"scope": "profile"})
    gateway.resolve_gateway_clarify.assert_not_called()
    assert "cid" in adapter._clarify_state


@pytest.mark.parametrize("context", [{"message_id": "other"}, {"chat_id": "other"}])
def test_callback_must_match_the_recorded_message_and_chat(native_adapter, context):
    adapter, callback, gateway = native_adapter
    assert click(callback, option="0", **context).card is None
    gateway.resolve_gateway_clarify.assert_not_called()
    assert "cid" in adapter._clarify_state


def test_long_choice_is_displayed_as_a_label_and_resolved_verbatim(native_adapter, monkeypatch):
    _, callback, gateway = native_adapter
    prefix = "/projects/" + "nested/" * 14
    first, second = prefix + "choice-a.json", prefix + "choice-b.json"
    monkeypatch.setitem(patch_adapter._clarify_choices, "cid", [first, second])

    result = click(callback, option="1")

    gateway.resolve_gateway_clarify.assert_called_once_with("cid", second)
    assert "…" in result.card["body"]["elements"][1]["text"]["content"]


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
    assert "no longer accepting answers" in str(result.card)
    assert "select_static" not in str(result.card)


def test_other_session_does_not_answer(native_adapter):
    _, callback, gateway = native_adapter
    gateway.get_pending.return_value = SimpleNamespace(session_key="other-profile")
    click(callback, option="0")
    gateway.resolve_gateway_clarify.assert_not_called()


def test_racing_answer_does_not_report_false_success(native_adapter):
    _, callback, gateway = native_adapter
    gateway.resolve_gateway_clarify.return_value = False
    result = click(callback, option="0")
    assert "no longer accepting answers" in str(result.card)


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


@pytest.mark.parametrize("via_factory", [False, True])
def test_recreated_adapter_is_patched_when_class_id_is_reused(monkeypatch, via_factory):
    from hermes_lark_streaming import patching

    # Model CPython reusing a retired class's address during adapter reload.
    monkeypatch.setattr(patching, "id", lambda cls: 42, raising=False)

    def new_adapter_class():
        class Adapter:
            async def send(self, *args, **kwargs):
                return "native"

            def _handle_clarify_card_action(self, **kwargs):
                return "native"

        return Adapter

    retired = new_adapter_class()
    assert _apply_feishu_adapter_patches(retired)
    replacement = new_adapter_class()
    if via_factory:
        create = patching._wrap_platform_registry_create_adapter(
            lambda name, config: replacement(),
        )
        create("feishu", {})
    else:
        assert _apply_feishu_adapter_patches(replacement)
    assert patching._already_wrapped(replacement.send)
    assert patching._already_wrapped(replacement._handle_clarify_card_action)


@pytest.mark.asyncio
async def test_sent_card_registers_native_retirement_and_authorization_state(monkeypatch):
    adapter = SimpleNamespace(_clarify_state={}, _validate_card_action=Mock())
    client = SimpleNamespace(send_card_to_chat=AsyncMock(return_value="sent-card"))
    ctrl = SimpleNamespace(enabled=True, _client_ok=lambda: True, _client=client, _sess_items_snapshot=list)
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    host = SimpleNamespace(mark_awaiting_text=Mock(), get_pending=Mock(), resolve_gateway_clarify=Mock(),
                           clarify_refusal=Mock(), refusal_text=Mock())
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", host)
    monkeypatch.setitem(sys.modules, "gateway.approval_actor", host)
    send = patch_adapter._wrap_feishu_adapter_send_clarify(AsyncMock())
    long = "/projects/" + "nested/" * 13 + "selection.json"
    raw = "{'label': 'Use the default', 'value': 'default'}"
    try:
        await send(adapter, "chat", "Question", [long, "", raw], "sent-cid", "image-session")
        assert adapter._clarify_state["sent-cid"] == {
            "chat_id": "chat", "session_key": "image-session", "question": "Question", "message_id": "sent-card",
        }
        assert patch_adapter._clarify_choices["sent-cid"] == [long, raw]
        sent = client.send_card_to_chat.await_args.args[1]
        assert "Use the default" in sent["body"]["elements"][1]["content"]
        assert long not in sent["body"]["elements"][1]["content"]
    finally:
        with patch_adapter._clarify_lock:
            patch_adapter._clear_clarify_locked("sent-cid")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["choice", "select", "input"])
async def test_lost_send_ack_leaves_the_original_card_answerable(native_adapter, monkeypatch, kind):
    adapter, callback, gateway = native_adapter
    sender = AsyncMock(side_effect=MessageDeliveryUncertain("connection closed after send"))
    client = SimpleNamespace(send_card_to_chat=sender)
    ctrl = SimpleNamespace(enabled=True, _client_ok=lambda: True, _client=client, _sess_items_snapshot=list)
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    monkeypatch.setattr(patch_adapter, "_send_result", SimpleNamespace)
    gateway.mark_awaiting_text = Mock()
    original = AsyncMock()
    send = patch_adapter._wrap_feishu_adapter_send_clarify(original)
    choices = ["A", "B", "C", "D"] if kind == "select" else ["A", "B"]

    result = await send(adapter, "chat", "Do this?", choices, "cid", "image-session")

    assert result.success is False
    assert result.raw_response["ambiguous"] is True
    original.assert_not_awaited()
    gateway.mark_awaiting_text.assert_called_once_with("cid")
    assert adapter._clarify_state["cid"]["message_id"] == ""

    elements = sender.await_args.args[1]["body"]["elements"]
    if kind == "choice":
        row = next(element for element in elements if element["tag"] == "column_set")
        control = row["columns"][0]["elements"][0]
        fields = {}
    else:
        tag = "select_static" if kind == "select" else "input"
        control = next(element for element in elements if element["tag"] == tag)
        fields = {"option": "0"} if kind == "select" else {"input_value": "A"}
    value = control["behaviors"][0]["value"]
    assert value["clarify_token"] == adapter._clarify_state["cid"]["callback_token"]

    assert click(callback, value={**value, "clarify_token": "wrong"}, **fields).card is None
    assert click(callback, value=value, chat_id="other", **fields).card is None
    assert click(callback, value=value, message_id="", **fields).card is None
    gateway.resolve_gateway_clarify.assert_not_called()

    assert "Selected: A" in str(click(callback, value=value, **fields).card)
    gateway.resolve_gateway_clarify.assert_called_once_with("cid", "A")
    assert "cid" not in adapter._clarify_state


@pytest.mark.asyncio
async def test_old_host_without_authority_renders_its_native_prompt(monkeypatch):
    client = SimpleNamespace(send_card_to_chat=AsyncMock())
    ctrl = SimpleNamespace(enabled=True, _client_ok=lambda: True, _client=client)
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: ctrl)
    monkeypatch.setitem(sys.modules, "gateway.approval_actor", None)
    adapter = SimpleNamespace(_validate_card_action=Mock())
    original = AsyncMock(return_value="legacy prompt")
    send = patch_adapter._wrap_feishu_adapter_send_clarify(original)

    assert await send(adapter, "chat", "Question", ["Yes"], "cid", "image-session") == "legacy prompt"
    original.assert_awaited_once()
    client.send_card_to_chat.assert_not_awaited()
