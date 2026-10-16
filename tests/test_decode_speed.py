"""Provider-side decode timing, independently of gateway/display latency."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from hermes_lark_streaming.cardkit.elements import _render_footer_field
from hermes_lark_streaming.patching import _maybe_wrap_callbacks, _msg_ctx
from hermes_lark_streaming.patching.usage import (
    StreamTiming,
    _chunk_sizes,
    _ObservedStream,
    _speed_footer,
)


def _usage(output, reasoning=0):
    return {
        "output_tokens": output, "reasoning_tokens": reasoning,
        "prompt_tokens": 100, "total_tokens": 100 + output,
    }


def _timing(events, end):
    return StreamTiming("synthetic", first=events[0][0], ended=end, events=events, streams=1)


def test_decode_excludes_prefill_and_host_teardown():
    # Request started at zero, first text at 8s, provider EOF at 10s;
    # UI callbacks and the host's monitoring thread may return at 12s.
    timing = _timing([(8, 10, 0, 0), (9, 10, 0, 0), (10, 10, 0, 0)], 10)
    assert timing.decode(_usage(400)) == (400, 2)


@pytest.mark.parametrize(("events", "end", "usage", "expected"), [
    ([(1, 0, 10, 0), (8, 10, 0, 0), (9, 10, 0, 0), (10, 10, 0, 0)], 10, _usage(1500, 1100), (400, 2)),
    ([(1, 0, 10, 0), (3, 0, 10, 0), (5, 0, 0, 10)], 5, _usage(800, 700), (800, 4)),
    ([(1, 0, 0, 10), (3, 0, 0, 10), (5, 0, 0, 10)], 5, _usage(800), (800, 4)),
    ([(1, 0, 0, 10), (3, 0, 0, 10), (5, 0, 0, 10)], 5, _usage(800, 700), (None, 0)),
    ([(8, 10, 0, 0)], 8.5, _usage(800), (None, 0)),
    ([(1, 1, 0, 0), (9, 99, 0, 0)], 10, _usage(800), (None, 0)),
    ([(1, 0, 10, 0), (11, 50, 0, 0), (11.2, 50, 0, 0)], 11.2, _usage(550, 20), (None, 0)),
    ([(1, 10, 0, 0), (1.05, 10, 0, 0)], 1.05, _usage(100), (None, 0)),
    ([(1, 10, 0, 0), (2, 10, 0, 0)], 2, _usage(10001), (None, 0)),
])
def test_reasoning_tool_and_buffered_streams(events, end, usage, expected):
    assert _timing(events, end).decode(usage) == expected


@pytest.mark.parametrize(("chunk", "sizes"), [
    ({"choices": [{"delta": {"role": "assistant"}}]}, (0, 0, 0)),
    ({"usage": {"completion_tokens": 100}}, (0, 0, 0)),
    ({"choices": [{"delta": {"reasoning_content": "think"}}]}, (0, 5, 0)),
    ({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "abcd"}}]}}]}, (0, 0, 4)),
    ({"type": "content_block_delta", "delta": {"thinking": "abc"}}, (0, 3, 0)),
    ({"type": "content_block_delta", "delta": {"partial_json": "{}"}}, (0, 0, 2)),
    ({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "read"}}, (0, 0, 4)),
    ({"type": "response.output_text.delta", "delta": "abcd"}, (4, 0, 0)),
])
def test_transport_shapes(chunk, sizes):
    assert _chunk_sizes(chunk) == sizes


def test_stream_proxy_preserves_identity_close_and_provider_exception():
    chunk = {"choices": [{"delta": {"content": "text"}}]}
    failure = RuntimeError("synthetic disconnect")

    class Raw:
        final_response = None
        closed = False

        def __iter__(self):
            yield chunk
            raise failure

        def close(self):
            self.closed = True

    raw = Raw()
    observed = _ObservedStream(raw, StreamTiming("synthetic"))
    assert observed.final_response is None
    assert next(observed) is chunk
    with pytest.raises(RuntimeError) as caught:
        next(observed)
    assert caught.value is failure
    observed.close()
    assert raw.closed


def test_request_with_multiple_raw_attempts_does_not_mix_usage():
    timing = _timing([(1, 10, 0, 0), (2, 10, 0, 0)], 2)
    timing.streams = 2
    assert timing.decode(_usage(100)) == (None, 0)


def test_provider_measurement_survives_executor_prefetch_and_ui_delay(monkeypatch):
    # This uses the same Relay factory seam as Hermes: raw iteration is in a
    # context-copied worker, and all UI delivery happens AFTER it was consumed.
    clock = [0.0]
    relay = ModuleType("agent.relay_llm")

    def stream(request, factory, **kwargs):
        chunks = copy_context().run(lambda: list(factory(request)))
        clock[0] = 12.0  # Relay post-processing / buffered display.
        return iter(chunks)

    relay.stream = stream
    monkeypatch.setitem(sys.modules, "agent.relay_llm", relay)
    ctx = {"message_id": "synthetic", "event_message_id": "synthetic"}
    token = _msg_ctx.set(ctx)
    try:
        agent = SimpleNamespace(session_id="synthetic")

        def raw(_):
            clock[0] = 1.0
            yield {"choices": [{"delta": {"role": "assistant"}}]}
            for at in (8, 9, 10):
                clock[0] = float(at)
                yield {"choices": [{"delta": {"content": "synthetic"}}]}

        def api(request, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                chunks = pool.submit(copy_context().run, lambda: list(relay.stream(
                    request, raw, session_id=agent.session_id,
                ))).result()
            assert len(chunks) == 4
            assert clock[0] == 12
            return SimpleNamespace(usage={"completion_tokens": 400})

        agent._interruptible_streaming_api_call = api
        _maybe_wrap_callbacks(agent)
        with patch("hermes_lark_streaming.patching.usage.monotonic", side_effect=lambda: clock[0]):
            agent._interruptible_streaming_api_call({})
        footer = _speed_footer(ctx)
        assert footer["speed_output_tokens"] == 400
        assert footer["speed_request_seconds"] == 2
        assert footer["speed_total_requests"] == 1
    finally:
        _msg_ctx.reset(token)


def test_api_count_uses_same_turn_accumulator_as_speed():
    data = {"api_calls": 1, "speed_total_requests": 14}
    assert _render_footer_field("api_calls", data, False, False, False)[0] == "API 14"
    assert _render_footer_field("api_calls", {"api_calls": 3}, False, False, False)[0] == "API 3"
