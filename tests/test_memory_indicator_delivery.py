"""Memory status delivery must coexist with streamed-answer deduplication."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming.patching import (
    _gateway_status_ctx,
    _msg_ctx,
    _patched_feishu_classes,
)
from hermes_lark_streaming.patching import adapter as adapter_patch
from hermes_lark_streaming.patching.status import _wrap_gateway_status_send


async def _host_status_send(adapter, chat_id, status_key, content, metadata):
    """Hermes gateway.run status routing, as verified at aeb5a071."""
    sender = getattr(adapter, "send_or_update_status", None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await adapter.send(chat_id, content, metadata=metadata)


@pytest.fixture
def indicator_delivery(monkeypatch):
    delivered = SimpleNamespace(success=True, message_id="om_memory_indicator")
    original_send = AsyncMock(return_value=delivered)
    original_edit = AsyncMock(return_value=delivered)
    original_delete = AsyncMock(return_value=True)

    class Adapter:
        send = adapter_patch._wrap_feishu_adapter_send(original_send)
        edit_message = adapter_patch._wrap_feishu_adapter_edit(original_edit)

        async def delete_message(self, chat_id, message_id):
            return await original_delete(chat_id, message_id)

    adapter = Adapter()
    _patched_feishu_classes.add(Adapter)
    session = SimpleNamespace(card_msg_id=None, state="creating")
    controller = SimpleNamespace(
        enabled=True,
        _get_session_for_delivery=Mock(return_value=session),
        _do_gateway_deliver=AsyncMock(return_value=("om_gateway_card", "gateway_card")),
    )
    ctx = {"event_message_id": "om_question", "card_sent": False}
    token = _msg_ctx.set(ctx)
    gateway_cards = {}
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: controller)
    monkeypatch.setattr(adapter_patch, "_gateway_cards", gateway_cards)
    monkeypatch.setattr(adapter_patch, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(adapter_patch, "_get_config", lambda: SimpleNamespace(gateway_cards=True))
    try:
        yield SimpleNamespace(
            adapter=adapter,
            original_send=original_send,
            original_edit=original_edit,
            original_delete=original_delete,
            controller=controller,
            session=session,
            ctx=ctx,
            delivered=delivered,
            gateway_cards=gateway_cards,
        )
    finally:
        _msg_ctx.reset(token)
        _patched_feishu_classes.discard(Adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_ready", "card_sent"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_ordinary_reply_keeps_existing_card_deduplication(
    indicator_delivery, card_ready, card_sent,
):
    delivery = indicator_delivery
    delivery.session.card_msg_id = "om_streaming_card" if card_ready else None
    delivery.ctx["card_sent"] = card_sent

    result = await delivery.adapter.send("chat", "Hindsight is discussed in the answer")

    if card_ready or card_sent:
        delivery.original_send.assert_not_awaited()
        assert result.success is True
    else:
        delivery.original_send.assert_awaited_once()
        assert result is delivery.delivered
    delivery.controller._do_gateway_deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_ready", "card_sent"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_status_send_reaches_transport_before_and_after_card_creation(
    indicator_delivery, card_ready, card_sent,
):
    delivery = indicator_delivery
    delivery.session.card_msg_id = "om_streaming_card" if card_ready else None
    delivery.ctx["card_sent"] = card_sent
    before = delivery.ctx.copy()
    content = "👁️ Hindsight\nMemory recall completed"
    metadata = {"thread_id": "thread", "routing": {"opaque": "keep"}}
    status_send = _wrap_gateway_status_send(_host_status_send)

    result = await status_send(delivery.adapter, "chat", "memory_recall", content, metadata)

    assert result is delivery.delivered
    assert result.message_id == "om_memory_indicator"
    delivery.original_send.assert_awaited_once_with(
        delivery.adapter, "chat", content, reply_to=None, metadata=metadata,
    )
    assert delivery.original_send.await_args.kwargs["metadata"] is metadata
    assert delivery.ctx == before
    assert delivery.gateway_cards == {}
    delivery.controller._do_gateway_deliver.assert_not_awaited()
    assert _gateway_status_ctx.get() is False


@pytest.mark.asyncio
async def test_status_without_agent_context_stays_a_native_status_message(indicator_delivery):
    delivery = indicator_delivery
    token = _msg_ctx.set(None)
    try:
        result = await _wrap_gateway_status_send(_host_status_send)(
            delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
        )
    finally:
        _msg_ctx.reset(token)

    assert result is delivery.delivered
    delivery.original_send.assert_awaited_once()
    delivery.controller._do_gateway_deliver.assert_not_awaited()
    assert delivery.gateway_cards == {}


@pytest.mark.asyncio
async def test_status_failure_preserves_real_result_and_reply_deduplication(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    failure = SimpleNamespace(success=False, message_id=None, error="rate limited")
    delivery.original_send.return_value = failure
    status_send = _wrap_gateway_status_send(_host_status_send)

    result = await status_send(delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None)

    assert result is failure
    assert _gateway_status_ctx.get() is False
    await delivery.adapter.send("chat", "final answer")
    assert delivery.original_send.await_count == 1
    assert delivery.ctx["card_sent"] is True


@pytest.mark.asyncio
async def test_status_exception_does_not_leave_reply_bypass_enabled(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    failure = OSError("connection lost")
    delivery.original_send.side_effect = failure
    status_send = _wrap_gateway_status_send(_host_status_send)

    with pytest.raises(OSError) as raised:
        await status_send(delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None)

    assert raised.value is failure
    assert _gateway_status_ctx.get() is False
    await delivery.adapter.send("chat", "final answer")
    assert delivery.original_send.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_status_does_not_leave_reply_bypass_enabled(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    delivery.original_send.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _wrap_gateway_status_send(_host_status_send)(
            delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
        )

    assert _gateway_status_ctx.get() is False
    await delivery.adapter.send("chat", "final answer")
    assert delivery.original_send.await_count == 1


@pytest.mark.asyncio
async def test_native_status_message_can_be_edited_and_deleted(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    result = await _wrap_gateway_status_send(_host_status_send)(
        delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
    )

    edited = await delivery.adapter.edit_message("chat", result.message_id, "Recall completed")
    deleted = await delivery.adapter.delete_message("chat", result.message_id)

    assert edited is delivery.delivered
    assert deleted is True
    delivery.original_edit.assert_awaited_once_with(
        delivery.adapter, "chat", "om_memory_indicator", "Recall completed",
    )
    delivery.original_delete.assert_awaited_once_with("chat", "om_memory_indicator")
    assert delivery.gateway_cards == {}


@pytest.mark.asyncio
async def test_status_send_or_update_keeps_native_key_and_message_lifecycle(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    native_statuses = {}
    seen = []
    metadata = {"thread_id": "thread"}

    async def send_or_update_status(chat_id, status_key, content, metadata=None):
        seen.append((chat_id, status_key, metadata))
        key = (chat_id, status_key)
        if key in native_statuses:
            return await delivery.adapter.edit_message(chat_id, native_statuses[key], content)
        result = await delivery.adapter.send(chat_id, content, metadata=metadata)
        if result.success and result.message_id:
            native_statuses[key] = result.message_id
        return result

    delivery.adapter.send_or_update_status = send_or_update_status
    status_send = _wrap_gateway_status_send(_host_status_send)
    first = await status_send(delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", metadata)
    second = await status_send(delivery.adapter, "chat", "memory_recall", "Recall completed", metadata)

    assert first is delivery.delivered
    assert second is delivery.delivered
    assert native_statuses == {("chat", "memory_recall"): "om_memory_indicator"}
    assert seen == [("chat", "memory_recall", metadata)] * 2
    delivery.original_send.assert_awaited_once()
    delivery.original_edit.assert_awaited_once_with(
        delivery.adapter, "chat", "om_memory_indicator", "Recall completed",
    )
    assert delivery.ctx["card_sent"] is True
    assert delivery.gateway_cards == {}


@pytest.mark.asyncio
async def test_native_status_edit_failure_can_resend_and_keep_real_receipt(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    delivery.original_edit.side_effect = OSError("previous status bubble was deleted")
    native_statuses = {("chat", "memory_recall"): "om_old_indicator"}
    metadata = {"thread_id": "thread"}

    async def send_or_update_status(chat_id, status_key, content, metadata=None):
        key = (chat_id, status_key)
        try:
            return await delivery.adapter.edit_message(chat_id, native_statuses[key], content)
        except OSError:
            # The host adapter owns this fallback; the plugin must preserve it.
            result = await delivery.adapter.send(chat_id, content, metadata=metadata)
            if result.success and result.message_id:
                native_statuses[key] = result.message_id
            return result

    delivery.adapter.send_or_update_status = send_or_update_status
    result = await _wrap_gateway_status_send(_host_status_send)(
        delivery.adapter, "chat", "memory_recall", "Recall completed", metadata,
    )

    assert result is delivery.delivered
    assert native_statuses == {("chat", "memory_recall"): "om_memory_indicator"}
    delivery.original_edit.assert_awaited_once_with(
        delivery.adapter, "chat", "om_old_indicator", "Recall completed",
    )
    delivery.original_send.assert_awaited_once_with(
        delivery.adapter, "chat", "Recall completed", reply_to=None, metadata=metadata,
    )
    assert delivery.ctx["card_sent"] is True
    assert delivery.gateway_cards == {}
    assert _gateway_status_ctx.get() is False


@pytest.mark.asyncio
async def test_nested_status_scopes_restore_outer_scope_and_final_reply_dedup(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    inner = _wrap_gateway_status_send(_host_status_send)

    async def outer_status(adapter, chat_id, status_key, content, metadata):
        await inner(adapter, chat_id, status_key, content, metadata)
        return await adapter.send(chat_id, "Provider ready", metadata=metadata)

    result = await _wrap_gateway_status_send(outer_status)(
        delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
    )

    assert result is delivery.delivered
    assert delivery.original_send.await_count == 2
    assert _gateway_status_ctx.get() is False
    await delivery.adapter.send("chat", "final answer")
    assert delivery.original_send.await_count == 2


@pytest.mark.asyncio
async def test_nested_status_failure_restores_parent_scope(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    failure = RuntimeError("status formatter failed")

    async def failing_status(*args, **kwargs):
        raise failure

    inner = _wrap_gateway_status_send(failing_status)

    async def outer_status(adapter, chat_id, status_key, content, metadata):
        with pytest.raises(RuntimeError) as raised:
            await inner(adapter, chat_id, status_key, content, metadata)
        assert raised.value is failure
        return await adapter.send(chat_id, content, metadata=metadata)

    await _wrap_gateway_status_send(outer_status)(
        delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
    )

    delivery.original_send.assert_awaited_once()
    assert _gateway_status_ctx.get() is False


@pytest.mark.asyncio
async def test_concurrent_status_send_does_not_release_plain_reply_dedup(indicator_delivery):
    delivery = indicator_delivery
    delivery.ctx["card_sent"] = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return delivery.delivered

    delivery.original_send.side_effect = send
    task = asyncio.create_task(_wrap_gateway_status_send(_host_status_send)(
        delivery.adapter, "chat", "memory_recall", "👁️ Hindsight", None,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(delivery.adapter.send("chat", "final answer"), 2)
        assert delivery.original_send.await_count == 1
        assert "_hls_bg_sending" not in vars(delivery.adapter)
        assert "send" not in vars(delivery.adapter)
    finally:
        release.set()
        await task

    assert _gateway_status_ctx.get() is False
    assert delivery.ctx["card_sent"] is True


@pytest.mark.asyncio
async def test_status_wrapper_preserves_arguments_and_original_return():
    result = object()
    original = AsyncMock(return_value=result)
    metadata = {"thread_id": "topic"}
    wrapped = _wrap_gateway_status_send(original)

    received = await wrapped("adapter", "chat", status_key="memory", content="notice", metadata=metadata)

    assert received is result
    original.assert_awaited_once_with(
        "adapter", "chat", status_key="memory", content="notice", metadata=metadata,
    )
    assert _gateway_status_ctx.get() is False


@pytest.mark.asyncio
async def test_status_scope_is_shared_across_profile_plugin_copies(monkeypatch):
    if "hermes_constants" not in sys.modules:
        monkeypatch.setitem(sys.modules, "hermes_constants", types.ModuleType("hermes_constants"))
    root = Path(__file__).resolve().parent.parent
    namespaces = ("hls_status_copy_a", "hls_status_copy_b")

    def load_copy(name):
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return importlib.import_module(f"{name}.patching")

    try:
        patching_a = load_copy(namespaces[0])
        patching_b = load_copy(namespaces[1])
        status_a = importlib.import_module(f"{namespaces[0]}.patching.status")
        adapter_b = importlib.import_module(f"{namespaces[1]}.patching.adapter")
        delivered = SimpleNamespace(success=True, message_id="om_cross_profile_indicator")
        original_send = AsyncMock(return_value=delivered)
        monkeypatch.setattr(adapter_b, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))

        class Adapter:
            send = adapter_b._wrap_feishu_adapter_send(original_send)

        patching_b._patched_feishu_classes.add(Adapter)
        token = patching_b._msg_ctx.set({"event_message_id": "profile-B-question", "card_sent": True})
        try:
            adapter = Adapter()
            result = await status_a._wrap_gateway_status_send(_host_status_send)(
                adapter, "chat", "memory_recall", "👁️ Hindsight", None,
            )
            assert result is delivered
            original_send.assert_awaited_once()
            await adapter.send("chat", "final answer")
            original_send.assert_awaited_once()
            assert patching_a._gateway_status_ctx.get() is False
            assert patching_b._gateway_status_ctx.get() is False
        finally:
            patching_b._msg_ctx.reset(token)
            patching_b._patched_feishu_classes.discard(Adapter)
    finally:
        for namespace in namespaces:
            for name in list(sys.modules):
                if name == namespace or name.startswith(namespace + "."):
                    sys.modules.pop(name, None)
