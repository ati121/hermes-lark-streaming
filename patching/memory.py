"""Expose automatic memory retrieval as a transient card status."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")


def _maybe_wrap_memory_prefetch(
    agent: Any, resolve_message_id: Callable[[], str | None],
) -> None:
    manager = getattr(agent, "_memory_manager", None)
    original = getattr(manager, "_prefetch_provider", None)
    if not callable(original) or getattr(original, "_hls_prefetch_wrapper", False):
        return

    @wraps(original)
    def prefetch_wrapper(provider, *args, **kwargs):
        if getattr(provider, "name", None) != "openviking":
            return original(provider, *args, **kwargs)
        try:
            message_id = resolve_message_id()
        except Exception:
            _logger.debug("HLS: memory prefetch context unavailable", exc_info=True)
            message_id = None
        if not message_id:
            return original(provider, *args, **kwargs)

        # Pin both values before the call: a cached agent can move to another
        # turn while an old request is finishing.
        request_id = object()

        def notify(active: bool) -> None:
            try:
                from .hooks import on_memory_prefetch_updated

                on_memory_prefetch_updated(
                    message_id=message_id, request_id=request_id, active=active,
                )
            except Exception:
                _logger.debug("HLS: memory prefetch status failed", exc_info=True)

        notify(True)
        try:
            # Wrap the manager's bounded wait, not the provider's daemon
            # thread: the status must end when Hermes gives up on a timeout.
            return original(provider, *args, **kwargs)
        finally:
            notify(False)

    prefetch_wrapper._hls_prefetch_wrapper = True
    try:
        manager._prefetch_provider = prefetch_wrapper
    except (AttributeError, TypeError):
        _logger.debug("HLS: memory manager cannot be wrapped", exc_info=True)
