"""Measure combined model-request throughput for one conversation turn."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from time import monotonic
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")
_API_METHODS = ("_interruptible_api_call", "_interruptible_streaming_api_call")
_active_calls: ContextVar[tuple[int, ...]] = ContextVar("hls_speed_calls", default=())


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


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _output_tokens(usage: dict | None) -> int | None:
    """Include reasoning exactly once, matching the full request duration."""
    if usage is None:
        return None
    output = _count(usage.get("output_tokens"))
    if output is None:
        return None
    reasoning = _count(usage.get("reasoning_tokens")) or 0
    prompt = _count(usage.get("prompt_tokens"))
    total = _count(usage.get("total_tokens"))
    if reasoning and prompt is not None and total == prompt + output + reasoning:
        # Some compatible providers bill reasoning separately from output.
        return output + reasoning
    return output


@dataclass
class RequestTotals:
    """Shared by context copies; an in-flight call pins its original owner."""

    requests: int = 0
    measured: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    _lock: Any = field(default_factory=Lock, repr=False)

    def start(self) -> None:
        with self._lock:
            self.requests += 1

    def record(self, output: int | None, seconds: float) -> None:
        with self._lock:
            if output is None or not math.isfinite(seconds) or seconds <= 0:
                return
            self.measured += 1
            self.output_tokens += output
            self.seconds += seconds

    def footer(self) -> dict[str, int | float]:
        with self._lock:
            result: dict[str, int | float] = {
                "speed_requests": self.measured,
                "speed_total_requests": self.requests,
            }
            # Never advertise a subset as the average of the entire turn.
            if self.requests and self.requests == self.measured:
                result.update(
                    speed_output_tokens=self.output_tokens,
                    speed_request_seconds=self.seconds,
                )
            return result


def _totals_for_context(ctx: dict) -> RequestTotals:
    return ctx.setdefault("_speed_totals", RequestTotals())


def _speed_footer(ctx: dict) -> dict[str, int | float]:
    totals = ctx.get("_speed_totals")
    if not isinstance(totals, RequestTotals):
        return {}
    result = totals.footer()
    if result.get("speed_total_requests") != result.get("speed_requests"):
        _logger.info(
            "HLS: speed hidden reason=incomplete_requests measured=%s requests=%s",
            result["speed_requests"], result["speed_total_requests"],
        )
    return result


def _wrap_api_usage(agent: Any, original: Callable) -> Callable:
    @wraps(original)
    def wrapper(*args, **kwargs):
        from . import _msg_ctx

        if getattr(agent, "_persist_disabled", False):
            return original(*args, **kwargs)
        ctx = _msg_ctx.get()
        totals = _totals_for_context(ctx) if ctx is not None else agent._hls_speed_totals
        active = _active_calls.get()
        if id(agent) in active:
            return original(*args, **kwargs)
        token = _active_calls.set((*active, id(agent)))
        started = monotonic()
        totals.start()
        output = None
        try:
            response = original(*args, **kwargs)
            ended = monotonic()
            try:
                output = _output_tokens(_snapshot_usage(_field(response, "usage")))
            except Exception:
                _logger.debug("HLS: response usage unavailable for speed", exc_info=True)
            totals.record(output, ended - started)
            return response
        except BaseException:
            totals.record(None, monotonic() - started)
            raise
        finally:
            _active_calls.reset(token)

    wrapper._hls_usage_wrapper = True
    return wrapper


def _maybe_wrap_api_usage(agent: Any, totals: RequestTotals) -> None:
    methods = [(name, getattr(agent, name, None)) for name in _API_METHODS]
    methods = [(name, original) for name, original in methods if callable(original)]
    if not methods:
        return
    # Repeated setup in the same turn must not discard earlier tool rounds.
    agent._hls_speed_totals = totals
    for name, original in methods:
        if not getattr(original, "_hls_usage_wrapper", False):
            setattr(agent, name, _wrap_api_usage(agent, original))
