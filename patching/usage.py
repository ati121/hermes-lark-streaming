"""Measure provider decode speed before chunks reach UI callbacks."""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from importlib import import_module
from threading import Lock
from time import monotonic
from typing import Any

from ..runtime_globals import shared_object

_logger = logging.getLogger("hermes_lark_streaming")
_API_METHODS = ("_interruptible_api_call", "_interruptible_streaming_api_call")
_active_calls: ContextVar[tuple[int, ...]] = ContextVar("hls_speed_calls", default=())
_stream_timing = shared_object(
    "speed.stream_timing", lambda: ContextVar("hls_stream_timing", default=None),
)
_observer_lock = shared_object("speed.observer_lock", Lock)
_session_speed = shared_object("speed.session_totals", OrderedDict)
_session_speed_lock = shared_object("speed.session_totals.lock", Lock)
_SESSION_SPEED_LIMIT = 64
_MIN_DECODE_SECONDS = 1.0
_MAX_DECODE_TPS = 10000.0


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _call_role(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Read Hermes' provider call role without depending on one argument shape."""
    sources = [kwargs]
    if args:
        sources.insert(0, args[0])
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        role = source.get("call_role")
        if role:
            return str(role)
        metadata = source.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("call_role"):
            return str(metadata["call_role"])
    return ""


def _is_auxiliary_role(role: str) -> bool:
    """Auxiliary work must never enter the main-model speed sample."""
    value = role.strip().lower()
    return (
        value == "auxiliary"
        or value.startswith(("auxiliary:", "auxiliary.", "compression:", "compaction:"))
        or value in ("compression", "compaction")
    )


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
    """Include reasoning exactly once in the provider's total output."""
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


def _text_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_text_size(_field(part, "text")) for part in value)
    return 0


def _chunk_sizes(chunk: Any) -> tuple[int, int, int]:
    """Return text/reasoning/tool sizes without retaining response content."""
    text = reasoning = tool = 0
    for choice in _field(chunk, "choices") or ():
        delta = _field(choice, "delta")
        text += _text_size(_field(delta, "content"))
        reasoning += _text_size(
            _field(delta, "reasoning_content") or _field(delta, "reasoning")
            or _field(delta, "reasoning_text")
        )
        for call in _field(delta, "tool_calls") or ():
            function = _field(call, "function")
            tool += _text_size(_field(function, "name")) + _text_size(_field(function, "arguments"))
    kind = _field(chunk, "type")
    if kind == "content_block_start":
        block = _field(chunk, "content_block")
        if _field(block, "type") == "text":
            text += _text_size(_field(block, "text"))
        elif _field(block, "type") == "thinking":
            reasoning += _text_size(_field(block, "thinking"))
        elif _field(block, "type") == "tool_use":
            tool += _text_size(_field(block, "name"))
    elif kind == "content_block_delta":
        delta = _field(chunk, "delta")
        text += _text_size(_field(delta, "text"))
        reasoning += _text_size(_field(delta, "thinking"))
        tool += _text_size(_field(delta, "partial_json"))
    elif kind == "response.output_text.delta":
        text += _text_size(_field(chunk, "delta"))
    elif kind == "response.function_call_arguments.delta":
        tool += _text_size(_field(chunk, "delta"))
    return text, reasoning, tool


@dataclass
class StreamTiming:
    session_id: str
    first: float | None = None
    ended: float | None = None
    # Keep sizes/times only, bounded independently of a long stream's chunk count.
    events: list[tuple[float, int, int, int]] = field(default_factory=list)
    overflow: bool = False
    streams: int = 0

    def observe(self, chunk: Any) -> None:
        sizes = _chunk_sizes(chunk)
        if not any(sizes):
            return
        now = monotonic()
        if self.first is None:
            self.first = now
        if len(self.events) >= 65536:
            self.overflow = True
            return
        self.events.append((now, *sizes))

    def decode(self, usage: dict | None) -> tuple[int | None, float]:
        output = _output_tokens(usage)
        if output is None or self.first is None or self.ended is None or self.overflow or self.streams != 1:
            return None, 0.0
        reasoning = _count((usage or {}).get("reasoning_tokens")) or 0
        first_text = next((at for at, text, _, _ in self.events if text), None)
        first_reasoning = next((at for at, _, think, _ in self.events if think), None)
        start = self.first
        events = self.events
        if reasoning:
            if reasoning > output:
                return None, 0.0
            if first_text is not None:
                output -= reasoning
                start = first_text
                events = [(at, text, 0, 0) for at, text, _, _ in self.events if text]
            elif first_reasoning is not None:
                start = first_reasoning
            else:
                return None, 0.0
        if output <= 0:
            return None, 0.0
        seconds = self.ended - start
        if seconds <= _MIN_DECODE_SECONDS or output > _MAX_DECODE_TPS * seconds:
            return None, 0.0
        # Magpie's 10%-90% content flow detects a buffered burst, not decode.
        size = sum(text + think + tool for _, text, think, tool in events)
        lower = upper = None
        consumed = 0
        for at, text, think, tool in events:
            consumed += text + think + tool
            if lower is None and consumed >= size * 0.1:
                lower = at
            if upper is None and consumed >= size * 0.9:
                upper = at
        flow = (upper - lower) / 0.8 if lower is not None and upper is not None else 0.0
        if flow <= 0 or flow * 4 < seconds:
            return None, 0.0
        return output, seconds


