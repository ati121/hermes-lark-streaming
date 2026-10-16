"""Request-boundary aggregation, turn isolation, and gateway footer delivery."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from hermes_lark_streaming.cardkit.elements import _render_footer_field
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.patching import (
    _maybe_wrap_callbacks,
    _msg_ctx,
    _session_contexts,
    _session_contexts_lock,
    _thread_local_ctx,
)
from hermes_lark_streaming.patching.gateway import (
    _wrap_run_agent,
    _wrap_run_background_task,
)
from hermes_lark_streaming.patching.usage import (
    RequestTotals,
    _maybe_wrap_api_usage,
    _output_tokens,
    _snapshot_usage,
    _speed_footer,
    _totals_for_context,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import CardPhase


def _chat_usage(output=135, reasoning=678, total=839):
    return {
        "prompt_tokens": 26, "completion_tokens": output,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": total,
    }


@pytest.fixture
def make_agent():
    token = _msg_ctx.set({"message_id": "speed-message", "event_message_id": "speed-message"})
    previous = getattr(_thread_local_ctx, "data", None)
    _thread_local_ctx.data = None

    def make(usage=None):
        agent = SimpleNamespace(
            session_id="speed-session", response=SimpleNamespace(usage=usage),
            _last_turn_usage={"output_tokens": 9999}, calls=[], during=None,
            stream_delta_callback=lambda text: None,
            interim_assistant_callback=lambda text: None,
        )

        def api_call(api_kwargs, **kwargs):
            agent.calls.append((api_kwargs, kwargs))
            if agent.during:
                agent.during()
            if isinstance(agent.response, BaseException):
                raise agent.response
            if kwargs.get("on_first_delta"):
                kwargs["on_first_delta"]()
            return agent.response

        agent._interruptible_api_call = api_call
        agent._interruptible_streaming_api_call = api_call
        return agent

    yield make
    _msg_ctx.reset(token)
    _thread_local_ctx.data = previous


def _call(agent, start, end, method="_interruptible_streaming_api_call", **kwargs):
    with patch("hermes_lark_streaming.patching.usage.monotonic", side_effect=[start, end]):
        return getattr(agent, method)({}, **kwargs)


def _speed(data):
    return _render_footer_field(
        "speed", data, is_error=False, is_aborted=False, show_label=False,
    )[0]


@pytest.mark.parametrize(("output", "reasoning", "total", "expected"), [
    (135, 678, 839, 813), (1000, 200, 1226, 1200), (135, 135, 296, 270),
    (500, 320, 526, 500), (135, 135, 161, 135), (135, 0, 161, 135),
    (135, 678, None, 135), (135, 678, 999, 135),
])
def test_output_includes_reasoning_once(output, reasoning, total, expected):
    assert _output_tokens(_snapshot_usage(_chat_usage(output, reasoning, total))) == expected


@pytest.mark.parametrize("bad", [None, "135", True, float("nan"), float("inf"), -1, 1.5])
def test_invalid_output_is_not_guessed(bad):
    assert _output_tokens(_snapshot_usage(_chat_usage(output=bad))) is None


def test_responses_and_anthropic_usage_shapes():
    usage = SimpleNamespace(
        input_tokens=1026, output_tokens=500, total_tokens=1526,
        output_tokens_details=SimpleNamespace(reasoning_tokens=320),
    )
    assert _output_tokens(_snapshot_usage(usage)) == 500
    assert _output_tokens(_snapshot_usage(SimpleNamespace(output_tokens=135))) == 135


@pytest.mark.parametrize("method", ["_interruptible_api_call", "_interruptible_streaming_api_call"])
def test_capture_preserves_response_and_provider_usage(make_agent, method):
    usage = _chat_usage()
    agent = make_agent(usage)
    totals = _totals_for_context(_msg_ctx.get())
    _maybe_wrap_api_usage(agent, totals)
    assert _call(agent, 1, 4, method) is agent.response
    assert agent.calls == [({}, {})]
    usage["completion_tokens"] = 1
    assert totals.footer()["speed_output_tokens"] == 813
    assert totals.footer()["speed_request_seconds"] == 3
    assert agent._last_turn_usage == {"output_tokens": 9999}


def test_weighted_average_includes_all_requests_not_tool_wait(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(agent)
    _call(agent, 10, 12)
    # Repeated callback setup, tools, and idle time cannot clear the accumulator.
    _maybe_wrap_callbacks(agent)
    agent.response = SimpleNamespace(usage=_chat_usage(output=900, reasoning=0, total=926))
    _call(agent, 100, 109, method="_interruptible_api_call")
    footer = _speed_footer(_msg_ctx.get())
    assert footer == {
        "speed_output_tokens": 1000, "speed_request_seconds": 11,
        "speed_requests": 2, "speed_total_requests": 2,
    }
    assert _speed(footer) == "91 t/s"  # Not mean(50, 100) = 75.


def test_cached_agent_resets_only_when_turn_changes(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(agent)
    wrapped = agent._interruptible_streaming_api_call
    first = Mock()
    with patch("hermes_lark_streaming.patching.hooks.on_model_activity") as activity:
        _call(agent, 1, 3, on_first_delta=first)
    first.assert_called_once_with()
    activity.assert_called_once()
    old_ctx = _msg_ctx.get()
    _msg_ctx.set({"message_id": "next", "event_message_id": "next"})
    _maybe_wrap_callbacks(agent)
    assert agent._interruptible_streaming_api_call is wrapped
    assert _speed(_speed_footer(_msg_ctx.get())) is None
    _call(agent, 20, 25)
    assert _speed(_speed_footer(old_ctx)) == "50 t/s"
    assert _speed(_speed_footer(_msg_ctx.get())) == "20 t/s"


@pytest.mark.parametrize("outcome", ["missing", "exception", "unreadable"])
def test_incomplete_turn_never_presents_a_subset_as_all_requests(make_agent, outcome):
    agent = make_agent(_chat_usage())
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 3)
    if outcome == "exception":
        failure = RuntimeError("provider failed")
        agent.response = failure
        with pytest.raises(RuntimeError) as raised:
            _call(agent, 4, 6)
        assert raised.value is failure
    else:
        class Unreadable:
            @property
            def usage(self):
                raise ValueError("unreadable usage")
        agent.response = Unreadable() if outcome == "unreadable" else SimpleNamespace(usage=None)
        assert _call(agent, 4, 6) is agent.response
    footer = _speed_footer(_msg_ctx.get())
    assert footer == {"speed_requests": 1, "speed_total_requests": 2}
    assert _speed(footer) is None


def test_old_canonical_usage_is_not_a_current_turn_measurement(make_agent):
    agent = SimpleNamespace(_last_turn_usage={"output_tokens": 500})
    _maybe_wrap_callbacks(agent)
    assert _speed(_speed_footer(_msg_ctx.get())) is None


def test_nested_stream_to_nonstream_fallback_is_not_double_counted(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    agent._interruptible_streaming_api_call = lambda *a, **kw: agent._interruptible_api_call(*a, **kw)
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 3)
    assert _speed_footer(_msg_ctx.get())["speed_total_requests"] == 1
    assert _speed(_speed_footer(_msg_ctx.get())) == "50 t/s"


def test_inflight_request_keeps_old_owner_when_agent_is_rebound(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(agent)
    old_ctx = _msg_ctx.get()
    new_ctx = {"message_id": "new", "event_message_id": "new"}

    def change_turn():
        _msg_ctx.set(new_ctx)
        _maybe_wrap_callbacks(agent)
        assert _speed(_speed_footer(old_ctx)) is None  # In-flight is incomplete.

    agent.during = change_turn
    _call(agent, 1, 3)
    assert _speed(_speed_footer(old_ctx)) == "50 t/s"
    assert _speed(_speed_footer(new_ctx)) is None


def test_live_context_wins_over_rebound_agent_totals(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(agent)
    old_turn = _msg_ctx.get()
    new_turn = {"message_id": "other-turn", "event_message_id": "other-turn"}
    _msg_ctx.set(new_turn)
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 6)
    _msg_ctx.set(old_turn)
    _call(agent, 10, 12)
    assert _speed(_speed_footer(old_turn)) == "50 t/s"
    assert _speed(_speed_footer(new_turn)) == "20 t/s"


def test_replacement_agent_keeps_previous_requests_in_same_turn(make_agent):
    first = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(first)
    _call(first, 1, 3)
    replacement = make_agent(_chat_usage(output=900, reasoning=0, total=926))
    _maybe_wrap_callbacks(replacement)
    _call(replacement, 10, 19)
    footer = _speed_footer(_msg_ctx.get())
    assert footer["speed_requests"] == 2
    assert _speed(footer) == "91 t/s"


def test_detached_fork_cannot_steal_usage_or_output(make_agent):
    agent = make_agent(_chat_usage())
    fork = make_agent(_chat_usage())
    fork._persist_disabled = True
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 3)
    _maybe_wrap_callbacks(fork)
    assert _msg_ctx.get()["_agent_ref"] is agent
    assert not getattr(fork._interruptible_api_call, "_hls_usage_wrapper", False)
    assert _speed_footer(_msg_ctx.get())["speed_total_requests"] == 1


def test_worker_registry_context_shares_totals_with_gateway(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    ctx = _msg_ctx.get()
    totals = _totals_for_context(ctx)
    with _session_contexts_lock:
        _session_contexts[agent.session_id] = dict(ctx)
    try:
        _msg_ctx.set(None)
        _maybe_wrap_callbacks(agent)
        _call(agent, 1, 3)
        assert agent._hls_speed_totals is totals
        assert _speed(_speed_footer(ctx)) == "50 t/s"
    finally:
        with _session_contexts_lock:
            _session_contexts.pop(agent.session_id, None)


def test_parallel_requests_have_matched_sums(make_agent):
    totals = RequestTotals()

    def measure(_):
        totals.start()
        totals.record(100, 2)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(measure, range(20)))
    assert totals.footer() == {
        "speed_requests": 20, "speed_total_requests": 20,
        "speed_output_tokens": 2000, "speed_request_seconds": 40,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stream", "burst", "nonstream"])
async def test_gateway_aggregates_all_tool_rounds_independent_of_text_delivery(make_agent, monkeypatch, transport):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True},
        "feishu": {"app_id": "test-app", "app_secret": "test-secret"},
    }
    monkeypatch.setattr(ctrl, "_schedule_linear_flush", Mock())
    monkeypatch.setattr(ctrl, "_complete_session", Mock())
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.get_controller", lambda: ctrl)
    session = CardSession("speed-message", "chat", asyncio.get_running_loop())
    session.state = CardPhase.STREAMING
    session.linear = True
    session.unified_state = UnifiedLinearState()
    ctrl._sessions[session.message_id] = session
    usage = _chat_usage(output=100, reasoning=0, total=126)
    agent = make_agent(usage)

    async def run_agent(*args, **kwargs):
        # Mirrors Hermes' executor hop: context copies share the accumulator.
        copy_context().run(_maybe_wrap_callbacks, agent)
        _call(agent, 1, 3)
        agent.tool_gen_callback("read")
        ctrl.on_tool_update(message_id=session.message_id, tool_name="read", status="started")
        ctrl.on_tool_update(message_id=session.message_id, tool_name="read", status="completed")
        ctrl.on_compression_started(message_id=session.message_id)
        ctrl.on_compression_completed(message_id=session.message_id)
        agent.response = SimpleNamespace(usage=_chat_usage(output=900, reasoning=0, total=926))
        method = "_interruptible_api_call" if transport == "nonstream" else "_interruptible_streaming_api_call"
        _call(agent, 100, 109, method=method)
        if transport == "stream":
            agent.stream_delta_callback("first ")
            agent.stream_delta_callback("last")
        elif transport == "burst":
            agent.stream_delta_callback("first last")
        # Hermes' mutated/session totals must never be used for this rate.
        agent._last_turn_usage = {"output_tokens": 90000}
        return {"final_response": "first last", "model": "example", "output_tokens": 9000}

    result = await _wrap_run_agent(run_agent)(
        SimpleNamespace(), "question", "", [], SimpleNamespace(), agent.session_id,
        event_message_id=session.message_id,
    )
    assert result["already_sent"] is True
    assert session.footer["output_tokens"] == 9000
    assert session.footer["speed_output_tokens"] == 1000
    assert session.footer["speed_request_seconds"] == 11
    assert session.footer["speed_requests"] == 2
    assert _speed(session.footer) == "91 t/s"
    assert usage == _chat_usage(output=100, reasoning=0, total=126)


@pytest.mark.asyncio
async def test_background_completion_receives_aggregate(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))

    async def run(*args, **kwargs):
        _maybe_wrap_callbacks(agent)
        _call(agent, 1, 3)
        _call(agent, 5, 7)
        return {"final_response": "done"}

    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="test-chat")
    with patch("hermes_lark_streaming.patching.hooks.on_message_started"), patch(
        "hermes_lark_streaming.patching.hooks.on_message_completed", return_value=True,
    ) as completed:
        result = await _wrap_run_background_task(run)(SimpleNamespace(), "question", source, "test-task")
    assert result["_hls_card_sent"] is True
    assert _speed(completed.call_args.kwargs["tokens"]) == "50 t/s"
    assert completed.call_args.kwargs["tokens"]["speed_requests"] == 2


@pytest.mark.asyncio
async def test_recursive_interrupt_has_independent_request_totals(make_agent):
    agent = make_agent(_chat_usage(output=100, reasoning=0, total=126))
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 3)
    parent = _msg_ctx.get()
    parent_totals = _totals_for_context(parent)

    async def child(*args, **kwargs):
        _maybe_wrap_callbacks(agent)
        assert _totals_for_context(_msg_ctx.get()) is not parent_totals
        _call(agent, 10, 15)
        _call(agent, 20, 25)
        return {"final_response": "child answer"}

    with patch("hermes_lark_streaming.patching.hooks.on_message_started"), patch(
        "hermes_lark_streaming.patching.hooks.on_message_interrupted",
    ), patch(
        "hermes_lark_streaming.patching.hooks.on_message_completed", return_value=True,
    ) as completed:
        await _wrap_run_agent(child)(
            SimpleNamespace(), "follow-up", "", [], SimpleNamespace(), agent.session_id,
            event_message_id="child-message", _interrupt_depth=1,
        )

    child_completion, parent_completion = completed.call_args_list
    assert child_completion.kwargs["message_id"] == "child-message"
    assert _speed(child_completion.kwargs["tokens"]) == "20 t/s"
    assert child_completion.kwargs["tokens"]["speed_total_requests"] == 2
    assert parent_completion.kwargs["aborted"] is True
    assert _speed(_speed_footer(parent)) == "50 t/s"
    assert _msg_ctx.get()["_speed_totals"] is parent_totals


def test_zero_output_request_still_contributes_time(make_agent):
    agent = make_agent(_chat_usage(output=0, reasoning=0, total=26))
    _maybe_wrap_callbacks(agent)
    _call(agent, 1, 3)
    agent.response = SimpleNamespace(usage=_chat_usage(output=100, reasoning=0, total=126))
    _call(agent, 10, 13)
    footer = _speed_footer(_msg_ctx.get())
    assert footer["speed_total_requests"] == 2
    assert footer["speed_request_seconds"] == 5
    assert _speed(footer) == "20 t/s"


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_unusable_request_clock_never_displays_partial_average(seconds):
    totals = RequestTotals()
    totals.start()
    totals.record(100, 2)
    totals.start()
    totals.record(100, seconds)
    assert _speed(totals.footer()) is None
