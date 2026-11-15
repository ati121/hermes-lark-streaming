"""异步卡片 API 编排 — 创建、更新、完成卡片的重试/降级逻辑."""

from __future__ import annotations

import logging
from copy import deepcopy
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..cardkit import (
    build_cron_card,
    build_gateway_card,
)

# ── Phase constants — single source of truth ─────────────────────────
from ..state.phase import (
    CardPhase,
    _TERMINAL,
)

IDLE = CardPhase.IDLE
CREATING = CardPhase.CREATING
STREAMING = CardPhase.STREAMING
COMPLETING = CardPhase.COMPLETING
COMPLETED = CardPhase.COMPLETED
CREATION_FAILED = CardPhase.CREATION_FAILED
ABORTED = CardPhase.ABORTED
TERMINATED = CardPhase.TERMINATED

if TYPE_CHECKING:
    from ..config import Config
    from ..feishu import FeishuClient

_logger = logging.getLogger("hermes_lark_streaming")

__all__ = [
    "ControllerMixin",
    "IDLE",
    "CREATING",
    "STREAMING",
    "COMPLETING",
    "COMPLETED",
    "CREATION_FAILED",
    "TERMINATED",
    "ABORTED",
    "_TERMINAL",
]

class ControllerMixin:
    """异步卡片 API 操作 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[[], Coroutine[Any, Any, None]]
    _cleanup: Callable[[str], None]

    def _remember_gateway_card(
        self, card_msg_id: str, chat_id: str, card: dict, category: str,
        *, card_id: str | None = None,
    ) -> None:
        """Keep the sent payload for status edits without risking a second send."""
        try:
            from ..patching.adapter import _register_gateway_card

            _register_gateway_card(
                card_msg_id, chat_id=chat_id, card_id=card_id, category=category, card=card,
            )
        except Exception:
            _logger.debug("gateway card snapshot unavailable", exc_info=True)

    async def _do_cron_deliver(self, chat_id: str, content: str) -> None:
        _logger.info("cron _do_cron_deliver: chat=%s content_len=%d", chat_id[:12], len(content))
        await self._ensure_init()
        assert self._client is not None
        card = build_cron_card(content)
        await self._client.send_card_to_chat(chat_id, card)

    async def _do_gateway_deliver(
        self,
        chat_id: str,
        content: str,
        *,
        category: str = "",
    ) -> tuple[str | None, str | None]:
        """Send a gateway-internal message as a card."""
        try:
            await self._ensure_init()
            assert self._client is not None
            card = build_gateway_card(
                content,
                category=category,
            )
            # Use send_card_to_chat which returns card_msg_id
            card_msg_id = await self._client.send_card_to_chat(chat_id, card)
            self._remember_gateway_card(card_msg_id, chat_id, card, category)
            _logger.info(
                "gateway card delivered: chat=%s category=%s card_msg_id=%s "
                "content_len=%d",
                chat_id[:12],
                category or "system",
                card_msg_id[:12] if card_msg_id else None,
                len(content),
            )
            return card_msg_id, None  # No card_id for static gateway cards
        except Exception:
            _logger.warning("gateway card delivery failed, caller should fall back", exc_info=True)
            return None, None

    async def _do_gateway_card_update(
        self,
        *,
        chat_id: str,
        card_msg_id: str,
        card_id: str | None = None,
        content: str,
        category: str = "",
    ) -> bool:
        """Update a gateway card's content (called from edit_message interception)."""
        try:
            await self._ensure_init()
            assert self._client is not None
            card = build_gateway_card(content, category=category)
            await self._update_gateway_card(card, card_id=card_id, card_msg_id=card_msg_id)
            self._remember_gateway_card(card_msg_id, chat_id, card, category, card_id=card_id)
            _logger.info(
                "gateway card updated (%s): card_id=%s card_msg_id=%s category=%s",
                "cardkit" if card_id else "im_patch",
                (card_id or "?")[:12],
                card_msg_id[:12],
                category,
            )
            return True
        except Exception:
            return False

    async def _update_gateway_card(self, card: dict, *, card_id: str | None, card_msg_id: str) -> None:
        """CardKit 容器走 cardkit_update，否则 IM PATCH."""
        assert self._client is not None
        if card_id:
            await self._client.cardkit_update(card_id, card)
        else:
            await self._client.update_card(card_msg_id, card)

    async def _do_gateway_card_status(
        self,
        *,
        card_msg_id: str,
        card_id: str | None = None,
        status_label: str,
        emoji: str,
        category: str = "",
    ) -> bool:
        """Update a gateway card's status indicator (from reaction interception)."""
        try:
            await self._ensure_init()
            assert self._client is not None
            from ..cardkit.special import _GATEWAY_STATUS_ELEMENT_ID
            from ..patching import _gateway_cards, _gateway_cards_lock

            with _gateway_cards_lock:
                info = _gateway_cards.get(card_msg_id, {})
                saved_card = info.get("card")
                if not isinstance(saved_card, dict):
                    return False  # Let the original reaction handle unknown cards.
                card = deepcopy(saved_card)
            body = card.get("body")
            if not isinstance(body, dict) or not isinstance(body.get("elements"), list):
                return False
            elements = [
                element for element in body["elements"]
                if element.get("element_id") != _GATEWAY_STATUS_ELEMENT_ID
            ]
            if status_label and emoji:
                elements[:0] = build_gateway_card(
                    "", status_label=status_label, status_emoji=emoji,
                )["body"]["elements"]
            body["elements"] = elements
            await self._update_gateway_card(card, card_id=card_id, card_msg_id=card_msg_id)
            with _gateway_cards_lock:
                if _gateway_cards.get(card_msg_id) is info:
                    info["card"] = card
            _logger.info(
                "gateway card status: card_msg_id=%s status=%s emoji=%s",
                card_msg_id[:12],
                status_label or "(cleared)",
                emoji or "(none)",
            )
            return True
        except Exception:
            return False
