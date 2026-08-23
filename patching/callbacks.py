"""Callback wrapping for AIAgent streaming callbacks."""

from __future__ import annotations

from typing import Any

from . import (
    _msg_ctx,
    _thread_local_ctx,
    _logger,
    _session_contexts,
    _session_contexts_lock,
)

def _eid_from_context(ctx: Any) -> str | None:
    if not isinstance(ctx, dict):
        return None
    return ctx.get("event_message_id") or ctx.get("message_id")

def _current_context_eid() -> str | None:
    """Return only the live ContextVar id, without thread-local fallback."""
    return _eid_from_context(_msg_ctx.get())

def _thread_local_eid() -> str | None:
    """Last-resort id for legacy executor paths without session registration."""
    return _eid_from_context(getattr(_thread_local_ctx, "data", None))

def _resolve_eid(fallback_eid: str | None, agent=None) -> str | None:
    """Resolve the live turn id at callback time.

    Cached agents can retain synthetic callbacks (notably tool_gen_callback)
    across turns.  Prefer the live session registry before the closure's
    fallback id so those callbacks cannot write into the previous card.
    """
    _eid = _current_context_eid()
    if _eid:
        return _eid
    registered = _registered_eid(agent) if agent is not None else None
    return registered or _thread_local_eid() or fallback_eid

def _registered_eid(agent) -> str | None:
    """Resolve the turn id explicitly registered before the executor hop."""
    session_id = str(getattr(agent, "session_id", None) or "")
    if not session_id:
        return None
    with _session_contexts_lock:
        ctx = _session_contexts.get(session_id)
        if not ctx:
            return None
        return ctx.get("event_message_id") or ctx.get("message_id")