class _ObservedStream:
    """Transparent stream proxy; closes and yields exactly as the host would."""

    def __init__(self, stream: Any, timing: StreamTiming):
        self._stream = stream
        self._iterator = iter(stream)
        self._timing = timing
        timing.streams += 1

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._timing.ended = monotonic()
            raise
        try:
            self._timing.observe(chunk)
        except Exception:  # Observability must never interrupt a provider stream.
            self._timing.overflow = True
            _logger.debug("HLS: provider chunk timing unavailable", exc_info=True)
        return chunk


def _install_stream_observer() -> None:
    with _observer_lock:
        _install_stream_observer_locked()


def _install_stream_observer_locked() -> None:
    try:
        relay = import_module("agent.relay_llm")
    except ImportError:
        return  # Older hosts cannot measure provider decode reliably.
    original = getattr(relay, "stream", None)
    if not callable(original) or getattr(original, "_hls_speed_observer", False):
        return

    @wraps(original)
    def stream(request, stream_factory, **kwargs):
        timing = _stream_timing.get()
        role = _call_role((), kwargs)
        if (
            timing is None
            or str(kwargs.get("session_id") or "") != timing.session_id
            or _is_auxiliary_role(role)
        ):
            return original(request, stream_factory, **kwargs)

        def observed_factory(next_request):
            raw = stream_factory(next_request)
            # Some providers ignore stream=True and return a completed object.
            predicate = kwargs.get("completed_response_predicate")
            if predicate is not None and predicate(raw):
                return raw
            return _ObservedStream(raw, timing)

        return original(request, observed_factory, **kwargs)

    stream._hls_speed_observer = True
    relay.stream = stream


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

    def reset(self) -> None:
        with self._lock:
            self.requests = 0
            self.measured = 0
            self.output_tokens = 0
            self.seconds = 0.0

    def record(self, output: int | None, seconds: float) -> None:
        with self._lock:
            if output is None or not math.isfinite(seconds) or seconds <= 0:
                return
            self.measured += 1
            self.output_tokens += output
            self.seconds += seconds

    def footer(self, prefix: str = "speed") -> dict[str, int | float]:
        with self._lock:
            result: dict[str, int | float] = {
                f"{prefix}_requests": self.measured,
                f"{prefix}_total_requests": self.requests,
            }
            # Keep every measured request in both sums. The renderer labels a
            # subset explicitly instead of hiding the entire rate.
            if self.measured:
                result.update(
                    **{
                        f"{prefix}_output_tokens": self.output_tokens,
                        f"{prefix}_request_seconds": self.seconds,
                    },
                )
            return result


@dataclass
class SessionSpeedTotals:
    """Lifetime and post-compaction windows for one Hermes session."""

    session: RequestTotals = field(default_factory=RequestTotals)
    since_compaction: RequestTotals = field(default_factory=RequestTotals)


def _totals_for_context(ctx: dict) -> RequestTotals:
    return ctx.setdefault("_speed_totals", RequestTotals())


def _session_speed_for(session_id: str) -> SessionSpeedTotals | None:
    if not session_id:
        return None
    with _session_speed_lock:
        totals = _session_speed.get(session_id)
        if totals is None:
            totals = SessionSpeedTotals()
            _session_speed[session_id] = totals
            while len(_session_speed) > _SESSION_SPEED_LIMIT:
                _session_speed.popitem(last=False)
        else:
            _session_speed.move_to_end(session_id)
        return totals


def _attach_session_speed(ctx: dict, session_id: str) -> None:
    """Share one lifetime accumulator across every top-level turn."""
    totals = _session_speed_for(str(session_id or ""))
    if totals is None:
        return
    ctx["_speed_session_totals"] = totals.session
    ctx["_speed_compaction_totals"] = totals.since_compaction


def _reset_compaction_speed(session_id: str) -> None:
    """Start a fresh recent window without discarding lifetime totals."""
    key = str(session_id or "")
    if not key:
        return
    with _session_speed_lock:
        totals = _session_speed.get(key)
    if totals is not None:
        totals.since_compaction.reset()


