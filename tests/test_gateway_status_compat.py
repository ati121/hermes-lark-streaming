"""Exercise the official Hermes status bridge without importing the full host.

The sender below is unchanged across the verified 0.21.4 canary
666f313d1d3abd8077291ba464cf0a10f1a6157f (gateway/run.py:757) and release
0.23.3 caafe5389ae585ef77387a962aee05b1bcf17029 (gateway/run.py:741).
The callback/receipt methods are from that 0.23.3 release's
gateway/run_turn_progress.py. Keep their bodies intact so dynamic imports,
notice policy, generation keys and cleanup exercise the actual host contract.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_lark_streaming import patching as P
from hermes_lark_streaming.patching import adapter as adapter_patch

_HOST_GATEWAY_SOURCE = '''
async def _send_or_update_status_coro(adapter, chat_id, status_key, content, metadata):
    """Route a status through adapter.send_or_update_status when supported (edits the previous
    bubble for the same status_key instead of appending); otherwise fall back to plain send.

    See #30045.
    """
    sender = getattr(adapter, "send_or_update_status", None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await adapter.send(chat_id, content, metadata=metadata)
'''

_HOST_PROGRESS_SOURCE = '''
class GatewayTurnProgressMixin:
    def _track_progress_result(self, result) -> None:
        """Remember a delivered progress/status message id for end-of-turn cleanup."""
        ctx = self._ctx
        if ctx._cleanup_progress and getattr(result, "success", False) and getattr(result, "message_id", None):
            ctx._cleanup_msg_ids.append(str(result.message_id))

    def _track_future_cleanup_id(self, fut) -> None:
        try:
            res = fut.result()
        except Exception:
            return
        self._track_progress_result(res)

    def _status_live(self) -> bool:
        """Status adapter present and this run is still the current generation."""
        return bool(self._ctx._status_adapter) and self._ctx._run_still_current()

    def _status_callback_sync(self, event_type: str, message: str) -> None:
        from gateway.run import _prepare_gateway_status_message, _redact_gateway_user_facing_secrets, _send_or_update_status_coro
        from gateway.run_status_filter import status_chat_type
        from gateway.warning_notifications import is_warning_status, render_notification
        ctx = self._ctx
        if ctx.mute_notification_reply or not self._status_live():
            return
        from gateway.bot_peer_notices import automatic_notices_allowed
        if not automatic_notices_allowed(ctx.source):
            logger.debug("status_callback suppressed for bot-authored message: %s", event_type)
            return
        prepared = _prepare_gateway_status_message(ctx.source.platform, event_type, message, status_chat_type(ctx.source))
        if prepared is None:
            logger.debug(
                "status_callback suppressed for %s/%s: %s",
                ctx.source.platform.value if ctx.source.platform else "unknown", event_type,
                _redact_gateway_user_facing_secrets(str(message or ""))[:160],
            )
            return
        status_key = event_type
        if ctx.session_key and ctx.run_generation is not None:
            status_key = f"{ctx.session_key}:run:{ctx.run_generation}:{event_type}"

        def present():
            if local_viewer_turn(self):
                from agent.conversation_compression import compaction_status_kind
                self._publish_execution("status.update", {"kind": compaction_status_kind(event_type, message),
                                                          "text": prepared})
                return
            fut = self._schedule(
                _send_or_update_status_coro(ctx._status_adapter, ctx._status_chat_id, status_key, prepared, ctx._status_thread_metadata),
                f"status_callback ({event_type}) scheduling error",
            )
            if fut is not None and ctx._cleanup_progress:
                fut.add_done_callback(self._track_future_cleanup_id)
        render_notification(present, platform=ctx.source.platform, user_config=ctx.user_config,
                            diagnostic=is_warning_status(event_type, message))
'''


def _snapshot_module(name, source):
    module = ModuleType(name)
    exec(compile(ast.parse(source), f"<Hermes v0.23.3 {name}>", "exec"), module.__dict__)  # noqa: S102 - pinned in-file host snapshot
    return module


@pytest.fixture
def host_status(monkeypatch):
    gateway = ModuleType("gateway")
    gateway.__path__ = []
    host = _snapshot_module("gateway.run", _HOST_GATEWAY_SOURCE)
    gateway.run = host
    progress_module = _snapshot_module("gateway.run_turn_progress", _HOST_PROGRESS_SOURCE)
    progress_module.logger = logging.getLogger("gateway.run")
    progress_module.local_viewer_turn = lambda _: False
    host._prepare_gateway_status_message = Mock(side_effect=lambda _p, _e, text, _c: text)
    host._redact_gateway_user_facing_secrets = lambda text: text

    status_filter = ModuleType("gateway.run_status_filter")
    status_filter.status_chat_type = lambda _: "dm"
    notices = ModuleType("gateway.warning_notifications")
    notices.is_warning_status = lambda *_: False
    notices.render_notification = lambda present, **_: present()
    bot_notices = ModuleType("gateway.bot_peer_notices")
    bot_notices.automatic_notices_allowed = Mock(return_value=True)
    for module in (gateway, host, progress_module, status_filter, notices, bot_notices):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    receipt = SimpleNamespace(success=True, message_id="om_native_status")
    transport = AsyncMock(return_value=receipt)

    class Adapter:
        send = adapter_patch._wrap_feishu_adapter_send(transport)

    adapter = Adapter()
    P._patched_feishu_classes.add(Adapter)
    session = SimpleNamespace(card_msg_id="om_streaming_card", state="streaming")
    controller = SimpleNamespace(enabled=True, _sess_get=Mock(return_value=session))
    monkeypatch.setattr("hermes_lark_streaming.controller.get_controller", lambda: controller)
    monkeypatch.setattr(adapter_patch, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))

    progress = progress_module.GatewayTurnProgressMixin()
    ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="chat", _run_still_current=lambda: True,
        source=SimpleNamespace(platform=SimpleNamespace(value="feishu")),
        mute_notification_reply=False, session_key="profile:session", run_generation=7,
        _status_thread_metadata={"thread_id": "topic", "reply_to_message_id": "om_question"},
        _cleanup_progress=True, _cleanup_msg_ids=[], user_config={},
    )
    progress._ctx = ctx
    tasks = []

    def schedule(coro, _log_message):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    progress._schedule = schedule
    token = P._msg_ctx.set({"event_message_id": "om_question", "card_sent": False})
    try:
        yield SimpleNamespace(
            host=host, progress=progress, ctx=ctx, tasks=tasks, receipt=receipt,
            adapter=adapter, transport=transport, bot_notices=bot_notices,
        )
    finally:
        P._msg_ctx.reset(token)
        P._patched_feishu_classes.discard(Adapter)


async def _settle_callbacks(host_status):
    results = await asyncio.gather(*host_status.tasks)
    # Task completion callbacks own the real host's cleanup receipt collection.
    await asyncio.sleep(0)
    host_status.tasks.clear()
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "👁️ Hindsight — recalled 3 memories", "👁️ Hindsight — saving to memory…",
])
async def test_prebound_official_callback_observes_patch_and_tracks_native_receipt(host_status, message):
    callback = host_status.progress._status_callback_sync
    callback("lifecycle", message)
    await _settle_callbacks(host_status)
    host_status.transport.assert_not_awaited()
    assert host_status.ctx._cleanup_msg_ids == []

    assert P._patch_gateway_status()
    callback("lifecycle", message)
    results = await _settle_callbacks(host_status)

    assert results == [host_status.receipt]
    assert host_status.ctx._cleanup_msg_ids == [host_status.receipt.message_id]
    host_status.transport.assert_awaited_once_with(
        host_status.adapter, "chat", message, reply_to=None,
        metadata=host_status.ctx._status_thread_metadata,
    )
    assert P._gateway_status_ctx.get() is False


@pytest.mark.asyncio
async def test_official_callback_keeps_native_update_key_and_metadata(host_status):
    native_update = host_status.adapter.send_or_update_status = AsyncMock(return_value=host_status.receipt)
    metadata = host_status.ctx._status_thread_metadata
    assert P._patch_gateway_status()

    host_status.progress._status_callback_sync("memory_recall", "👁️ Hindsight — recalled 1 memory")
    assert await _settle_callbacks(host_status) == [host_status.receipt]

    native_update.assert_awaited_once_with(
        "chat", "profile:session:run:7:memory_recall", "👁️ Hindsight — recalled 1 memory", metadata=metadata,
    )
    assert native_update.await_args.kwargs["metadata"] is metadata
    host_status.transport.assert_not_awaited()
    assert host_status.ctx._cleanup_msg_ids == [host_status.receipt.message_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsuccessful", "no_receipt", "exception"])
async def test_official_callback_does_not_track_failed_or_missing_receipts(host_status, failure):
    assert P._patch_gateway_status()
    if failure == "exception":
        host_status.transport.side_effect = RuntimeError("status transport failed")
    else:
        host_status.transport.return_value = SimpleNamespace(
            success=failure == "no_receipt", message_id=None,
        )
    host_status.progress._status_callback_sync("lifecycle", "👁️ Hindsight")
    await asyncio.gather(*host_status.tasks, return_exceptions=True)
    await asyncio.sleep(0)

    assert host_status.ctx._cleanup_msg_ids == []
    assert P._gateway_status_ctx.get() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["muted", "stale", "bot_authored", "filtered"])
async def test_status_patch_preserves_official_notice_policy(host_status, policy):
    assert P._patch_gateway_status()
    if policy == "muted":
        host_status.ctx.mute_notification_reply = True
    elif policy == "stale":
        host_status.ctx._run_still_current = lambda: False
    elif policy == "bot_authored":
        host_status.bot_notices.automatic_notices_allowed.return_value = False
    else:
        host_status.host._prepare_gateway_status_message.return_value = None
        host_status.host._prepare_gateway_status_message.side_effect = None

    host_status.progress._status_callback_sync("lifecycle", "👁️ Hindsight")

    assert host_status.tasks == []
    host_status.transport.assert_not_awaited()
    assert host_status.ctx._cleanup_msg_ids == []


def test_gateway_status_patch_is_idempotent_and_keeps_host_signature(host_status):
    original = host_status.host._send_or_update_status_coro
    assert P._patch_gateway_status()
    installed = host_status.host._send_or_update_status_coro
    assert installed.__wrapped__ is original
    assert inspect.signature(installed) == inspect.signature(original)
    assert P._patch_gateway_status()
    assert host_status.host._send_or_update_status_coro is installed
    assert ("gateway_status", P._patch_gateway_status, False) in P._patch_targets()


@pytest.mark.parametrize("missing", ["module", "symbol", "callable"])
def test_optional_status_patch_retries_when_host_becomes_available(host_status, monkeypatch, missing):
    original = host_status.host._send_or_update_status_coro
    if missing == "module":
        monkeypatch.setitem(sys.modules, "gateway.run", None)
    elif missing == "symbol":
        del host_status.host._send_or_update_status_coro
    else:
        host_status.host._send_or_update_status_coro = None

    assert P._patch_gateway_status() is False
    assert P._gateway_status_ctx.get() is False

    monkeypatch.setitem(sys.modules, "gateway.run", host_status.host)
    host_status.host._send_or_update_status_coro = original
    assert P._patch_gateway_status() is True
    assert host_status.host._send_or_update_status_coro.__wrapped__ is original