def _maybe_wrap_callbacks(agent) -> None:
    """Replace streaming callbacks on *agent* with wrappers that also fire
    Feishu CardKit updates.  Skips silently when outside a Feishu message
    context (i.e. no event_message_id in context)."""
    _logger.debug(
        "HLS: _maybe_wrap_callbacks invoked, has_stream=%s, eid_lookup=%s",
        bool(getattr(agent, "stream_delta_callback", None)),
        bool(_current_context_eid() or _registered_eid(agent) or _thread_local_eid()),
    )

    eid = _current_context_eid() or _registered_eid(agent) or _thread_local_eid()
    if not eid:
        _logger.debug("HLS: skip — no event_message_id in ctx")
        return  # Not in a hermes-lark-streaming context — skip

    # Hermes' ``on_first_delta`` is not the transport-level first event: the
    # host only fires it after a chunk contains renderable text, reasoning, or
    # a complete tool name.  OpenAI-compatible providers commonly yield an
    # earlier role-only / empty / keepalive chunk. Opening the HTTP response
    # itself is not enough: model prefill may still be in progress, so wait
    # for Hermes' "receiving stream response" activity boundary below.
    _current_touch_activity = getattr(agent, "_touch_activity", None)
    if _current_touch_activity and not getattr(
        _current_touch_activity, "_hls_wrapper", False
    ):
        _orig_touch_activity = _current_touch_activity

        def _touch_activity_wrapper(desc, *args, **kwargs):
            try:
                _activity = str(desc or "")
                if _activity.startswith("receiving stream response"):
                    from .hooks import on_model_activity

                    _chunk_eid = _resolve_eid(eid, agent)
                    if _chunk_eid:
                        on_model_activity(
                            message_id=_chunk_eid,
                            source="stream.first_chunk",
                        )
                elif _activity.startswith((
                    "context compression started",
                    "context compression in progress",
                )):
                    from .hooks import on_compression_started

                    _compression_eid = _resolve_eid(eid, agent)
                    if _compression_eid:
                        on_compression_started(
                            message_id=_compression_eid,
                            source=_activity,
                        )
                elif _activity.startswith((
                    "context compression completed",
                    "context compression failed",
                    "context compression cancelled",
                    "context compression rollback failed",
                    "context compression timed out",
                )):
                    from .hooks import on_compression_completed

                    _compression_eid = _resolve_eid(eid, agent)
                    if _compression_eid:
                        on_compression_completed(
                            message_id=_compression_eid,
                            source=_activity,
                        )
            except Exception:
                _logger.debug(
                    "HLS: touch_activity_wrapper exception", exc_info=True
                )
            return _orig_touch_activity(desc, *args, **kwargs)

        agent._touch_activity = _touch_activity_wrapper
        setattr(agent._touch_activity, "_hls_wrapper", True)

    # Hermes fires ``on_first_delta`` at the wire-level boundary before it
    # dispatches text, reasoning, or a tool name.  It remains a useful fallback
    # for providers that do not expose the earlier response-open/raw-chunk
    # seams above.  The wrapper is installed on the agent instance (not the
    # class), is marked for idempotence, and always chains Hermes' own callback
    # exactly once.
    _current_streaming_call = getattr(agent, "_interruptible_streaming_api_call", None)
    if _current_streaming_call and not getattr(_current_streaming_call, "_hls_wrapper", False):
        _orig_streaming_call = _current_streaming_call

        def _streaming_call_wrapper(api_kwargs, *args, **kwargs):
            _orig_first_delta = kwargs.get("on_first_delta")

            def _first_delta_wrapper(*first_args, **first_kwargs):
                try:
                    from .hooks import on_model_activity

                    _first_eid = _resolve_eid(eid, agent)
                    if _first_eid:
                        on_model_activity(
                            message_id=_first_eid,
                            source="stream.first_delta",
                        )
                except Exception:
                    _logger.debug("HLS: first_delta_wrapper exception", exc_info=True)
                if _orig_first_delta:
                    return _orig_first_delta(*first_args, **first_kwargs)
                return None

            kwargs["on_first_delta"] = _first_delta_wrapper
            return _orig_streaming_call(api_kwargs, *args, **kwargs)

        agent._interruptible_streaming_api_call = _streaming_call_wrapper
        setattr(agent._interruptible_streaming_api_call, "_hls_wrapper", True)

    # Hermes emits the first meaningful event of a tool-only model response
    # through tool_gen_callback as soon as the function name is available.
    # It can arrive many seconds before tool.started, so it must independently
    # move the card from waiting to thinking.  ``on_first_delta`` is injected
    # by conversation_loop itself, so wrapping the private streaming method
    # here would wrap an already-correct callback and can double-fire it.
    _current_tool_gen = getattr(agent, "tool_gen_callback", None)
    if not (_current_tool_gen and getattr(_current_tool_gen, "_hls_wrapper", False)):
        _orig_tool_gen = _current_tool_gen

        def _tool_gen_wrapper(tool_name, *args, **kwargs):
            _eid = _resolve_eid(eid, agent)
            if _eid:
                try:
                    from .hooks import on_model_activity

                    on_model_activity(
                        message_id=_eid,
                        source=f"tool.generating:{tool_name or 'unknown'}",
                    )
                    # This is a side-channel state transition, not a
                    # replacement for Hermes' own display callback. Preserve
                    # the original callback for CLI/TUI and other consumers.
                except Exception:
                    _logger.debug("HLS: tool_gen_wrapper exception", exc_info=True)
            if _orig_tool_gen:
                return _orig_tool_gen(tool_name, *args, **kwargs)

        agent.tool_gen_callback = _tool_gen_wrapper
        setattr(agent.tool_gen_callback, "_hls_wrapper", True)

    _current_stream = getattr(agent, "stream_delta_callback", None)
    _current_interim = getattr(agent, "interim_assistant_callback", None)
    _any_wrapped = (
        (_current_stream and getattr(_current_stream, "_hls_wrapper", False))
        or (_current_interim and getattr(_current_interim, "_hls_wrapper", False))
    )
    if _any_wrapped:
        # ── Late-arriving reasoning_callback fix ──
        _late_reasoning = getattr(agent, "reasoning_callback", None)
        if _late_reasoning and not getattr(_late_reasoning, "_hls_wrapper", False):
            _orig_late = _late_reasoning

            def _late_reasoning_wrapper(text, *args, **kwargs):
                _eid = _resolve_eid(eid, agent)
                try:
                    from .hooks import on_reasoning_delta
                    if text and _eid:
                        on_reasoning_delta(message_id=_eid, text=text)
                except Exception:
                    _logger.debug("HLS: suppressed exception", exc_info=True)
                # again with a stale eid, duplicating reasoning text.
                if not getattr(_orig_late, "_hls_wrapper", False):
                    return _orig_late(text, *args, **kwargs)

            agent.reasoning_callback = _late_reasoning_wrapper
            setattr(agent.reasoning_callback, "_hls_wrapper", True)
        return

    # v1.3.2 fix (P3-02): _stream_consumed_len is cleaned up when the thinking
    _stream_consumed_len: dict[str, int] = {}

    def _cleanup_consumed_len(_eid: str) -> None:
        """Remove consumed-length tracking for a completed message."""
        _stream_consumed_len.pop(_eid, None)

    if getattr(agent, "stream_delta_callback", None):
        _orig_stream = agent.stream_delta_callback

        def _answer_wrapper(text, *args, **kwargs):
            _eid = _resolve_eid(eid, agent)
            if not _eid:
                return _orig_stream(text, *args, **kwargs)
            try:
                from .hooks import on_answer_delta

                if text and on_answer_delta(message_id=_eid, text=text):
                    # Record total consumed length for dedup with interim_assistant_callback
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper exception", exc_info=True)
            return _orig_stream(text, *args, **kwargs)

        agent.stream_delta_callback = _answer_wrapper
        _logger.debug("HLS: _maybe_wrap_callbacks stream_delta_callback wrapped")
    else:
        # Fix: Create our own stream_delta_callback that routes answer tokens to
        def _answer_wrapper_synthetic(text, *args, **kwargs):
            # Handle None — stream boundary signal from conversation_loop
            # (tool boundary flush / end-of-stream). Just ignore it.
            if text is None:
                return
            _eid = _resolve_eid(eid, agent)
            if not _eid:
                return
            try:
                from .hooks import on_answer_delta

                if text and on_answer_delta(message_id=_eid, text=text):
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper_synthetic exception", exc_info=True)
            # No original callback to call — Hermes didn't provide one

        agent.stream_delta_callback = _answer_wrapper_synthetic
        setattr(agent.stream_delta_callback, "_hls_wrapper", True)

    if getattr(agent, "interim_assistant_callback", None):
        _orig_interim = agent.interim_assistant_callback

        def _thinking_wrapper(text, *args, **kwargs):
            _eid = _resolve_eid(eid, agent)
            if not _eid:
                return _orig_interim(text, *args, **kwargs)
            try:
                # ── already_streamed passthrough (Hermes hint) ──
                already_streamed = kwargs.get("already_streamed", False)
                if already_streamed:
                    # v1.3.2 fix (P3-02): clean up consumed-length tracking for
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                # ── Length-based dedup ──
                consumed_len = _stream_consumed_len.get(_eid, 0)
                if text and consumed_len > 0 and len(text) <= consumed_len:
                    # v1.3.2 fix (P3-02): same cleanup — text fully consumed,
                    # message streaming is done.
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                if text:
                    from .hooks import on_thinking_delta
                    consumed = on_thinking_delta(message_id=_eid, text=text)
                    if consumed:
                        return
            except Exception:
                _logger.debug("HLS: thinking_wrapper exception", exc_info=True)
            return _orig_interim(text, *args, **kwargs)

        agent.interim_assistant_callback = _thinking_wrapper
        setattr(agent.interim_assistant_callback, "_hls_wrapper", True)
        _logger.debug("HLS: _maybe_wrap_callbacks interim_assistant_callback wrapped")
    else:
        _logger.debug("HLS: _maybe_wrap_callbacks NO interim_assistant_callback on agent")

    # ── TOOL: wrap tool_progress_callback ──
    if getattr(agent, "tool_progress_callback", None):
        _orig_tool = agent.tool_progress_callback

        def _tool_wrapper(event_type, tool_name=None, preview=None, *args, **kwargs):
            _eid = _resolve_eid(eid, agent)
            if not _eid:
                return _orig_tool(event_type, tool_name, preview, *args, **kwargs)
            try:
                from .hooks import on_model_activity, on_reasoning_delta, on_tool_updated

                # Hermes 0.20.x reports completed scratch reasoning through
                # tool_progress_callback instead of reasoning_callback on some
                # provider paths.  Keep it inside the active card and use it
                # as an upstream-activity signal rather than letting Gateway
                # render a separate progress message.
                if event_type == "_thinking" or tool_name == "_thinking":
                    reasoning_text = (
                        (tool_name or preview or "")
                        if event_type == "_thinking"
                        else (preview or "")
                    )
                    if reasoning_text:
                        if on_reasoning_delta(message_id=_eid, text=reasoning_text):
                            return
                    elif on_model_activity(
                        message_id=_eid, source="reasoning.available"
                    ):
                        return

                if event_type in ("tool.started", "tool.completed"):
                    if on_tool_updated(
                        message_id=_eid,
                        tool_name=tool_name or "",
                        status="started" if event_type == "tool.started" else "completed",
                        detail=preview or "",
                    ):
                        return
            except Exception:
                _logger.debug("HLS: tool_wrapper exception", exc_info=True)
            return _orig_tool(event_type, tool_name, preview, *args, **kwargs)

        agent.tool_progress_callback = _tool_wrapper

    # Mark wrapper functions so guard can detect them next time
    if getattr(agent, "stream_delta_callback", None):
        setattr(agent.stream_delta_callback, "_hls_wrapper", True)
    # interim_assistant_callback is already marked above (in its wrapper block)
    if getattr(agent, "tool_progress_callback", None):
        setattr(agent.tool_progress_callback, "_hls_wrapper", True)

    # ── REASONING: wrap reasoning_callback ──
    _orig_reasoning = getattr(agent, "reasoning_callback", None)

    def _reasoning_wrapper(text, *args, **kwargs):
        _eid = _resolve_eid(eid, agent)
        if not _eid:
            # No ctx — call original if present
            if _orig_reasoning and not getattr(_orig_reasoning, "_hls_wrapper", False):
                return _orig_reasoning(text, *args, **kwargs)
            return
        try:
            from .hooks import on_reasoning_delta

            if text:
                on_reasoning_delta(message_id=_eid, text=text)
        except Exception:
            _logger.debug("HLS: reasoning_wrapper exception", exc_info=True)
        if _orig_reasoning and not getattr(_orig_reasoning, "_hls_wrapper", False):
            return _orig_reasoning(text, *args, **kwargs)

    agent.reasoning_callback = _reasoning_wrapper
    setattr(agent.reasoning_callback, "_hls_wrapper", True)

    # ── BACKGROUND_REVIEW: wrap background_review_callback ──
    if getattr(agent, "background_review_callback", None):
        _orig_bg = agent.background_review_callback

        def _bg_wrapper(message, *args, **kwargs):
            _eid = _resolve_eid(eid, agent)
            if not _eid:
                return _orig_bg(message, *args, **kwargs)
            try:
                from .hooks import on_background_review_message

                deferred = on_background_review_message(
                    message_id=_eid,
                    text=message,
                    sender=_orig_bg,
                )
                if deferred:
                    return
            except Exception:
                _logger.debug("HLS: bg_wrapper exception", exc_info=True)
            return _orig_bg(message, *args, **kwargs)

        agent.background_review_callback = _bg_wrapper

    # Mark background_review_callback wrapper (already marked above for others)
    if getattr(agent, "background_review_callback", None):
        setattr(agent.background_review_callback, "_hls_wrapper", True)

    # ── Store agent reference for cache token extraction ──
    ctx = _msg_ctx.get()
    if ctx is not None:
        ctx["_agent_ref"] = agent
        _thread_local_ctx.data = dict(ctx)
