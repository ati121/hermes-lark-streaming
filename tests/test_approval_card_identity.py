"""Card operators can carry different Feishu ID tiers from paired messages."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_lark_streaming.patching import _apply_feishu_adapter_patches


class NativeAdapter:
    """The host's identity preference and synchronous SDK dispatch seam."""

    _CARD_ACTION_ROUTES = (("hermes_action", "_handle_approval_card_action"),)

    def __init__(self, allowed, *, admins=(), allowed_users=()):
        self._admins = set(admins)
        self._allowed_group_users = set(allowed_users)
        self._is_sender_authorized = Mock(side_effect=lambda ident, kind, chat: ident in allowed and chat == "chat")
        self._approval_state = {1: {"chat_id": "chat"}}
        self.resolve = Mock()
        self._loop = object()

    def _is_interactive_operator_authorized(self, open_id, *, user_id="", union_id="", chat_id=""):
        normalized = str(open_id or "").strip()
        if not normalized:
            return False
        allowed_ids = self._admins | self._allowed_group_users
        if allowed_ids:
            operator_ids = {normalized, str(user_id or "").strip(), str(union_id or "").strip()} - {""}
            return "*" in allowed_ids or bool(operator_ids & allowed_ids)
        return self._is_sender_authorized(str(user_id or "").strip() or normalized, "group", chat_id) is True

    def _on_card_action_trigger(self, data):
        event = data.event
        for key, handler in self._CARD_ACTION_ROUTES:
            if event.action.value.get(key):
                return getattr(self, handler)(event=event, action_value=event.action.value, loop=self._loop)

    def _handle_approval_card_action(self, *, event, action_value, loop):
        ident = action_value["approval_id"]
        state = self._approval_state.get(ident)
        if not state:
            return None
        operator = event.operator
        if not self._is_interactive_operator_authorized(
            operator.open_id, user_id=operator.user_id, union_id=operator.union_id, chat_id=state["chat_id"],
        ):
            return None
        if event.context.open_chat_id != state["chat_id"]:
            return None
        self._approval_state.pop(ident)
        self.resolve(action_value["hermes_action"])
        return "resolved"

    async def send(self, *args, **kwargs):
        pass


def patched_adapter(allowed=(), **kwargs):
    # Each profile owns a distinct host class. Keep test instances isolated too.
    class Adapter(NativeAdapter):
        pass

    adapter = Adapter(allowed, **kwargs)
    stale_callback = adapter._on_card_action_trigger
    assert _apply_feishu_adapter_patches(Adapter)
    return adapter, stale_callback


def click(callback, action="approve_once", *, chat_id="chat"):
    return callback(SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value={"hermes_action": action, "approval_id": 1}),
        operator=SimpleNamespace(open_id="ou_operator", user_id="tenant_operator", union_id="on_operator"),
        context=SimpleNamespace(open_chat_id=chat_id),
    )))


@pytest.mark.parametrize("paired_id", ["ou_operator", "tenant_operator", "on_operator"])
@pytest.mark.parametrize("action", ["approve_once", "deny"])
def test_paired_identity_resolves_native_card_through_stale_sdk_callback(paired_id, action):
    adapter, callback = patched_adapter({paired_id})
    assert click(callback, action) == "resolved"
    adapter.resolve.assert_called_once_with(action)
    assert not adapter._approval_state
    assert click(callback, action) is None
    adapter.resolve.assert_called_once()


def test_unpaired_operator_cannot_answer():
    adapter, callback = patched_adapter({"ou_someone_else"})
    assert click(callback) is None
    adapter.resolve.assert_not_called()
    assert 1 in adapter._approval_state


@pytest.mark.parametrize("settings", [
    {"admins": {"ou_admin"}},
    {"allowed_users": {"ou_other"}},
])
def test_explicit_host_allowlist_still_restricts_paired_operator(settings):
    adapter, callback = patched_adapter({"ou_operator"}, **settings)
    assert click(callback) is None
    adapter._is_sender_authorized.assert_not_called()
    adapter.resolve.assert_not_called()


def test_wrong_chat_cannot_answer():
    adapter, callback = patched_adapter({"ou_operator"})
    assert click(callback, chat_id="other-chat") is None
    adapter.resolve.assert_not_called()
    assert 1 in adapter._approval_state


@pytest.mark.parametrize("verdict", [None, "yes", 1, Mock()])
def test_unknown_or_truthy_non_boolean_verdict_does_not_grant_access(verdict):
    adapter, callback = patched_adapter()
    adapter._is_sender_authorized.side_effect = None
    adapter._is_sender_authorized.return_value = verdict
    assert click(callback) is None
    adapter.resolve.assert_not_called()


def test_missing_open_id_is_rejected():
    adapter, _ = patched_adapter({"tenant_operator"})
    assert not adapter._is_interactive_operator_authorized("", user_id="tenant_operator", chat_id="chat")
    adapter._is_sender_authorized.assert_not_called()


def test_profile_authorization_is_not_shared_between_adapters():
    authorized, authorized_callback = patched_adapter({"ou_operator"})
    other, other_callback = patched_adapter({"ou_other"})
    assert click(authorized_callback) == "resolved"
    assert click(other_callback) is None
    authorized.resolve.assert_called_once()
    other.resolve.assert_not_called()
