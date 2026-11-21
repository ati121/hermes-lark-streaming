"""Preserve Hermes' dedicated status-message delivery and update lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from . import _gateway_status_ctx


def _wrap_gateway_status_send(original: Callable) -> Callable:
    """Mark only a dedicated status coroutine, never the surrounding agent run.

    Hermes routes memory recall and retain notices through this coroutine after
    applying the provider's indicator settings and the gateway's notice policy.
    The original sender still owns status keys, adapter routing and receipts.
    """
    @wraps(original)
    async def status_send(*args: Any, **kwargs: Any) -> Any:
        token = _gateway_status_ctx.set(True)
        try:
            return await original(*args, **kwargs)
        finally:
            _gateway_status_ctx.reset(token)

    return status_send
