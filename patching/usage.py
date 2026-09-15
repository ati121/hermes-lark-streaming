"""Retain provider token counts before Hermes normalizes the response usage."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")
_API_METHODS = ("_interruptible_api_call", "_interruptible_streaming_api_call")


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _snapshot_usage(usage: Any) -> dict[str, Any] | None:
    """Copy only counters, preserving the provider's original total."""
    if usage is None:
        return None
    output = _field(usage, "completion_tokens")
    prompt = _field(usage, "prompt_tokens")
    reasoning = _field(_field(usage, "completion_tokens_details"), "reasoning_tokens")
    if output is None:
        output = _field(usage, "output_tokens")
    if prompt is None:
        prompt = _field(usage, "input_tokens")
    if reasoning is None:
        reasoning = _field(_field(usage, "output_tokens_details"), "reasoning_tokens")
    if reasoning is None:
        reasoning = _field(usage, "reasoning_tokens")
    return {
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "prompt_tokens": prompt,
        "total_tokens": _field(usage, "total_tokens"),
    }


def _wrap_api_usage(agent: Any, original: Callable) -> Callable:
    @wraps(original)
    def wrapper(*args, **kwargs):
        # A failed or usage-less final call must not reuse an earlier call's
        # counters. Capture on the caller side, after Hermes's bounded wait.
        agent._hls_speed_usage = None
        response = original(*args, **kwargs)
        try:
            agent._hls_speed_usage = _snapshot_usage(_field(response, "usage"))
        except Exception:
            _logger.debug("HLS: response usage unavailable for speed", exc_info=True)
        return response

    wrapper._hls_usage_wrapper = True
    return wrapper


def _maybe_wrap_api_usage(agent: Any) -> None:
    methods = [(name, getattr(agent, name, None)) for name in _API_METHODS]
    methods = [(name, original) for name, original in methods if callable(original)]
    if not methods:
        return
    # Cached agents keep their API wrappers across turns; their counters do not.
    agent._hls_speed_usage = None
    for name, original in methods:
        if not getattr(original, "_hls_usage_wrapper", False):
            setattr(agent, name, _wrap_api_usage(agent, original))


def _speed_usage_for_agent(agent: Any) -> Any:
    # Missing capture support may use the older canonical record. An explicit
    # None from a wrapped call instead means this call has no reliable usage.
    return getattr(agent, "_hls_speed_usage", getattr(agent, "_last_turn_usage", None))