def _clear_session_speed(session_id: str | None = None) -> None:
    """Drop one or every session accumulator; used by reset paths and tests."""
    with _session_speed_lock:
        if session_id is None:
            _session_speed.clear()
        else:
            _session_speed.pop(str(session_id), None)


def _speed_targets(ctx: dict | None, agent: Any) -> tuple[RequestTotals, ...]:
    if ctx is not None:
        values = (
            _totals_for_context(ctx),
            ctx.get("_speed_session_totals"),
            ctx.get("_speed_compaction_totals"),
        )
    else:
        values = (
            getattr(agent, "_hls_speed_totals", None),
            getattr(agent, "_hls_speed_session_totals", None),
            getattr(agent, "_hls_speed_compaction_totals", None),
        )
    targets: list[RequestTotals] = []
    seen: set[int] = set()
    for value in values:
        if not isinstance(value, RequestTotals) or id(value) in seen:
            continue
        seen.add(id(value))
        targets.append(value)
    return tuple(targets)


def _speed_footer(ctx: dict) -> dict[str, int | float]:
    turn = ctx.get("_speed_totals")
    session = ctx.get("_speed_session_totals")
    recent = ctx.get("_speed_compaction_totals")

    if isinstance(session, RequestTotals) and session.requests:
        primary = session
        scope = "session"
    elif isinstance(turn, RequestTotals):
        primary = turn
        scope = "turn"
    else:
        return {}

    result = primary.footer("speed")
    if isinstance(turn, RequestTotals) and turn is not primary:
        result["speed_api_requests"] = turn.requests
        result.update(turn.footer("speed_turn"))
    if (
        isinstance(recent, RequestTotals)
        and recent is not primary
        and recent is not turn
        and recent.requests
    ):
        result.update(recent.footer("speed_recent"))

    measured = int(result.get("speed_requests", 0) or 0)
    requests = int(result.get("speed_total_requests", 0) or 0)
    if measured != requests:
        _logger.info(
            "HLS: speed partial scope=%s measured=%s requests=%s",
            scope, measured, requests,
        )
    else:
        _logger.info(
            "HLS: speed measured scope=%s requests=%s output=%s decode_seconds=%.3f",
            scope, measured, result.get("speed_output_tokens", 0),
            result.get("speed_request_seconds", 0.0),
        )
    return result


def _wrap_api_usage(agent: Any, original: Callable) -> Callable:
    @wraps(original)
    def wrapper(*args, **kwargs):
        from . import _msg_ctx

        if getattr(agent, "_persist_disabled", False):
            return original(*args, **kwargs)
        role = _call_role(args, kwargs)
        if _is_auxiliary_role(role):
            _logger.debug("HLS: speed skip auxiliary role=%s", role)
            return original(*args, **kwargs)
        ctx = _msg_ctx.get()
        targets = _speed_targets(ctx, agent)
        if not targets:
            return original(*args, **kwargs)
        active = _active_calls.get()
        if id(agent) in active:
            return original(*args, **kwargs)
        token = _active_calls.set((*active, id(agent)))
        timing = StreamTiming(str(getattr(agent, "session_id", None) or ""))
        timing_token = _stream_timing.set(timing)
        for totals in targets:
            totals.start()
        try:
            response = original(*args, **kwargs)
            output, seconds = None, 0.0
            try:
                output, seconds = timing.decode(_snapshot_usage(_field(response, "usage")))
            except Exception:
                _logger.debug("HLS: response usage unavailable for speed", exc_info=True)
            for totals in targets:
                totals.record(output, seconds)
            _logger.debug(
                "HLS: speed request measured=%s output=%s decode_seconds=%.3f streams=%d events=%d",
                output is not None, output, seconds, timing.streams, len(timing.events),
            )
            return response
        except BaseException:
            for totals in targets:
                totals.record(None, 0.0)
            raise
        finally:
            _stream_timing.reset(timing_token)
            _active_calls.reset(token)

    wrapper._hls_usage_wrapper = True
    return wrapper


def _maybe_wrap_api_usage(agent: Any, totals: RequestTotals) -> None:
    _install_stream_observer()
    methods = [(name, getattr(agent, name, None)) for name in _API_METHODS]
    methods = [(name, original) for name, original in methods if callable(original)]
    if not methods:
        return
    # Repeated setup in the same turn must not discard earlier tool rounds.
    agent._hls_speed_totals = totals
    for name, original in methods:
        if not getattr(original, "_hls_usage_wrapper", False):
            setattr(agent, name, _wrap_api_usage(agent, original))
