"""异步卡片 API 编排 — 创建、更新、完成卡片的重试/降级逻辑."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..cardkit import (
    build_cron_cards,
    build_gateway_card,
    build_gateway_cards,
)
from ..cardkit.budget import card_fits
from ..cardkit.special import _GATEWAY_STATUS_ELEMENT_ID
from ..feishu import MessageDeliveryUncertain

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


class PartialCardDelivery(RuntimeError):
    """Earlier pages are already visible. Only the remainder may fall back to text."""

    def __init__(self, remaining: str) -> None:
        super().__init__("static card delivery stopped after sending at least one page")
        self.remaining = remaining


class ControllerMixin:
    """异步卡片 API 操作 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[[], Coroutine[Any, Any, None]]
    _cleanup: Callable[[str], None]

    def _remember_gateway_card(
        self, card_msg_id: str, chat_id: str, card: dict, category: str,
        *, continuation_ids: list[str],
    ) -> None:
        """Keep the sent payload for status edits without risking a second send."""
        try:
            from ..patching.adapter import _register_gateway_card

            _register_gateway_card(
                card_msg_id, chat_id=chat_id, card_id=None, category=category,
                card=card, continuation_ids=continuation_ids,
            )
        except Exception:
            _logger.debug("gateway card snapshot unavailable", exc_info=True)

    async def _send_static_pages(self, chat_id: str, pages: list[tuple[str, dict]]) -> list[str]:
        assert self._client is not None
        sent: list[str] = []
        for index, (_, card) in enumerate(pages):
            try:
                if not card_fits(card):
                    raise ValueError("static card page exceeds the card budget")
                sent.append(await self._client.send_card_to_chat(chat_id, card))
            except MessageDeliveryUncertain:
                raise
            except Exception as exc:
                if not sent:
                    raise
                raise PartialCardDelivery("".join(source for source, _ in pages[index:])) from exc
        return sent

    async def _do_cron_deliver(self, chat_id: str, content: str) -> None:
        _logger.info("cron _do_cron_deliver: chat=%s content_len=%d", chat_id[:12], len(content))
        await self._ensure_init()
        await self._send_static_pages(chat_id, build_cron_cards(content))

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
            pages = build_gateway_cards(content, category=category)
            card_msg_id, *continuation_ids = await self._send_static_pages(chat_id, pages)
            self._remember_gateway_card(
                card_msg_id, chat_id, pages[0][1], category, continuation_ids=continuation_ids,
            )
            _logger.info(
                "gateway card delivered: chat=%s category=%s card_msg_id=%s "
                "content_len=%d",
                chat_id[:12],
                category or "system",
                card_msg_id[:12] if card_msg_id else None,
                len(content),
            )
            return card_msg_id, None  # No card_id for static gateway cards
        except (PartialCardDelivery, MessageDeliveryUncertain):
            raise
        except Exception:
            _logger.warning("gateway card delivery failed, caller should fall back", exc_info=True)
            return None, None

    @asynccontextmanager
    async def _gateway_card_transaction(self, card_msg_id: str) -> AsyncIterator[dict | None]:
        from ..patching import _gateway_cards, _gateway_cards_lock

        with _gateway_cards_lock:
            info = _gateway_cards.get(card_msg_id)
            if info is not None and "write_lock" not in info:
                info["write_lock"] = asyncio.Lock()
        if info is None:
            yield None
            return
        async with info["write_lock"]:
            with _gateway_cards_lock:
                current = _gateway_cards.get(card_msg_id)
            yield info if current is info else None

    async def _do_gateway_card_update(
        self,
        *,
        chat_id: str,
        card_msg_id: str,
        card_id: str | None = None,
        content: str,
        category: str = "",
        delete_message: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> bool:
        """Update a gateway card's content (called from edit_message interception)."""
        try:
            await self._ensure_init()
            assert self._client is not None
            async with self._gateway_card_transaction(card_msg_id) as info:
                if info is None:
                    return False
                status_label, status_emoji = info.get("status", ("", ""))
                pages = build_gateway_cards(
                    content, category=category, status_label=status_label, status_emoji=status_emoji,
                )
                if not all(card_fits(card) for _, card in pages):
                    return False
                siblings = info.setdefault("continuation_ids", [])
                wanted_siblings = len(pages) - 1
                if len(siblings) > wanted_siblings and delete_message is None:
                    return False

                card = pages[0][1]
                await self._update_gateway_card(
                    card, card_id=info.get("card_id") or card_id, card_msg_id=card_msg_id,
                )
                info.update(card=card, category=category)

                for i, (_, page) in enumerate(pages[1:]):
                    if i < len(siblings):
                        await self._update_gateway_card(page, card_id=None, card_msg_id=siblings[i])
                    else:
                        siblings.append(await self._client.send_card_to_chat(chat_id, page))
                while len(siblings) > wanted_siblings:
                    assert delete_message is not None
                    if not await delete_message(chat_id, siblings[-1]):
                        return False
                    siblings.pop()
                return True
        except Exception:
            _logger.debug("gateway card content update failed", exc_info=True)
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
        clear_emoji: str = "",
    ) -> bool:
        """Update a gateway card's status indicator (from reaction interception)."""
        try:
            await self._ensure_init()
            assert self._client is not None
            async with self._gateway_card_transaction(card_msg_id) as info:
                saved_card = info.get("card") if info is not None else None
                if not isinstance(saved_card, dict):
                    return False  # Let the original reaction handle unknown cards.
                assert info is not None
                _label, active_emoji = info.get("status", ("", ""))
                if clear_emoji and active_emoji != clear_emoji:
                    return False  # Let Hermes remove the actual reaction if it was added there.
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
                if not card_fits(card):
                    return False
                if card != saved_card:
                    await self._update_gateway_card(
                        card, card_id=info.get("card_id") or card_id, card_msg_id=card_msg_id,
                    )
                info.update(card=card, status=(status_label, emoji))
            _logger.info(
                "gateway card status: card_msg_id=%s status=%s emoji=%s",
                card_msg_id[:12],
                status_label or "(cleared)",
                emoji or "(none)",
            )
            return True
        except Exception:
            return False
