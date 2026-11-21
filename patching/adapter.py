"""FeishuAdapter interception layer — send, edit, reactions, and clarify cards."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import threading
import time
from typing import Any, Callable

from .. import __version__
from . import (
    _cron_delivery_ctx,
    _gateway_status_ctx,
    _msg_ctx,
    _gateway_cards,
    _gateway_cards_lock,
    _logger,
    _get_config,
    _patched_feishu_classes,
)

# ── FeishuAdapter interception layer (Phase 1: gateway message cards) ─

def _send_result(**kwargs: Any) -> Any:
    """Delayed ``SendResult``, or ``None`` when Hermes's gateway is absent.

    Eleven nested call sites repeated the same try/except pair. The import has
    to stay lazy: the host module only exists at runtime.
    """
    try:
        from gateway.platforms.base import SendResult
    except (ImportError, AttributeError):
        return None
    return SendResult(**kwargs)


def _classify_gateway_message(content: str) -> str:
    """Classify a gateway-internal message by its content for card category."""
    if not isinstance(content, str):
        return "system"
    # Auth / pairing messages
    if any(kw in content for kw in ("pairing code", "pairing requests", "配对码", "I don't recognize you")):
        return "auth"
    # Error messages
    if any(kw in content for kw in ("❌", "⚠️", "error", "failed", "Error", "Failed")):
        return "error"
    # Session lifecycle messages
    if any(kw in content for kw in ("Session", "session", "🔄", "♻", "compress", "compres")):
        return "session"
    # Slash command replies (common prefixes)
    if any(kw in content for kw in ("/help", "/status", "/model", "/usage", "/whoami", "/reset", "/new", "/stop", "/resume", "/undo", "/compress", "/goal", "/agents", "/background", "/queue", "/steer", "/yolo", "/footer")):
        return "slash"
    return "system"

async def _deliver_cron_card(chat_id: str, content: str) -> bool:
    """Send the cron cards; False means the original text is still unsent."""
    from ..controller.mixin import PartialCardDelivery
    from ..feishu import MessageDeliveryUncertain

    try:
        from ..controller import get_controller
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        await ctrl._do_cron_deliver(chat_id, content.strip())
    except (PartialCardDelivery, MessageDeliveryUncertain):
        raise
    except Exception:
        _logger.warning(
            "cron card delivery failed, falling back to text: chat=%s",
            chat_id[:12] if chat_id else "?",
            exc_info=True,
        )
        return False
    _logger.info(
        "hermes-lark-streaming v%s: cron card delivered: chat=%s",
        __version__,
        chat_id[:12] if chat_id else "?",
    )
    return True


def _wrap_feishu_adapter_send(orig_send: Callable) -> Callable:
    """Intercept ``FeishuAdapter.send()`` — convert text to gateway cards."""
    async def _intercepted_send(self_feishu, chat_id, content, reply_to=None, metadata=None, **kwargs):
        # On-demand repatch: if this adapter instance's class isn't patched yet
        # (deferred loading edge case), patch it now. O(1) set lookup.
        _cls = type(self_feishu)
        if _cls not in _patched_feishu_classes:
            from . import _apply_feishu_adapter_patches
            _apply_feishu_adapter_patches(_cls)

        # ── EphemeralReply passthrough (v1.3.1 fix) ──
        # NOT duplicate agent replies. They must NEVER be suppressed by the
        try:
            from gateway.platforms.base import EphemeralReply
            if isinstance(content, EphemeralReply):
                return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
        except (ImportError, AttributeError):
            pass  # EphemeralReply not available in this Hermes version

        if not isinstance(content, str):
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # ── Guard: skip empty content ──
        if not content.strip():
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # Hermes' dedicated status channel carries provider notices rather than
        # the agent's answer. Keep the native receipt/message id so status edits
        # and progress cleanup can address the message that was actually sent.
        if _gateway_status_ctx.get():
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        from ..controller.mixin import PartialCardDelivery
        from ..feishu import MessageDeliveryUncertain

        _text_content = content

        # ── Cron path: this send belongs to a cron ``_deliver_result`` ──
        if _cron_delivery_ctx.get() is not None:
            try:
                if await _deliver_cron_card(chat_id, content):
                    return _send_result(success=True)
            except PartialCardDelivery as exc:
                _logger.warning("cron card delivery incomplete; sending unsent text", exc_info=True)
                content = exc.remaining
            except MessageDeliveryUncertain:
                _logger.warning("cron card delivery was not confirmed", exc_info=True)
                return _send_result(
                    success=False, error="Feishu did not confirm card delivery", raw_response={"ambiguous": True},
                )
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        if getattr(self_feishu, "_hls_bg_sending", 0):
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # ── Agent path: suppress duplicate text reply ──
        ctx = _msg_ctx.get(None)
        if ctx is not None:
            eid = ctx.get("event_message_id") or ctx.get("message_id") or ""
            if eid:
                # We're inside an agent message pipeline.
                # If card was already sent, suppress the gateway's text reply.
                if ctx.get("card_sent"):
                    return _send_result(success=True)
                else:
                    try:
                        from ..controller import get_controller
                        _ctrl = get_controller()
                        if _ctrl and _ctrl.enabled:
                            _sess = _ctrl._sess_get(eid)
                            if _sess and _sess.card_msg_id:
                                _logger.info(
                                    "feishu_adapter_send: suppressing text reply "
                                    "(card exists for msg=%s, state=%s, card_sent=%s)",
                                    eid[:12], _sess.state, ctx.get("card_sent"),
                                )
                                ctx["card_sent"] = True
                                return _send_result(success=True)
                    except Exception:
                        _logger.debug("HLS: suppressed exception", exc_info=True)
                    # Agent still running, card not yet sent — don't interfere
                    return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # v1.3.2 fix (B3-04): the previous detection used a bare substring
        _stripped = content.strip()
        _is_stop_response = (
            len(_stripped) < 50  # /stop response is always short
            and _stripped.startswith("⚡")
            and any(kw in _stripped for kw in ("已停止", "stopped", "Stopped"))
        )
        if _is_stop_response:
            try:
                from ..controller import get_controller
                _ctrl = get_controller()
                if _ctrl and _ctrl.enabled:
                    # Find an active streaming session in this chat
                    for _sess in _ctrl._sess_values_snapshot():
                        if (
                            _sess.chat_id == chat_id
                            and _sess.state in ("streaming", "creating", "idle")
                            and _sess.card_msg_id
                        ):
                            _logger.info(
                                "gateway_send: /stop response detected, aborting "
                                "streaming card for msg=%s (state=%s)",
                                (_sess.message_id or "?")[:12],
                                _sess.state,
                            )
                            try:
                                from .hooks import on_message_aborted
                                on_message_aborted(message_id=_sess.message_id)
                            except Exception:
                                _logger.debug("HLS: suppressed exception", exc_info=True)
                            # Suppress the "⚡ 已停止" gateway card —
                            # the streaming card will show the stopped state.
                            return _send_result(success=True)
            except Exception:
                _logger.debug("HLS: suppressed exception", exc_info=True)
        _logger.info(
            "gateway_send: entering gateway-internal path, chat=%s content_len=%d",
            chat_id[:12] if chat_id else "?",
            len(content),
        )
        try:
            from ..controller import get_controller
            ctrl = get_controller()
            if ctrl and ctrl.enabled and _get_config().gateway_cards:
                cleaned = _text_content

                category = _classify_gateway_message(cleaned or content)
                card_msg_id, card_id = await ctrl._do_gateway_deliver(
                    chat_id, cleaned.strip() if cleaned.strip() else content,
                    category=category,
                )
                if card_msg_id:
                    _logger.info(
                        "hermes-lark-streaming v%s: gateway message card sent: "
                        "chat=%s category=%s content_len=%d card_id=%s",
                        __version__,
                        chat_id[:12] if chat_id else "?",
                        category,
                        len(content),
                        (card_id or "?")[:12],
                    )
                    return _send_result(success=True, message_id=card_msg_id)
        except PartialCardDelivery as exc:
            _logger.warning("gateway card delivery incomplete; sending unsent text", exc_info=True)
            content = exc.remaining
        except MessageDeliveryUncertain:
            _logger.warning("gateway card delivery was not confirmed", exc_info=True)
            return _send_result(
                success=False, error="Feishu did not confirm card delivery", raw_response={"ambiguous": True},
            )
        except Exception:
            _logger.info(
                "hermes-lark-streaming v%s: gateway card delivery failed, "
                "falling back to plain text",
                __version__,
                exc_info=True,
            )

        # ── Fallback: original plain text send ──
        _logger.info(
            "gateway_send: plain text fallback, chat=%s content_len=%d",
            chat_id[:12] if chat_id else "?",
            len(content),
        )
        return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

    return _intercepted_send

def _register_gateway_card(
    card_msg_id: str, *, chat_id: str, card_id: str | None, category: str,
    card: dict[str, Any] | None = None,
    continuation_ids: list[str] | None = None,
) -> None:
    """Register a gateway card so edit_message can update it later."""
    if not card_msg_id:
        return
    with _gateway_cards_lock:
        info = _gateway_cards.setdefault(card_msg_id, {"card": None})
        info.update(chat_id=chat_id, card_id=card_id, category=category, registered_at=time.time())
        if card is not None:
            info["card"] = copy.deepcopy(card)
        if continuation_ids is not None:
            info["continuation_ids"] = continuation_ids
        # v1.3.1: prune oldest entries when over capacity
        _GATEWAY_CARDS_MAX = 500
        if len(_gateway_cards) > _GATEWAY_CARDS_MAX:
            # Sort by registered_at, remove oldest 20% to amortize prune cost
            excess = len(_gateway_cards) - _GATEWAY_CARDS_MAX + (_GATEWAY_CARDS_MAX // 5)
            removable = [
                key for key, value in _gateway_cards.items()
                if "write_lock" not in value or not value["write_lock"].locked()
            ]
            sorted_keys = sorted(removable, key=lambda k: _gateway_cards[k].get("registered_at", 0))
            for k in sorted_keys[:excess]:
                _gateway_cards.pop(k, None)
            _logger.debug("HLS: _gateway_cards pruned %d entries (was %d)", excess, len(_gateway_cards) + excess)

def _unregister_gateway_card(card_msg_id: str) -> None:
    """Remove a gateway card from the registry."""
    with _gateway_cards_lock:
        _gateway_cards.pop(card_msg_id, None)

def _wrap_feishu_adapter_edit(orig_edit: Callable) -> Callable:
    """Intercept ``FeishuAdapter.edit_message()`` — update gateway card content."""
    async def _intercepted_edit(self_feishu, chat_id, message_id, content, metadata=None, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None and isinstance(content, str) and content.strip():
            _logger.info(
                "feishu_adapter_edit: updating gateway card msg_id=%s content_len=%d",
                message_id[:12] if message_id else "?",
                len(content),
            )
            try:
                from ..controller import get_controller
                ctrl = get_controller()
                if ctrl and ctrl.enabled:
                    # Check if gateway_cards feature is enabled
                    cfg = _get_config()
                    if cfg.gateway_cards:
                        cleaned = content

                        category = _classify_gateway_message(cleaned)
                        delete_message = getattr(self_feishu, "delete_message", None)
                        updated = await ctrl._do_gateway_card_update(
                            chat_id=card_info.get("chat_id", chat_id),
                            card_msg_id=message_id,
                            card_id=card_info.get("card_id"),
                            content=cleaned.strip(),
                            category=category,
                            delete_message=delete_message if callable(delete_message) else None,
                        )
                        if updated:
                            return _send_result(success=True, message_id=message_id)
                        return _send_result(success=False, error="Feishu gateway card update failed")
            except Exception:
                _logger.debug("gateway card edit failed, falling back to edit_message", exc_info=True)

        # ── Fallback: original edit_message ──
        _fallback_kwargs = {k: v for k, v in kwargs.items() if k != "metadata"}
        try:
            return await orig_edit(self_feishu, chat_id, message_id, content, **_fallback_kwargs)
        except TypeError:
            # If the original still rejects kwargs, try with no extra kwargs
            return await orig_edit(self_feishu, chat_id, message_id, content)

    return _intercepted_edit

# ── Reaction → card status indicator (Phase 3) ─────────────────────

# Map Feishu reaction emojis to human-readable status labels
_REACTION_STATUS_MAP: dict[str, str] = {
    "👀": "Reading",
    "👍": "Done",
    "🤔": "Thinking",
    "⏳": "Processing",
    "✅": "Completed",
    "🔄": "Refreshing",
    "📝": "Composing",
}

def _wrap_feishu_adapter_add_reaction(orig_add_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.add_reaction()`` — card status indicator."""
    async def _intercepted_add_reaction(self_feishu, message_id, emoji, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None:
            status_label = _REACTION_STATUS_MAP.get(emoji)
            if status_label:
                _logger.info(
                    "feishu_adapter_add_reaction: gateway card status msg_id=%s emoji=%s → %s",
                    message_id[:12] if message_id else "?",
                    emoji,
                    status_label,
                )
                try:
                    from ..controller import get_controller
                    ctrl = get_controller()
                    if ctrl and ctrl.enabled:
                        cfg = _get_config()
                        if cfg.gateway_cards:
                            # Update the card with a status indicator
                            updated = await ctrl._do_gateway_card_status(
                                card_msg_id=message_id,
                                card_id=card_info.get("card_id"),
                                status_label=status_label,
                                emoji=emoji,
                                category=card_info.get("category", "system"),
                            )
                            if updated:
                                # Suppress the actual reaction — card shows status instead
                                return _send_result(success=True)
                except Exception:
                    _logger.debug("gateway card status update failed, falling back to reaction", exc_info=True)

        # ── Fallback: original add_reaction ──
        return await orig_add_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_add_reaction

def _wrap_feishu_adapter_delete_reaction(orig_delete_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.delete_reaction()`` — clear card status."""
    async def _intercepted_delete_reaction(self_feishu, message_id, emoji, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None:
            status_label = _REACTION_STATUS_MAP.get(emoji)
            if status_label:
                _logger.info(
                    "feishu_adapter_delete_reaction: gateway card clear status msg_id=%s emoji=%s",
                    message_id[:12] if message_id else "?",
                    emoji,
                )
                try:
                    from ..controller import get_controller
                    ctrl = get_controller()
                    if ctrl and ctrl.enabled:
                        cfg = _get_config()
                        if cfg.gateway_cards:
                            # Clear the status indicator from the card
                            updated = await ctrl._do_gateway_card_status(
                                card_msg_id=message_id,
                                card_id=card_info.get("card_id"),
                                status_label="",
                                emoji="",
                                category=card_info.get("category", "system"),
                                clear_emoji=emoji,
                            )
                            if updated:
                                return _send_result(success=True)
                except Exception:
                    _logger.debug("gateway card status clear failed, falling back to reaction", exc_info=True)

        # ── Fallback: original delete_reaction ──
        return await orig_delete_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_delete_reaction

_clarify_lock = threading.Lock()
_clarify_choices: dict[str, list[str]] = {}  # clarify_id → exact answers in displayed order
_clarify_questions: dict[str, str] = {}  # clarify_id → question text
_clarify_card_msg_ids: dict[str, str] = {}  # clarify_id → card_msg_id (for server-side confirm update)
_clarify_selections: dict[str, str] = {}  # clarify_id → user's selected/input text (for retry)
_clarify_timestamps: dict[str, float] = {}  # clarify_id → creation time (for TTL cleanup)
_legacy_clarify_prompts: dict[str, tuple[object, dict[str, str]]] = {}
_CLARIFY_TTL_SEC = 30 * 60  # 30 分钟后未确认的追问自动清除

# Backward-compatible aliases (old names used in tests)
_clarify_answers = _clarify_selections  # noqa: F841
_clarify_card_info = _clarify_card_msg_ids  # noqa: F841

def _clear_clarify_locked(clarify_id: str) -> None:
    for store in (_clarify_choices, _clarify_questions, _clarify_card_msg_ids,
                  _clarify_selections, _clarify_timestamps, _legacy_clarify_prompts):
        store.pop(clarify_id, None)


def _prune_expired_clarify() -> None:
    """清理过期的追问数据（超过 _CLARIFY_TTL_SEC 未确认的条目）."""
    with _clarify_lock:
        if not _clarify_timestamps:
            return
        now = time.monotonic()
        expired = [cid for cid, ts in _clarify_timestamps.items() if now - ts > _CLARIFY_TTL_SEC]
        for cid in expired:
            _clear_clarify_locked(cid)
        if expired:
            _logger.debug("HLS: pruned %d expired clarify entries", len(expired))


def _clarify_host_helpers():
    try:
        from gateway.approval_actor import clarify_refusal, refusal_text
        from tools.clarify_gateway import get_pending, resolve_gateway_clarify
    except ImportError:
        return None
    return clarify_refusal, refusal_text, get_pending, resolve_gateway_clarify


def _clarify_host_validator(adapter):
    validator = getattr(adapter, "_validate_card_action", None)
    if callable(validator):
        return validator
    # Newer hosts moved the same validation out of the adapter class.
    try:
        from plugins.platforms.feishu.cards import validate_card_action
    except ImportError:
        return None
    return lambda **kwargs: validate_card_action(adapter, **kwargs)


def _wrap_feishu_adapter_send_clarify(orig_send_clarify: Callable) -> Callable:
    """Intercept ``FeishuAdapter.send_clarify()`` — render interactive card."""

    async def _intercepted_send_clarify(
        self_feishu, chat_id, question, choices, clarify_id, session_key, metadata=None, **kwargs
    ):
        from ..feishu import MessageDeliveryUncertain

        _logger.info(
            "clarify card: send_clarify intercepted chat=%s choice_count=%s clarify_id=%s",
            (chat_id or "?")[:12],
            len(choices or []),
            (clarify_id or "?")[:12],
        )

        # Prune expired clarify data before creating new entries
        _prune_expired_clarify()

        try:
            from ..controller import get_controller
            ctrl = get_controller()
            if (not ctrl or not ctrl.enabled or not ctrl._client_ok()
                    or _clarify_host_helpers() is None or _clarify_host_validator(self_feishu) is None):
                _logger.debug("clarify card: controller or prompt authority unavailable, using host prompt")
                return await orig_send_clarify(
                    self_feishu, chat_id, question, choices, clarify_id, session_key,
                    metadata=metadata, **kwargs
                )

            # v1.3.0 fix: Flush + cancel pending timers BEFORE sending clarify card.
            # Fix: find the active streaming session for this chat_id, cancel its
            try:
                for _mid, _sess in ctrl._sess_items_snapshot():
                    if _sess.chat_id == chat_id and not _sess.is_terminal_phase:
                        if _sess.unified_state and _sess.unified_state.has_dirty:
                            _logger.info(
                                "clarify card: flushing pending answer before clarify "
                                "msg=%s dirty=%s",
                                (_mid or "?")[:12],
                                bool(_sess.unified_state.answer_dirty),
                            )
                            # Force immediate flush and wait for completion.
                            # This cancels the pending timer and writes dirty data now.
                            await _sess.flush.flush_now(
                                lambda s=_sess: ctrl._do_unified_flush(s)
                            )
                        else:
                            # No dirty data — just cancel the pending timer so the
                            # streaming card stops updating while the clarify is shown.
                            _sess.flush._cancel_timer()
                        break
            except Exception:
                _logger.debug("clarify card: pre-flush failed (non-fatal)", exc_info=True)

            from ..cardkit import build_clarify_card, clarify_choice_pairs

            callback_token = secrets.token_urlsafe(16)
            card = build_clarify_card(
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                callback_token=callback_token,
            )
            original_choices = [answer for _, answer in clarify_choice_pairs(choices)]

            # Send the card via FeishuClient
            reply_to = None
            if metadata and isinstance(metadata, dict):
                reply_to = metadata.get("reply_to") or metadata.get("message_id")

            try:
                if reply_to:
                    card_msg_id = await ctrl._client.reply_card(reply_to, card)
                else:
                    card_msg_id = await ctrl._client.send_card_to_chat(chat_id, card)
            except MessageDeliveryUncertain:
                card_msg_id = ""
                _logger.warning("clarify card: Feishu did not confirm delivery id=%s", clarify_id[:12], exc_info=True)

            # Hermes 0.23+ routes clarify callbacks before _handle_card_action_event.
            # Register the same state its native cards use for authorization,
            # session checks and retiring prompts after timeout or a typed reply.
            native_state = getattr(self_feishu, "_clarify_state", None)
            state = {"session_key": session_key, "message_id": card_msg_id or "",
                     "chat_id": chat_id, "question": question}
            if not card_msg_id:
                state["callback_token"] = callback_token
            with _clarify_lock:
                _clarify_choices[clarify_id] = original_choices
                _clarify_questions[clarify_id] = question
                _clarify_timestamps[clarify_id] = time.monotonic()
                if card_msg_id:
                    _clarify_card_msg_ids[clarify_id] = card_msg_id
                if isinstance(native_state, dict):
                    native_state[clarify_id] = state
                else:
                    _legacy_clarify_prompts[clarify_id] = (self_feishu, state)

            if card_msg_id:
                _register_gateway_card(card_msg_id, chat_id=chat_id, card_id=None, category="clarify", card=card)

            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
                _logger.debug("clarify card: mark_awaiting_text called for clarify_id=%s", (clarify_id or "?")[:12])
            except Exception as e:
                _logger.debug("clarify card: mark_awaiting_text failed (%s), card callback will handle resolution", e)

            if not card_msg_id:
                # Hermes leaves the question pending without sending a duplicate.
                return _send_result(success=False, error="Feishu did not confirm card delivery",
                                    raw_response={"ambiguous": True})
            return _send_result(success=True, message_id=card_msg_id)

        except Exception as e:
            _logger.warning(
                "clarify card: failed to send card, falling back to text: %s",
                e,
                exc_info=True,
            )
            return await orig_send_clarify(
                self_feishu, chat_id, question, choices, clarify_id, session_key,
                metadata=metadata, **kwargs
            )

    return _intercepted_send_clarify


def _wrap_feishu_interactive_authorization(original_method: Callable) -> Callable:
    """Check SDK operator aliases against the host's existing authorization gate.

    Message events may pair an open_id while card callbacks additionally supply
    a tenant user_id. Hermes checks only the preferred user_id at its gateway
    fallback, so the same paired operator can be denied. Explicit Feishu lists
    remain authoritative; aliases never create or share authorization grants.
    """
    def _wrapped(self, open_id, **kwargs):
        if original_method(self, open_id, **kwargs) is True:
            return True
        normalized = str(open_id or "").strip()
        if not normalized or getattr(self, "_admins", None) or getattr(self, "_allowed_group_users", None):
            return False
        check = getattr(self, "_is_sender_authorized", None)
        if not callable(check):
            return False
        primary = str(kwargs.get("user_id") or "").strip() or normalized
        seen = {primary}
        for field, ident in (("open_id", normalized), ("union_id", str(kwargs.get("union_id") or "").strip())):
            if not ident or ident in seen:
                continue
            seen.add(ident)
            if check(ident, "group", str(kwargs.get("chat_id") or "") or None) is True:
                _logger.info("HLS: card operator authorized by existing %s grant", field)
                return True
        return False

    return _wrapped


_APPROVAL_ACTION_LABELS_ZH = {
    "approve_once": "仅本次允许",
    "approve_session": "本会话允许",
    "approve_always": "始终允许",
    "deny": "拒绝",
}
_APPROVAL_RESULT_LABELS_ZH = {
    "once": "已批准本次操作",
    "session": "已允许本会话",
    "always": "已设为始终允许",
    "deny": "已拒绝",
}
_APPROVAL_RECALL_DELAY_SEC = 5
_approval_recall_tasks: set[asyncio.Task[None]] = set()


def _wrap_feishu_send_interactive_card(original_method: Callable) -> Callable:
    """Translate the native approval card without changing its available actions."""
    async def _wrapped(self, chat_id, card, *args, **kwargs):
        states = getattr(self, "_approval_state", None)
        if states is not None and kwargs.get("state_map") is states:
            card = copy.deepcopy(card)
            card.setdefault("config", {})["update_multi"] = True
            if title := card.get("header", {}).get("title"):
                title["content"] = "⚠️ 操作审批"
            for row in card.get("elements", []):
                for button in row.get("actions", []):
                    action = button.get("value", {}).get("hermes_action")
                    if action in _APPROVAL_ACTION_LABELS_ZH:
                        button["text"]["content"] = _APPROVAL_ACTION_LABELS_ZH[action]
        return await original_method(self, chat_id, card, *args, **kwargs)

    return _wrapped


def _wrap_feishu_build_resolved_approval_card(original_method: Callable) -> Callable:
    """Use Chinese for both the callback card and the persisted result."""
    def _wrapped(*args, **kwargs):
        card = original_method(*args, **kwargs)
        card.setdefault("config", {})["update_multi"] = True
        choice = kwargs.get("choice")
        label = _APPROVAL_RESULT_LABELS_ZH.get(choice)
        title = card.get("header", {}).get("title")
        elements = card.get("elements", [])
        if label and title and elements and elements[0].get("tag") == "markdown":
            from ..cardkit.elements import _escape_md

            icon = "❌" if choice == "deny" else "✅"
            title["content"] = f"{icon} {label}"
            body = kwargs.get("body")
            user_name = _escape_md(str(kwargs.get("user_name") or ""))
            decided = f"{icon} **{label}** · 操作人：{user_name}" if user_name else f"{icon} **{label}**"
            elements[0]["content"] = f"{body}\n\n{decided}" if body else decided
        return card

    return _wrapped


async def _recall_approval_card(delete_message: Callable, chat_id: str, message_id: str) -> None:
    await asyncio.sleep(_APPROVAL_RECALL_DELAY_SEC)
    try:
        if not await delete_message(chat_id, message_id):
            _logger.warning("HLS: Feishu did not recall the resolved approval card")
    except Exception:  # noqa: BLE001 - recall is separate from settling the approval
        _logger.exception("HLS: resolved approval card recall failed")


async def _update_interactive_result(adapter, message_id: str, card: dict) -> None:
    """Persist the decision on the original IM card using its own bot."""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

    request = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build())
        .build()
    )
    for attempt in range(4):
        response = await adapter._run_blocking(adapter._client.im.v1.message.patch, request)
        if response.success():
            return
        if response.code in (230020, 230429) and attempt < 3:
            await asyncio.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"interactive card update failed: code={response.code}")


def _approval_persistence_resolver(adapter) -> Callable | None:
    if not all(callable(getattr(adapter, name, None)) for name in (
        "_pop_validated_prompt_state", "_build_resolved_approval_card", "_run_blocking",
    )):
        return None
    try:
        from tools.approval import resolve_gateway_approval_card

        patch_message = adapter._client.im.v1.message.patch
    except (AttributeError, ImportError):
        return None
    return resolve_gateway_approval_card if callable(patch_message) and callable(resolve_gateway_approval_card) else None


def _wrap_feishu_resolve_approval(original_method: Callable) -> Callable:
    """Settle through Hermes, then immediately persist its native result card.

    Hermes returns the result card in the SDK acknowledgement but never writes
    it through IM PATCH. Some clients retain the prompt until a later refresh.
    Use the host's validation and resolver, and patch only a settled request.
    Older hosts without these APIs retain their original resolver.
    """
    async def _persist_result(self, resolve_gateway_approval_card, approval_id, choice, user_name, *, open_id="",
                              chat_id="", user_id="", union_id=""):
        state = self._pop_validated_prompt_state(
            states=self._approval_state, ident=approval_id, label="Approval", open_id=open_id, chat_id=chat_id,
            unauthorized_fmt="[Feishu] Unauthorized approval click by %s for approval %s",
            operator_repr=open_id or "<unknown>", user_id=user_id, union_id=union_id,
        )
        if not state:
            return
        count = resolve_gateway_approval_card(
            state["session_key"], choice, state.get("request_id"), actor_id=user_id or open_id, extra=self.config.extra,
        )
        _logger.info("HLS: native approval settled count=%s choice=%s", count, choice)
        if not count:
            # Keep Hermes' late-approval notice, and never PATCH false success
            # over a timeout or another answer which already won the race.
            if choice != "deny" and (target := state.get("chat_id") or chat_id):
                try:
                    from agent.i18n import t
                    await self.send(target, t("platform.shared.approval_expired"))
                except Exception:  # noqa: BLE001 - notification failure cannot change an expired decision
                    _logger.exception("HLS: expired approval notice failed")
            return
        if not (message_id := state.get("message_id")):
            return
        try:
            card = self._build_resolved_approval_card(choice=choice, user_name=user_name, body=state.get("body"))
            await _update_interactive_result(self, message_id, card)
            _logger.info("HLS: approval result persisted immediately choice=%s", choice)
        except Exception:  # noqa: BLE001 - isolate display errors after approval has already settled
            # The command decision has already settled; a display failure must
            # never repeat it or prevent the waiting agent from continuing.
            _logger.exception("HLS: immediate approval card update failed")
            return
        delete_message = getattr(self, "delete_message", None)
        target = state.get("chat_id") or chat_id
        if callable(delete_message) and target:
            task = asyncio.create_task(_recall_approval_card(delete_message, target, message_id))
            _approval_recall_tasks.add(task)
            task.add_done_callback(_approval_recall_tasks.discard)

    async def _wrapped(self, *args, **kwargs):
        known = {"approval_id", "choice", "user_name", "open_id", "chat_id", "user_id", "union_id"}
        resolver = _approval_persistence_resolver(self)
        if kwargs.keys() - known or resolver is None:
            return await original_method(self, *args, **kwargs)
        return await _persist_result(self, resolver, *args, **kwargs)

    _wrapped._hls_persists_approval_result = True
    return _wrapped


def _wrap_feishu_approval_card_ack(original_method: Callable) -> Callable:
    """Let Hermes authorize and queue the click, then acknowledge without a speculative result."""
    def _wrapped(self, *args, **kwargs):
        action = kwargs.get("action_value")
        states = getattr(self, "_approval_state", None)
        state = states.get(action.get("approval_id")) if isinstance(action, dict) and isinstance(states, dict) else None
        resolver = getattr(self, "_resolve_approval", None)
        # Capture the live state before calling Hermes, since its worker can
        # remove it as soon as the scheduled coroutine starts.
        result_owned = (state and state.get("message_id")
                        and getattr(resolver, "_hls_persists_approval_result", False) is True
                        and _approval_persistence_resolver(self) is not None)
        response = original_method(self, *args, **kwargs)
        if result_owned and getattr(response, "card", None) is not None:
            response.card = None
        return response

    return _wrapped


def _clarify_choice_text(action, action_value, action_obj, choices: list[str]) -> str | None:
    raw_index = action_value.get("choice_index") if action == "choice" else getattr(action_obj, "option", None)
    index = str(raw_index)
    if not index.isascii() or not index.isdecimal():
        return None
    try:
        return choices[int(index)]
    except (IndexError, ValueError):
        return None


_CLARIFY_ACTIONS = {"choice", "select", "input_submit", "button_submit", "retry_submit"}


def _clarify_action_card(adapter, event, action: str, value: dict) -> tuple[dict | None, str | None]:
    """Validate the clicked prompt and resolve it before reporting success."""
    cid = str(value.get("clarify_id") or "")
    helpers = _clarify_host_helpers()
    validate = _clarify_host_validator(adapter)
    if not cid or action not in _CLARIFY_ACTIONS or helpers is None or validate is None:
        return None, None
    refuse, refusal_text, get_pending, resolve = helpers
    native_state = getattr(adapter, "_clarify_state", None)
    with _clarify_lock:
        if isinstance(native_state, dict):
            state = native_state.get(cid)
        else:
            owner, state = _legacy_clarify_prompts.get(cid, (None, None))
            if owner is not adapter:
                state = None
        choices = list(_clarify_choices.get(cid, []))
        previous_selection = _clarify_selections.get(cid, "")
    if not state:
        return None, None

    context = getattr(event, "context", None)
    message_id = getattr(context, "open_message_id", None)
    if not message_id or not state.get("chat_id") or getattr(context, "open_chat_id", None) != state["chat_id"]:
        return None, None
    if expected_message_id := state.get("message_id"):
        if message_id != expected_message_id:
            return None, None
    elif not state.get("callback_token") or value.get("clarify_token") != state["callback_token"]:
        # A lost send acknowledgement leaves no message ID to match. The
        # token in this app's signed callback identifies the rendered prompt.
        return None, None
    checked = validate(event=event, state=state, label="clarify", ident=cid)
    if checked is None:
        return None, None
    open_id, _chat_id, _user_name, user_id, _union_id = checked
    entry = get_pending(cid)
    if entry is not None and entry.session_key != state.get("session_key"):
        return None, None
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if requester := refuse(cid, user_id or open_id, extra=extra):
        return None, refusal_text(requester)

    from ..cardkit.special import _normalize_choice, build_clarify_confirmed_card, build_clarify_expired_card

    selected = label = None
    if entry is not None:
        action_obj = getattr(event, "action", None)
        if action in {"choice", "select"}:
            selected = _clarify_choice_text(action, value, action_obj, choices)
            label = _normalize_choice(selected)
        elif action == "input_submit":
            selected = str(getattr(action_obj, "input_value", "") or "").strip()
        elif action == "button_submit":
            values = getattr(action_obj, "form_value", None)
            if isinstance(values, dict):
                selected = str(values.get("clarify_input", "") or "").strip()
        else:
            selected = previous_selection
        if not selected:
            return None, None

    # Retire this card's callback state before waking the waiter. If a typed
    # response won between the lookup and the resolve, return an expired card.
    with _clarify_lock:
        if isinstance(native_state, dict):
            if native_state.pop(cid, None) is not state:
                return None, None
        elif _legacy_clarify_prompts.get(cid) != (adapter, state):
            return None, None
        _clear_clarify_locked(cid)
    question = state.get("question", "")
    if selected is None or not resolve(cid, selected):
        return build_clarify_expired_card(question=question), None
    _logger.info("clarify card: resolved action=%s id=%s", action, cid[:12])
    return build_clarify_confirmed_card(question=question, selected=label or selected), None


def _wrap_native_clarify_card_action(original_method: Callable) -> Callable:
    """Handle plugin controls at the host's synchronous dispatch target.

    The SDK may retain an old bound _on_card_action_trigger, but that callback
    looks up this method on self. Native numeric/other buttons still belong to
    Hermes; only the plugin's controls are intercepted here.
    """
    def _wrapped(self, *, event, action_value, loop):
        action = action_value.get("hermes_clarify_action")
        if not isinstance(action, str) or action not in _CLARIFY_ACTIONS:
            return original_method(self, event=event, action_value=action_value, loop=loop)
        card, refusal = _clarify_action_card(self, event, action, action_value)
        if refusal:
            return self._card_toast(refusal)
        return self._card_response(card)

    return _wrapped


def _safe_action_value_repr(action_value: Any) -> str:
    """Safely repr an action_value dict for logging (truncated, no secrets)."""
    try:
        import json
        s = json.dumps(action_value, ensure_ascii=False, default=str)
        return s[:200]
    except Exception:
        return repr(action_value)[:200]

def _coerce_action_bool(value: Any, *, default: bool) -> bool:
    """卡片回调里的布尔值。

    飞书回传 ``action.value`` 时不保证保留 JSON 类型，可能把 ``false`` 变成
    字符串 ``"false"``；直接 ``bool("false")`` 会得到 True，按钮就只能展开不能收起。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    return default


def _wrap_handle_card_action_event(original_method: Callable) -> Callable:
    """Wrap ``FeishuAdapter._handle_card_action_event`` — the REAL interception point."""

    async def _wrapped(self, data):
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}

        # 老大 2026-09-20: 思考块「展开全部 / 收起」按钮
        _hls_action = action_value.get("hls_action") if isinstance(action_value, dict) else None
        if _hls_action == "reasoning_toggle":
            try:
                from ..controller import get_controller
                _ctrl = get_controller()
                _ctx = getattr(event, "context", None)
                _mid = ""
                for _attr in ("open_message_id", "message_id"):
                    _v = getattr(_ctx, _attr, None) if _ctx is not None else None
                    if isinstance(_v, str) and _v:
                        _mid = _v
                        break
                _expand = _coerce_action_bool(action_value.get("expanded"), default=True)
                await _ctrl.on_reasoning_toggle(card_msg_id=_mid, expanded=_expand)
                _logger.info("HLS: reasoning toggle action card_msg=%s expanded=%s", (_mid or "?")[:12], _expand)
            except Exception:
                _logger.warning("HLS: reasoning toggle action failed", exc_info=True)
            return  # suppress /card synthetic command generation
        clarify_action = (
            action_value.get("hermes_clarify_action")
            if isinstance(action_value, dict) else None
        )

        if isinstance(clarify_action, str) and clarify_action in _CLARIFY_ACTIONS:
            # didn't run (SDK holds stale bound method). Handle clarify resolution
            _cid = str(action_value.get("clarify_id") or "")
            _logger.info(
                "HLS: clarify card action %r reached _handle_card_action_event "
                "(SDK stale bound method path — _on_card_action_trigger wrapper "
                "bypassed), handling clarify resolution here, clarify_id=%s",
                clarify_action,
                (_cid or "?")[:12],
            )
            try:
                # The legacy SDK entry point has already acknowledged the click.
                # The returned card must be persisted by message ID on this path.
                response = _handle_clarify_card_action(self, data, clarify_action, action_value)
                card = getattr(getattr(response, "card", None), "data", None)
                if card is not None:
                    await _update_interactive_result(self, event.context.open_message_id, card)
            except Exception:
                _logger.warning(
                    "HLS: clarify card action handling in _handle_card_action_event "
                    "failed — clarify_id=%s",
                    (_cid or "?")[:12],
                    exc_info=True,
                )
            return  # suppress /card synthetic command generation

        # which Gateway rejects ("Unknown command /card"). Note: hermes_action /
        try:
            action_tag = str(getattr(action, "tag", "") or "button")
        except Exception:
            action_tag = "button"
        _logger.warning(
            "HLS: card action %r reached _handle_card_action_event — suppressing "
            "/card synthetic command (gateway would reject it). action_value=%s",
            action_tag,
            _safe_action_value_repr(action_value),
        )
        return  # suppress

    return _wrapped

def _handle_clarify_card_action(
    adapter_instance,
    data: Any,
    clarify_action: str,
    action_value: dict,
) -> Any:
    """Build the SDK response for a clarify action routed through the legacy entry point."""
    card_data, refusal = _clarify_action_card(adapter_instance, getattr(data, "event", None),
                                             clarify_action, action_value)
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            CallBackToast,
            P2CardActionTriggerResponse,
        )
    except ImportError:
        return None
    response = P2CardActionTriggerResponse()
    if card_data is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = card_data
        response.card = card
    elif refusal:
        toast = CallBackToast()
        toast.type = "warning"
        toast.content = refusal
        response.toast = toast
    return response
