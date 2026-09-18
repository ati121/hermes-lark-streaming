"""Provider usage survives normalization and reaches the final card footer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from hermes_lark_streaming.cardkit.elements import _render_footer_field
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.patching import (
    _maybe_wrap_callbacks,
    _msg_ctx,
    _thread_local_ctx,
)
from hermes_lark_streaming.patching.gateway import (
    _visible_output_tokens,
    _wrap_run_agent,
)
from hermes_lark_streaming.patching.usage import (
    _maybe_wrap_api_usage,
    _snapshot_usage,
    _speed_usage_for_agent,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import CardPhase


def _chat_usage(output=135, reasoning=678, total=839):
    # This provider counts completion and reasoning separately: 26 + 135 + 678.
    return {
        "prompt_tokens": 26,
        "completion_tokens": output,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": total,
    }


@pytest.fixture
def make_agent():
    token = _msg_ctx.set({"message_id": "speed-message", "event_message_id": "speed-message"})
    previous_thread_context = getattr(_thread_local_ctx, "data", None)
    _thread_local_ctx.data = None

    def make(usage=None):
        agent = SimpleNamespace(
            session_id="speed-session",
            response=SimpleNamespace(usage=usage),
            _last_turn_usage={"output_tokens": 999, "reasoning_tokens": 100},
            stream_delta_callback=lambda text: None,
            interim_assistant_callback=lambda text: None,
            calls=[],
        )

        def api_call(api_kwargs, **kwargs):
            agent.calls.append((api_kwargs, kwargs))
            if isinstance(agent.response, BaseException):
                raise agent.response
            first_delta = kwargs.get("on_first_delta")
            if first_delta:
                first_delta()
            return agent.response

        agent._interruptible_api_call = api_call
        agent._interruptible_streaming_api_call = api_call
        return agent

    yield make
    _msg_ctx.reset(token)
    _thread_local_ctx.data = previous_thread_context


@pytest.mark.parametrize(("output", "reasoning", "total", "expected"), [
    (135, 678, 839, 135),       # Observed Gemini-compatible usage.
    (1000, 200, 1226, 1000),   # Output > reasoning must not be undercounted either.
    (135, 135, 296, 135),      # Equal counters with reasoning billed separately.
    (500, 320, 526, 180),      # Standard OpenAI: reasoning is inside completion.
    (135, 135, 161, 0),        # Standard reasoning-only output stays hidden.
    (135, 0, 161, 135),
    (135, 678, None, 0),       # Missing/inconsistent totals cannot prove exclusion.
    (135, 678, 999, 0),
])
def test_token_accounting_uses_provider_total(output, reasoning, total, expected):
    assert _visible_output_tokens(_snapshot_usage(_chat_usage(output, reasoning, total))) == expected


def test_responses_sdk_shape_uses_full_input_including_cached_tokens():
    usage = SimpleNamespace(
        input_tokens=1026, output_tokens=500, total_tokens=1526,
        input_tokens_details=SimpleNamespace(cached_tokens=1000),
        output_tokens_details=SimpleNamespace(reasoning_tokens=320),
    )
    assert _visible_output_tokens(_snapshot_usage(usage)) == 180


def test_anthropic_usage_without_reasoning_breakdown_keeps_output():
    usage = SimpleNamespace(input_tokens=26, output_tokens=135, cache_read_input_tokens=1000)
    assert _visible_output_tokens(_snapshot_usage(usage)) == 135


@pytest.mark.parametrize("output", [None, "135", float("nan"), float("inf"), -1])
def test_invalid_output_cannot_break_card_completion(output):
    assert _visible_output_tokens(_snapshot_usage(_chat_usage(output=output))) == 0


@pytest.mark.parametrize("method", ["_interruptible_api_call", "_interruptible_streaming_api_call"])
def test_capture_preserves_call_and_copies_only_usage_counters(make_agent, method):
    usage = _chat_usage()
    usage["provider_extra"] = "not needed by cards"
    agent = make_agent(usage)
    response = agent.response
    request = {"model": "example"}
    _maybe_wrap_api_usage(agent)

    assert getattr(agent, method)(request) is response
    assert agent.calls == [(request, {})]
    assert response.usage is usage
    captured = _speed_usage_for_agent(agent)
    assert captured == {
        "prompt_tokens": 26, "output_tokens": 135,
        "reasoning_tokens": 678, "total_tokens": 839,
    }
    usage["completion_tokens"] = 1
    assert captured["output_tokens"] == 135
    assert agent._last_turn_usage == {"output_tokens": 999, "reasoning_tokens": 100}


def test_cached_callbacks_keep_one_wrapper_and_refresh_turn_ownership(make_agent):
    agent = make_agent(_chat_usage())
    _maybe_wrap_callbacks(agent)
    wrapped_call = agent._interruptible_streaming_api_call
    first_delta = Mock()
    with patch("hermes_lark_streaming.patching.hooks.on_model_activity") as activity:
        wrapped_call({}, on_first_delta=first_delta)
    first_delta.assert_called_once_with()
    activity.assert_called_once()
    assert _visible_output_tokens(_speed_usage_for_agent(agent)) == 135

    _msg_ctx.set({"message_id": "next-message", "event_message_id": "next-message"})
    _maybe_wrap_callbacks(agent)
    assert agent._interruptible_streaming_api_call is wrapped_call
    assert _msg_ctx.get()["_agent_ref"] is agent
    assert _speed_usage_for_agent(agent) is None


@pytest.mark.parametrize("final_call", ["usage_missing", "failure", "nonstream_usage_missing"])
def test_final_call_cannot_reuse_previous_usage(make_agent, final_call):
    agent = make_agent(_chat_usage())
    _maybe_wrap_api_usage(agent)
    agent._interruptible_streaming_api_call({})
    assert _visible_output_tokens(_speed_usage_for_agent(agent)) == 135

    if final_call == "failure":
        failure = RuntimeError("provider failed")
        agent.response = failure
        with pytest.raises(RuntimeError) as raised:
            agent._interruptible_streaming_api_call({})
        assert raised.value is failure
    else:
        agent.response = SimpleNamespace(usage=None)
        method = (agent._interruptible_api_call if final_call.startswith("nonstream")
                  else agent._interruptible_streaming_api_call)
        method({})
    assert _speed_usage_for_agent(agent) is None
    assert _visible_output_tokens(_speed_usage_for_agent(agent)) == 0


def test_usage_capture_error_preserves_the_response(make_agent):
    class Response:
        @property
        def usage(self):
            raise ValueError("unreadable usage")

    agent = make_agent()
    agent.response = Response()
    _maybe_wrap_api_usage(agent)
    assert agent._interruptible_api_call({}) is agent.response
    assert _speed_usage_for_agent(agent) is None


def test_older_agent_without_api_capture_uses_canonical_usage():
    agent = SimpleNamespace(_last_turn_usage={"output_tokens": 500, "reasoning_tokens": 320})
    _maybe_wrap_api_usage(agent)
    assert _visible_output_tokens(_speed_usage_for_agent(agent)) == 180


@pytest.mark.asyncio
async def test_gateway_completion_uses_original_total_after_hermes_normalizes_it(make_agent, monkeypatch):
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
    usage = _chat_usage()
    agent = make_agent(usage)

    async def run_agent(*args, **kwargs):
        _maybe_wrap_callbacks(agent)
        response = agent._interruptible_streaming_api_call({})
        assert response.usage is usage
        with patch("hermes_lark_streaming.controller.core.time.monotonic", return_value=10.0):
            agent.stream_delta_callback("first ")
        with patch("hermes_lark_streaming.controller.core.time.monotonic", return_value=10.3884):
            agent.stream_delta_callback("last")
        # Hermes recomputes total from prompt + output, losing the provider's
        # separate reasoning contribution. This must not change card speed.
        agent._last_turn_usage = {
            "prompt_tokens": 26, "output_tokens": 135,
            "reasoning_tokens": 678, "total_tokens": 161,
        }
        return {"final_response": "first last", "model": "example", "output_tokens": 9000}

    result = await _wrap_run_agent(run_agent)(
        SimpleNamespace(), "question", "", [], SimpleNamespace(), agent.session_id,
        event_message_id=session.message_id,
    )

    assert result["already_sent"] is True
    assert session.footer["output_tokens"] == 9000
    assert session.footer["speed_output_tokens"] == 135
    assert session.footer["gen_seconds"] == pytest.approx(0.3884)
    assert _render_footer_field(
        "speed", session.footer, is_error=False, is_aborted=False, show_label=False,
    ) == ("348 t/s", "348 t/s")
    assert usage == _chat_usage()


@pytest.mark.asyncio
async def test_gateway_burst_answer_still_reports_speed(make_agent, monkeypatch):
    """线上截图 2 的场景：短答案被上游整段下发时，速度仍必须显示."""
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
    # This model call started two seconds before its single visible chunk.
    session._speed_call_start = 100.0
    ctrl._sessions[session.message_id] = session
    agent = make_agent(_chat_usage(output=118, reasoning=0, total=144))

    async def run_agent(*args, **kwargs):
        _maybe_wrap_callbacks(agent)
        agent._interruptible_streaming_api_call({})
        # The whole visible answer arrives as one chunk: no in-stream span.
        with patch("hermes_lark_streaming.controller.core.time.monotonic", return_value=102.0):
            agent.stream_delta_callback("整段答案")
        return {
            "final_response": "整段答案",
            "model": "deepseek-v4.1-flash",
            "output_tokens": 118,
        }

    result = await _wrap_run_agent(run_agent)(
        SimpleNamespace(), "question", "", [], SimpleNamespace(), agent.session_id,
        event_message_id=session.message_id,
    )

    assert result["already_sent"] is True
    assert session.footer["speed_output_tokens"] == 118
    assert session.footer["speed_window"] == "call"
    assert session.footer["gen_seconds"] == pytest.approx(2.0)
    assert _render_footer_field(
        "speed", session.footer, is_error=False, is_aborted=False, show_label=False,
    ) == ("59 t/s", "59 t/s")
