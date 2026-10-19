"""Unified panel linear mode — create, flush, and seal a single-card streaming session."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..cardkit import (
    _LOADING_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _count_tag_objects,
    _loading_element,
    _loading_hint_element,
    _streaming_element,
    build_interactive_card_v2,
    build_preservative_seal_actions,
    build_streaming_card_v2,
    build_unified_panel,
    enforce_element_limit,
)
from ..cardkit.md import (
    render_answer_markdown,
)
from ..feishu import (
    CARDKIT_SEQUENCE_CONFLICT,
    CARDKIT_STREAMING_CLOSED,
    IM_FREQUENCY_LIMIT,
    FeishuAPIError,
    is_element_not_found_error,
    is_schema_error,
    is_terminal_api_code,
    is_unavailable,
)
from ..state.linear import UnifiedLinearState
from ..state.phase import TerminalReason
from ..state.text import split_reasoning_text
from .mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATING,
    CREATION_FAILED,
    IDLE,
    STREAMING,
    TERMINATED,
)

if TYPE_CHECKING:
    from ..config import Config
    from ..feishu import FeishuClient
    from ..state.session import CardSession

_logger = logging.getLogger("hermes_lark_streaming")


def _uses_interactive_card_transport(text_sizes: dict[str, Any] | None) -> bool:
    """飞书只在普通 IM 卡片传输中可靠应用 Markdown 字号。"""
    return isinstance(text_sizes, dict) and bool(text_sizes)

def _build_seal_summary(state: UnifiedLinearState | None) -> str:
    """Build seal summary from state — answer text or fallback to reasoning."""
    if state is None:
        return ""
    summary_text = state.answer_text
    if not summary_text and state.reasoning_rounds:
        summary_text = state.reasoning_rounds[-1].text if state.reasoning_rounds else ""
    if summary_text:
        return summary_text[:120].replace("\n", " ").replace("```", "").strip()
    return ""


def _footer_elapsed_ms(footer_data: dict | None) -> float:
    """Return the upstream wall-clock duration used by the final footer."""
    if not isinstance(footer_data, dict):
        return 0.0
    duration = footer_data.get("duration", 0)
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration) * 1000
    return 0.0

def _panel_update_action(panel: dict[str, Any]) -> dict[str, Any]:
    """``partial_update_element`` for the unified panel.

    ``header`` and ``elements`` together are the whole editable surface; the
    six hand-written copies of this dict were identical down to key order.
    """
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": UNIFIED_PANEL_ELEMENT_ID,
            "partial_element": {
                "header": panel["header"],
                "elements": panel["elements"],
            },
        },
    }


def _answer_update_action(content: str) -> dict[str, Any]:
    """``partial_update_element`` for answer text.

    Never carries ``tag``/``text_align``/``text_size``: Feishu rejects a
    partial update that changes an element's tag.
    """
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": ANSWER_ELEMENT_ID,
            "partial_element": {"content": content},
        },
    }


def _merge_streamed_answer(state: UnifiedLinearState, incoming: str) -> str | None:
    """Reconcile a cumulative answer with what the card already streamed.

    Hermes delivers interim text cumulatively and then the final answer again,
    so the same prefix/extension decision was written three times (thinking
    deltas, answer deltas, completion). Returns the fragment appended, ``""``
    when the incoming text was already covered, or ``None`` when it contradicts
    what was streamed and the caller must replace it.

    Compares with ``str.startswith`` rather than slicing a prefix — the old
    ``incoming[:len(existing)] == existing`` copied the whole existing answer
    on every delta.
    """
    if not incoming:
        return ""
    existing = state.answer_text
    if not existing:
        state.on_answer_delta(incoming)
        return incoming
    if len(incoming) > len(existing) and incoming.startswith(existing):
        new_part = incoming[len(existing):]
        if new_part:
            state.on_answer_delta(new_part)
        return new_part
    if incoming == existing:
        return ""
    return None


async def _write_answer_stream(
    client: Any,
    session: CardSession,
    state: UnifiedLinearState,
    content: str,
) -> bool:
    """Stream one answer snapshot; restore the dirty flag if it never landed.

    Phase 2 (first answer write) and Phase 3 (steady-state updates) carried
    near-identical copies of this block, including the restore-on-failure
    rules. Returns ``False`` when the caller must stop writing this flush.
    """
    session.sequence += 1
    try:
        await client.cardkit_stream_element(
            session.card_id, ANSWER_ELEMENT_ID, content, sequence=session.sequence,
        )
        return True
    except asyncio.CancelledError:
        _restore_answer_flush_snapshot(session, state)
        raise
    except FeishuAPIError as e:
        _restore_answer_flush_snapshot(session, state)
        if e.code == CARDKIT_STREAMING_CLOSED:
            if session._streaming_closed_logged:
                _logger.debug("HLS: answer stream closed (already logged): card=%s",
                              (session.card_id or "?")[:12])
            else:
                _logger.info(
                    "HLS: answer stream closed mid-flush, seal will finish: card=%s",
                    (session.card_id or "?")[:12],
                )
                session._streaming_closed_logged = True
            session._streaming_closed = True
            return False
        if is_element_not_found_error(e):
            # 300313: the answer element is not on the server yet. The seal
            # re-sends the final answer, so this is recoverable either way.
            _logger.info(
                "HLS: answer element missing, retry on next flush: card=%s",
                (session.card_id or "?")[:12],
            )
            return False
        _logger.debug("HLS: unified stream_element failed: %s", e)
        return True
    except Exception as e:
        _restore_answer_flush_snapshot(session, state)
        _logger.warning(
            "HLS: unified stream_element unexpected failure: %s card=%s",
            e,
            (session.card_id or "?")[:12],
        )
        return True


def _take_panel_dirty(state: UnifiedLinearState) -> tuple[bool, bool]:
    """Consume the panel flags for one write, returning what was set.

    Callbacks arrive from worker threads while CardKit is awaiting the network
    request. Clearing after the await discards anything that landed during the
    call; clearing before it and restoring on failure keeps both.
    """
    snapshot = (state.panel_dirty, state.tool_steps_dirty)
    state.panel_dirty = False
    state.tool_steps_dirty = False
    return snapshot


def _restore_panel_dirty(state: UnifiedLinearState, snapshot: tuple[bool, bool]) -> None:
    """Re-raise panel flags that a failed write never delivered."""
    panel, tools = snapshot
    state.panel_dirty = state.panel_dirty or panel
    state.tool_steps_dirty = state.tool_steps_dirty or tools


async def _fallback_write_answer(
    client: Any,
    card_id: str,
    content: str,
    *,
    sequence: int,
) -> bool:
    """Fallback: write answer content via batch_update + partial_update_element."""
    try:
        fallback_actions = [_answer_update_action(content)]
        await client.cardkit_batch_update(card_id, fallback_actions, sequence=sequence)
        return True
    except FeishuAPIError as e:
        _logger.warning("HLS: fallback write answer failed: %s", e)
        return False


def _take_answer_flush_snapshot(
    session: CardSession,
    state: UnifiedLinearState,
) -> str | None:
    """Atomically take the answer snapshot owned by one stream request.

    Hermes callbacks can run on worker threads while CardKit is awaiting the
    network request.  The lock covers both reading ``answer_text`` and
    consuming ``answer_dirty``; a callback therefore either lands before the
    snapshot or leaves a fresh dirty flag after it.
    """
    with session._stream_lock:
        if not state.answer_dirty:
            return None
        content = render_answer_markdown(state.answer_text)
        state.answer_dirty = False
        return content


def _restore_answer_flush_snapshot(
    session: CardSession,
    state: UnifiedLinearState,
) -> None:
    """Put a failed answer snapshot back into the stream queue."""
    with session._stream_lock:
        state.answer_dirty = True

# NOTE: This is the *server-side flush interval* (how often we send
_ANSWER_FAST_STREAM_MS = 0.150  # answer-only 节流间隔（150ms，v1.2.1 从 70ms 上调）
_INTERACTIVE_INTERVAL_SEC = 1.0
_INTERACTIVE_COOLDOWN_SEC = 12.0
_INTERACTIVE_RECOVERY_DELAYS = (20.0, 30.0, 60.0)

# Spinner-row emoji for the no-tool state. Tools bring their own (see
# ``_tool_emoji``); this keeps the row's shape when the turn switches between
# thinking and a tool, so the text doesn't jump sideways.
_THINKING_EMOJI = "💭"
_COMPRESSION_EMOJI = "🗜️"

def _only_preparation_records(session: CardSession) -> bool:
    state = session.unified_state
    return bool(
        state and session._response_phase in ("waiting", "compression")
        and session.tool_use.only_passive_steps
        and not (state.answer_text or state.current_reasoning_text or state.reasoning_rounds)
    )


def _simulated_seal_body(
    panel: dict[str, Any],
    state: UnifiedLinearState | None,
    seal_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """The card body a seal is about to produce, for budget accounting.

    ``enforce_element_limit`` takes a whole card and trims the panel until the
    total fits, because the budget counts tag objects at every nesting level.
    Building the body means the panel's own 100+ children are weighed together
    with the answer, footer and error rows that share the card.
    """
    elements: list[dict[str, Any]] = [panel]
    if state is not None and state.answer_text:
        elements.append({"tag": "markdown", "content": state.answer_text})
    else:
        elements.append({"tag": "markdown", "content": " "})
    for action in seal_actions:
        if action.get("action") == "add_elements":
            elements.extend(action.get("params", {}).get("elements", []))
    return {"body": {"elements": elements}}


def _trim_card_to_budget(
    card: dict[str, Any],
    *,
    text_sizes: dict[str, Any] | None,
    label: str,
) -> None:
    """Apply Feishu's tag-object budget to a card that is about to be written.

    The seal path used to carry its own copy of the trimming loop, which had
    drifted from ``enforce_element_limit``: it parsed the hint's "N 项" text
    instead of the structured counts, so a panel folded twice lost the first
    pass's tally. Both the first seal attempt and its sequence-conflict retry
    now go through the one implementation.

    ``enforce_element_limit`` trims the panel's child list in place, and the
    ``partial_update_element`` action holds a reference to that same list, so
    the caller's actions pick the trimming up with no rebuild step.
    """
    before = _count_tag_objects(card)
    enforce_element_limit(card, text_sizes=text_sizes)
    after = _count_tag_objects(card)
    if after < before:
        _logger.warning(
            "%s: card element count %d over budget, folded %d elements",
            label, before, before - after,
        )


class UnifiedControllerMixin:
    """Unified panel linear mode — phased card lifecycle."""

    # ── Instance attributes provided by StreamCardController ──
    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[..., Coroutine[Any, Any, None]]
    _schedule_card_update: Callable[[CardSession], None]
    _cleanup: Callable[[str], None]
    _flush_deferred_background_reviews: Callable[[CardSession], None]

    def _build_session_panel(
        self,
        session: CardSession,
        state: UnifiedLinearState,
        *,
        expanded: bool | None = None,
        total_elapsed_ms: float | None = None,
        current_reasoning_text: str | None = None,
        show_reasoning: bool | None = None,
        active_tool: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Single entry point for every panel the linear path renders.

        The eight call sites used to assemble :func:`build_unified_panel`
        arguments by hand, and they drifted: some passed the live reasoning
        text, some an empty string; only two passed ``active_tool``; only the
        final frames passed a clock. Two builders rendering the same state
        differently means a flush can visibly undo a seal, so the defaults
        live here and callers only override what genuinely differs.
        """
        return build_unified_panel(
            reasoning_rounds=state.reasoning_rounds,
            current_reasoning_text=(
                state.current_reasoning_text
                if current_reasoning_text is None else current_reasoning_text
            ),
            tool_steps=session.tool_use.build_display_steps(),
            tool_elapsed_ms=session.tool_use.elapsed_ms,
            total_elapsed_ms=0.0 if total_elapsed_ms is None else total_elapsed_ms,
            show_reasoning=self._cfg.show_reasoning if show_reasoning is None else show_reasoning,
            expanded=self._cfg.streaming_panel_expanded if expanded is None else expanded,
            panel_events=state.panel_events,
            max_tool_steps=self._cfg.max_tool_steps,
            max_reasoning_rounds=self._cfg.max_reasoning_rounds,
            text_sizes=session.text_sizes,
            active_tool=self._active_tool_for_header(session) if active_tool is None else active_tool,
        )

    def _build_interactive_linear_card(
        self,
        session: CardSession,
        *,
        final: bool = False,
        partial: bool = False,
        footer_data: dict | None = None,
        is_error: bool = False,
        is_aborted: bool = False,
        error_message: str = "",
        footer_fields: list[list[str]] | None = None,
        footer_show_label: bool = False,
    ) -> dict[str, Any]:
        """为普通 interactive 消息渲染完整线性卡片。"""
        state = session.unified_state
        elements: list[dict[str, Any]] = []

        # 老大 2026-09-20: 思考过程常显在折叠面板外面，几行预览 + 展开按钮
        if state is not None and self._cfg.show_reasoning:
            try:
                from ..cardkit.elements import (
                    build_pinned_reasoning_elements_from_text,
                    pinned_reasoning_full_text,
                )
                _cur = "" if final else state.current_reasoning_text
                _rsn_full = pinned_reasoning_full_text(state.reasoning_rounds, _cur)
                _rsn_els = build_pinned_reasoning_elements_from_text(
                    _rsn_full,
                    text_sizes=session.text_sizes,
                    expanded=bool(getattr(state, "reasoning_expanded", False)),
                    preview=not (final or bool(getattr(session, "is_terminal_phase", False))),
                )
                if _rsn_els:
                    elements.extend(_rsn_els)
                    self._remember_reasoning_snapshot(session, _rsn_full, session.text_sizes)
            except Exception:
                _logger.debug("pinned reasoning elements failed", exc_info=True)
        # 老大 2026-09-21：推理已经在顶部思考块里了，折叠面板只列工具步骤，
        # 标题也不再带「N 轮」；没有工具调用的回合面板整个不出现（否则只剩空壳）。
        # CardKit 实体卡路径没有顶部思考块，仍由面板承载推理，不受影响。
        _tool_steps = session.tool_use.build_display_steps() if state is not None else []
        _total_elapsed_ms = 0.0
        if final and isinstance(footer_data, dict):
            _duration = footer_data.get("duration", 0)
            if isinstance(_duration, (int, float)) and _duration > 0:
                _total_elapsed_ms = _duration * 1000
        if state is not None and state.panel_visible and _tool_steps:
            elements.append(self._build_session_panel(
                session, state,
                current_reasoning_text="" if final else None,
                total_elapsed_ms=_total_elapsed_ms,
                show_reasoning=False,
                expanded=self._cfg.panel_expanded if final else None,
                active_tool=None,
            ))

        if state is not None and state.answer_text:
            elements.append(_streaming_element(
                content=render_answer_markdown(state.answer_text, final=final),
                element_id=ANSWER_ELEMENT_ID,
                text_sizes=session.text_sizes,
            ))

        if final:
            actions = build_preservative_seal_actions(
                partial=partial,
                footer_data=footer_data,
                is_error=is_error,
                is_aborted=is_aborted,
                error_message=error_message,
                footer_fields=footer_fields,
                footer_show_label=footer_show_label,
                existing_elements=set(),
                card_trace_id=session.card_trace_id,
                text_sizes=session.text_sizes,
            )
            for action in actions:
                if action.get("action") == "add_elements":
                    elements.extend(action.get("params", {}).get("elements", []))
        else:
            label, status_key, emoji, brand = self._current_loading_status(session)
            if (not elements or _only_preparation_records(session)) and session._response_phase in ("waiting", "compression"):
                # Before the first model byte, the reversible context row owns
                # waiting, memory retrieval and compression. The spinner takes over after
                # model activity starts (or when content already exists).
                hint_status = self._loading_hint_status(session)
                elements.append(_loading_hint_element(hint_status))
                label, status_key, emoji, brand = None, None, None, None
            elements.append(_loading_element(
                label,
                status_key=status_key,
                emoji=emoji,
                brand=brand,
                text_sizes=session.text_sizes,
            ))

        summary = _build_seal_summary(state) if final else "Processing..."
        _card = build_interactive_card_v2(
            elements=elements,
            summary=summary,
            text_sizes=session.text_sizes,
        )
        # 老大 2026-09-20: 留整卡快照 —— 会话回收后按钮还要能重渲染这张卡
        try:
            self._remember_card_snapshot(session, _card)
        except Exception:
            _logger.debug("remember card snapshot failed", exc_info=True)
        return _card

    async def _do_create_linear_card(self, session: CardSession) -> None:
        """Create the initial placeholder card — loading hint only, no panel."""
        if session.state != IDLE:
            # Completion can claim the session before this coroutine is
            # dequeued. Signal readiness anyway: the completion path blocks on
            # ``_card_ready`` for 30 s and would otherwise degrade a turn that
            # was merely finishing fast.
            session._card_ready.set()
            return
        # Snapshot epoch before async creation
        epoch = session.create_epoch
        session.state = CREATING
        session._create_epoch_snap = epoch
        session.linear = True
        # v1.4.0 fix (问题3 根因1 — delegate_task 后卡片降级纯文本):
        if session.unified_state is None:
            session.unified_state = UnifiedLinearState()

        try:
            session.text_sizes = self._cfg.text_sizes
        except ValueError as e:
            # A malformed text_sizes entry used to raise before this try block
            # and leave the session stuck in CREATING: _card_ready never set,
            # so completion blocked for its full 30 s timeout.
            _logger.error("HLS: invalid text_sizes config, card disabled: %s", e)
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_create_linear_card:text_sizes",
            )
            session._card_ready.set()
            return
        session.interactive_mode = _uses_interactive_card_transport(session.text_sizes)

        try:
            await self._ensure_init()
            assert self._client is not None

            try:
                reply_to = session.anchor_id or session.message_id
                standalone = session.message_id.startswith("hls-recovery-")
                include_hint = False
                if session.interactive_mode:
                    card = self._build_interactive_linear_card(session)
                    if standalone:
                        card_msg_id = await self._client.send_card_to_chat(session.chat_id, card)
                    else:
                        card_msg_id = await self._client.reply_card(reply_to, card)
                    # 现有生命周期守卫使用 card_id 作为卡片已就绪标记。
                    session.card_id = f"im:{card_msg_id}"
                else:
                    # Creation races the stream: if upstream already spoke, skip
                    # the waiting hint entirely and let the spinner row open on
                    # 模型思考中 rather than flashing a stale 等待上游模型响应.
                    label, status_key, emoji, brand = self._current_loading_status(session)
                    loading_hint_status = self._loading_hint_status(session)
                    include_hint = session._response_phase in ("waiting", "compression")
                    if include_hint:
                        label, status_key, emoji, brand = None, None, None, None
                    card = build_streaming_card_v2(
                        include_unified_panel=False,   # Panel added on first token
                        include_answer_element=False,   # Answer element added with panel
                        include_loading_hint=include_hint,  # 等待上游模型响应
                        loading_hint_status=loading_hint_status,
                        loading_status_key=status_key,
                        loading_status_emoji=emoji,
                        loading_status_brand=brand,
                        streaming_panel_expanded=self._cfg.streaming_panel_expanded,
                        print_strategy=self._cfg.print_strategy,
                        print_step=self._cfg.print_step,
                        text_sizes=session.text_sizes,
                    )
                    card_id = await self._client.cardkit_create(card)
                    if standalone:
                        card_msg_id = await self._client.send_card_by_id_to_chat(
                            session.chat_id, card_id
                        )
                    else:
                        card_msg_id = await self._client.reply_card_by_id(reply_to, card_id)
                    session.card_id = card_id
                    session._loading_hint_state = loading_hint_status
                    session._loading_label = label
                    session._loading_status_key = status_key
                session.card_msg_id = card_msg_id
                session.card_created_at = _time.time()
                session.flush.set_throttle(self._cfg.flush_interval_sec)

                # 元素跟踪只供 CardKit 路径使用。
                if session.interactive_mode:
                    session.existing_elements = set()
                else:
                    session.existing_elements = {_LOADING_ELEMENT_ID}
                    if include_hint:
                        session.existing_elements.add(_LOADING_HINT_ELEMENT_ID)
                session._creation_stages.discard("panel")  # Panel NOT in initial card

            except FeishuAPIError as e:
                _logger.info("linear card create failed: %s", e)
                raise

            session.flush.set_card_message_ready(True)

            # ── Stale-create guard ──
            if session.state == CREATING and not session.is_stale_create(epoch):
                session.state = STREAMING

            if session.linear and session.unified_state and (
                session.unified_state.has_dirty or session._pending_flush
            ):
                status_only = session._pending_flush and not session.unified_state.has_dirty
                session._pending_flush = False
                if status_only:
                    # A reasoning-status update is not the first visible answer;
                    # keep the immediate-answer flush available for later.
                    self._fire_and_forget(
                        session.flush.flush_now(lambda: self._do_unified_flush(session)),
                        asyncio.get_running_loop(),
                    )
                elif not session._first_flush_done:
                    # First content → immediate flush (首字即显)
                    session._first_flush_done = True
                    # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
                    # （与 core.py _fire_and_forget 同模式）
                    self._fire_and_forget(
                        session.flush.flush_now(lambda: self._do_unified_flush(session)),
                        asyncio.get_running_loop(),
                    )
                else:
                    # Subsequent content → throttled flush
                    self._schedule_linear_flush(session)

            # Must be set AFTER card_id/card_msg_id are assigned and
            session._card_ready.set()
            _logger.info(
                "HLS: linear card created msg=%s trace=%s linear=%s card_id=%s",
                (session.message_id or "?")[:12],
                session.card_trace_id,
                session.linear,
                (session.card_id or "")[:12],
            )
        except Exception as e:
            _logger.exception("_do_create_linear_card failed")
            # v1.3.4 fix (P1): 消息被删/撤回时触发 UnavailableGuard，避免后续
            # 对已删除消息的无效 API 调用。原实现 guard 从未被调用（死代码）。
            if isinstance(e, FeishuAPIError) and is_terminal_api_code(e.code):
                try:
                    session.guard.terminate("_do_create_linear_card", err=e)
                except Exception:
                    _logger.debug("guard.terminate failed in create path", exc_info=True)
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_create_linear_card",
            )
            # Signal readiness even on failure so awaiters don't deadlock
            session._card_ready.set()

    def _schedule_linear_flush(self, session: CardSession, *, force: bool = False) -> None:
        """Schedule a unified panel flush for the given session."""
        if not session.should_proceed("_schedule_linear_flush"):
            return
        # COMPLETING is not terminal, but we should not schedule new flushes
        if session.state == IDLE:
            if force:
                session._pending_flush = True
            return
        if session.state == COMPLETING:
            return

        state = session.unified_state
        if state is None:
            if force:
                session._pending_flush = True
            return
        if not force and not state.has_dirty:
            return

        if not session.flush._card_message_ready:
            session._pending_flush = True
            return

        if session.interactive_mode:
            session.flush.set_throttle(max(self._cfg.flush_interval_sec, _INTERACTIVE_INTERVAL_SEC))
            if session.flush.cooldown_until > _time.monotonic():
                session.flush.schedule_update(lambda: self._do_unified_flush(session))
                return

        if force and not state.has_dirty:
            # Status-only transition (waiting -> thinking). Do not consume the
            # first-visible-content fast path and do not flush every reasoning
            # token after the transition has already been displayed.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = session._loop
            if loop is not None and not loop.is_closed():
                self._fire_and_forget(
                    session.flush.flush_now(lambda: self._do_unified_flush(session)),
                    loop,
                )
            return

        # ── First-Token Immediate Flush (首字即显) ──
        if not session._first_flush_done:
            session._first_flush_done = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = session._loop
            if loop is not None and not loop.is_closed():
                # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
                self._fire_and_forget(
                    session.flush.flush_now(lambda: self._do_unified_flush(session)),
                    loop,
                )
            return

        _answer_only = state.answer_dirty and not state.panel_dirty and not state.tool_steps_dirty
        if session.interactive_mode:
            session.flush.set_throttle(max(self._cfg.flush_interval_sec, _INTERACTIVE_INTERVAL_SEC))
        elif _answer_only:
            session.flush.set_throttle(_ANSWER_FAST_STREAM_MS)
        else:
            session.flush.set_throttle(self._cfg.flush_interval_sec)

        session.flush.schedule_update(lambda: self._do_unified_flush(session))

    async def _do_unified_flush(self, session: CardSession) -> None:
        """Unified panel flush — max 2 API calls per flush cycle."""
        if session.is_terminal_phase or session.state == COMPLETING:
            return
        if not session.card_id:
            return
        state = session.unified_state
        if state is None:
            return
        assert self._client is not None

        if session.interactive_mode:
            if not session.card_msg_id:
                return
            session.flush.set_cooldown(max(self._cfg.flush_interval_sec, _INTERACTIVE_INTERVAL_SEC))
            try:
                with session._stream_lock:
                    pending_panel = state.panel_dirty
                    pending_answer = state.answer_dirty
                    pending_tools = state.tool_steps_dirty
                    card = self._build_interactive_linear_card(session)
                    state.panel_dirty = False
                    state.answer_dirty = False
                    state.tool_steps_dirty = False
                await self._client.update_card(session.card_msg_id, card)
            except asyncio.CancelledError:
                with session._stream_lock:
                    state.panel_dirty = state.panel_dirty or pending_panel
                    state.answer_dirty = state.answer_dirty or pending_answer
                    state.tool_steps_dirty = state.tool_steps_dirty or pending_tools
                raise
            except Exception as e:
                with session._stream_lock:
                    state.panel_dirty = state.panel_dirty or pending_panel
                    state.answer_dirty = state.answer_dirty or pending_answer
                    state.tool_steps_dirty = state.tool_steps_dirty or pending_tools
                _logger.warning(
                    "interactive card update failed: %s msg=%s",
                    e, (session.card_msg_id or "?")[:12],
                )
                if isinstance(e, FeishuAPIError) and e.code == IM_FREQUENCY_LIMIT:
                    # Coalesce all updates rejected by IM frequency limits and
                    # retry only after the provider's rejection window cools.
                    session.flush.set_cooldown(_INTERACTIVE_COOLDOWN_SEC)
                elif isinstance(e, FeishuAPIError) and is_terminal_api_code(e.code):
                    session.guard.terminate("interactive_flush", err=e)
                    session.flush.mark_completed()
                    return
                self._schedule_linear_flush(session, force=True)
                return
            if state.panel_visible:
                session._creation_stages.add("panel")
            if state.answer_text:
                session._creation_stages.add("answer")
            session._creation_stages.add("hint_removed")
            if state.has_dirty:
                self._schedule_linear_flush(session)
            return

        # Before the phase work: every phase below has early returns, so a call
        # placed after them never runs on the tool-event path.
        await self._sync_loading_context(session)
        await self._sync_loading_label(session)

        actions: list[dict[str, Any]] = []

        # Bug fix (v1.0.5): Split Phase 2 into two sub-paths:
        # The answer element must be created in BOTH paths so the answer text can be
        if "answer" not in session._creation_stages and (state.panel_visible or state.answer_dirty or state.answer_text):
            new_elements: list[dict[str, Any]] = []

            # ── Path A: Has reasoning or tools → add unified panel ──
            if state.panel_visible:
                new_elements.append(self._build_session_panel(session, state))

            # ── Path A & B: Always add answer streaming element ──
            new_elements.append(_streaming_element(
                element_id=ANSWER_ELEMENT_ID,
                text_sizes=session.text_sizes,
            ))

            # Insert above the hint when it exists, otherwise above the spinner
            # — the hint is absent whenever the card opened mid-stream, and the
            # spinner row outlives every other placeholder.
            _anchor = (
                _LOADING_HINT_ELEMENT_ID
                if _LOADING_HINT_ELEMENT_ID in session.existing_elements
                else _LOADING_ELEMENT_ID
            )
            actions.append({
                "action": "add_elements",
                "params": {
                    "type": "insert_before",
                    "target_element_id": _anchor,
                    "elements": new_elements,
                },
            })
            # Delete loading hint
            _keep_preparation_hint = _only_preparation_records(session)
            if _LOADING_HINT_ELEMENT_ID in session.existing_elements and not _keep_preparation_hint:
                actions.append({
                    "action": "delete_elements",
                    "params": {"element_ids": [_LOADING_HINT_ELEMENT_ID]},
                })
            # Note: panel_dirty and tool_steps_dirty are cleared AFTER

            # ── Execute Phase 2 batch_update ──
            if actions:
                _has_panel = state.panel_visible
                _phase2_snapshot = _take_panel_dirty(state)
                session.sequence += 1
                try:
                    await self._client.cardkit_batch_update(
                        session.card_id, actions, sequence=session.sequence,
                    )
                    # Update tracking after success
                    session._creation_stages.add("answer")
                    if not _keep_preparation_hint:
                        session._creation_stages.add("hint_removed")
                    session.existing_elements.add(ANSWER_ELEMENT_ID)
                    if _has_panel:
                        session._creation_stages.add("panel")
                        session.existing_elements.add(UNIFIED_PANEL_ELEMENT_ID)
                    if not _keep_preparation_hint:
                        session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                except FeishuAPIError as e:
                    if e.code == CARDKIT_STREAMING_CLOSED:
                        if session._streaming_closed_logged:
                            pass
                        else:
                            _logger.info(
                                "unified flush: streaming closed, will be handled by TTL or seal: card=%s",
                                session.card_id[:12],
                            )
                            session._streaming_closed_logged = True
                        session._streaming_closed = True
                        return
                    if is_schema_error(e):
                        # Permanent: the snapshot is dropped, not restored.
                        _logger.error(
                            "unified flush phase 2 SCHEMA ERROR (permanent): %s — detail: %s card=%s",
                            e, e.extract_schema_detail(), session.card_id[:12],
                        )
                        state.answer_dirty = False
                        return
                    elif is_element_not_found_error(e):
                        _logger.warning(
                            "unified flush phase 2 element not found (non-fatal): %s card=%s",
                            e, session.card_id[:12],
                        )
                        session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                        state.answer_dirty = False
                        return
                    else:
                        # v1.3.3 fix (P0): transient API error (rate limit, auth
                        _logger.warning(
                            "unified flush phase 2 batch_update failed: %s — "
                            "resetting _first_flush_done for retry, card=%s",
                            e, session.card_id[:12] if session.card_id else "?",
                        )
                        _restore_panel_dirty(state, _phase2_snapshot)
                        session._first_flush_done = False
                        return
                except asyncio.CancelledError:
                    # v1.3.4 fix (P1): CancelledError 是 BaseException 子类，
                    _restore_panel_dirty(state, _phase2_snapshot)
                    session._first_flush_done = False
                    raise
                except Exception as e:
                    # v1.3.3 fix (P0): catch non-FeishuAPIError exceptions (network
                    # forever" bug.
                    _logger.warning(
                        "unified flush phase 2 non-API exception: %s — "
                        "resetting _first_flush_done for retry, card=%s",
                        e, session.card_id[:12] if session.card_id else "?",
                        exc_info=True,
                    )
                    session._first_flush_done = False
                    return

            # Note: skip markdown optimization during streaming for performance;
            content = _take_answer_flush_snapshot(session, state)
            if content is not None and not await _write_answer_stream(
                self._client, session, state, content,
            ):
                return

            if not state.panel_dirty and not state.tool_steps_dirty and not state.answer_dirty:
                return  # Phase 2 done, nothing more to do

        # ── Phase 3: Update existing panel + stream answer ──
        if state.panel_dirty:
            if "panel" in session._creation_stages:
                # Panel exists — update its content
                panel = self._build_session_panel(session, state)
                actions.append(_panel_update_action(panel))
            elif "answer" in session._creation_stages:
                # ── Bug fix (v1.0.5): Late-arriving reasoning/tools ──
                panel = self._build_session_panel(session, state)
                actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": ANSWER_ELEMENT_ID,
                        "elements": [panel],
                    },
                })
                # Note: "panel" stage will be added after API success below
            # Note: panel_dirty and tool_steps_dirty are cleared AFTER

        # ── Delete loading hint if still present (safety net) ──
        _hint_delete_in_batch = False
        has_visible_model_content = (state.panel_visible or bool(state.answer_text)) and not _only_preparation_records(session)
        # Once the spinner row says 模型思考中, leaving 等待上游模型响应 above it
        # would state the opposite. Drop the hint on the first upstream token
        # even when that token produced nothing visible yet (show_reasoning off).
        _spinner_owns_status = (
            session._loading_label_supported
            and session._response_phase in ("thinking", "answer", "tool")
        )
        if (
            (has_visible_model_content or _spinner_owns_status)
            and "hint_removed" not in session._creation_stages
            and _LOADING_HINT_ELEMENT_ID in session.existing_elements
        ):
            actions.append({
                "action": "delete_elements",
                "params": {"element_ids": [_LOADING_HINT_ELEMENT_ID]},
            })
            _hint_delete_in_batch = True

        # ── Execute Phase 3 batch_update ──
        if actions:
            _phase3_snapshot = _take_panel_dirty(state)
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id, actions, sequence=session.sequence,
                )
                if _hint_delete_in_batch:
                    session._creation_stages.add("hint_removed")
                    session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                # ── Track late-arriving panel creation ──
                if "panel" not in session._creation_stages and state.panel_visible:
                    session._creation_stages.add("panel")
                    session.existing_elements.add(UNIFIED_PANEL_ELEMENT_ID)
            except FeishuAPIError as e:
                if e.code == CARDKIT_STREAMING_CLOSED:
                    if session._streaming_closed_logged:
                        pass
                    else:
                        _logger.info(
                            "unified flush: streaming closed, will be handled by TTL or seal: card=%s",
                            session.card_id[:12],
                        )
                        session._streaming_closed_logged = True
                    session._streaming_closed = True
                    return
                if is_schema_error(e):
                    _logger.error(
                        "unified flush phase 3 SCHEMA ERROR (permanent): %s — "
                        "detail: %s — "
                        "clearing dirty flags to stop retry, card=%s",
                        e, e.extract_schema_detail(), session.card_id[:12],
                    )
                    # Permanent: drop the snapshot so the retry loop stops.
                    return
                if is_element_not_found_error(e):
                    # v1.4.1 fix (P1): Phase 3 batch_update 元素不存在 (300315 +
                    # warning 分支 → hint_removed 仍未同步 + existing_elements
                    # 重试。info 级别 (不是 warning/error) — 不是真正的故障。
                    _logger.info(
                        "unified flush phase 3 element not found (non-fatal): %s — "
                        "syncing hint tracking, card=%s",
                        e, session.card_id[:12],
                    )
                    session._creation_stages.add("hint_removed")
                    session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                    # 保留 panel_dirty / tool_steps_dirty 以便下轮 flush 重试
                    _restore_panel_dirty(state, _phase3_snapshot)
                    return
                _logger.warning("unified flush batch_update failed: %s", e)
                _restore_panel_dirty(state, _phase3_snapshot)
                return

        # Note: skip markdown optimization during streaming for performance;
        content = None
        if "answer" in session._creation_stages:
            content = _take_answer_flush_snapshot(session, state)
        if content is not None and not await _write_answer_stream(
            self._client, session, state, content,
        ):
            return

        if state.panel_dirty or state.answer_dirty or state.tool_steps_dirty:
            self._schedule_linear_flush(session)

    def _current_tool_label(self, session: CardSession) -> tuple[str, str] | None:
        """Return the status-row tool label only while the model is in tool phase.

        Reads the status-row view of the tracker, not the newest step: a batch
        that carries a side tool next to ``image_generate`` must not hide
        🎨 GPT · 生成图片 while the image is still rendering.
        """
        if session._response_phase in ("compression", "thinking", "answer"):
            return None
        return session.tool_use.status_tool_names

    def _loading_hint_status(self, session: CardSession) -> str:
        """Show preprocessing only until model/tool activity takes over."""
        if session._response_phase == "compression":
            return "context_compressing"
        if session._response_phase == "waiting" and session._image_analysis_requests:
            return "image_analyzing"
        if session._response_phase == "waiting" and session._memory_prefetch_requests:
            return "openviking_recall"
        if session._response_phase == "waiting" and session._has_native_image_input:
            return "image_processing"
        return "loading_context"

    def _current_loading_status(
        self, session: CardSession,
    ) -> tuple[tuple[str, str] | None, str | None, str | None, str | None]:
        """Spinner-row content as ``(tool_label, status_key, emoji, brand)``.

        The spinner row lives for the whole stream, so it — not the loading
        hint — is what can actually show 模型思考中 to a user: the hint is
        deleted in the same flush that renders the first visible content.

        A running tool's name still wins; 模型思考中 fills the row whenever the
        upstream has spoken but no tool is holding it. Both states carry an
        emoji so the text doesn't shift sideways when one replaces the other.

        ``brand`` is the artwork name for tools whose emoji is a stand-in (see
        ``_tool_brand``). It has to travel with the emoji: passing only the
        emoji left the spinner showing 🐙 for a `gh` call while the tool row
        below it already carried the GitHub mark.
        """
        if session._response_phase == "compression":
            if _LOADING_HINT_ELEMENT_ID in session.existing_elements:
                # The hint row is the visible compression status until the
                # spinner is the only remaining placeholder.
                return None, None, None, None
            return None, "context_compressing", _COMPRESSION_EMOJI, None
        if session._response_phase == "waiting" and session._memory_prefetch_requests:
            if _LOADING_HINT_ELEMENT_ID in session.existing_elements:
                return None, None, None, None
            return None, self._loading_hint_status(session), None, None
        label = self._current_tool_label(session)
        if label:
            return label, None, session.tool_use.status_tool_emoji, session.tool_use.status_tool_brand
        if session._response_phase == "waiting":
            # the hint element still says 等待上游模型响应
            return None, None, None, None
        return None, "model_thinking", _THINKING_EMOJI, None

    def _active_tool_for_header(self, session: CardSession) -> tuple[str, str] | None:
        """Active tool for the panel title — only when the spinner label can't carry it."""
        if session._loading_label_supported:
            return None
        return self._current_tool_label(session)

    async def _sync_loading_context(self, session: CardSession) -> None:
        """Update pre-model status or the fallback for unsupported spinner text."""
        if session.interactive_mode or session._streaming_closed:
            return
        if not session.card_id or _LOADING_HINT_ELEMENT_ID not in session.existing_elements:
            return

        if session._response_phase in ("waiting", "compression"):
            status_key = self._loading_hint_status(session)
        elif session._loading_label_supported:
            return  # spinner row owns model/tool status
        else:
            status_key = "model_thinking"
        if status_key == session._loading_hint_state:
            return
        hint = _loading_hint_element(status_key)
        actions = [{
            "action": "partial_update_element",
            "params": {
                "element_id": _LOADING_HINT_ELEMENT_ID,
                # Preserve the existing lark_md tag on partial updates.
                "partial_element": {
                    "icon": hint["icon"],
                    "text": {
                        "content": hint["text"]["content"],
                        "i18n_content": hint["text"]["i18n_content"],
                    },
                },
            },
        }]
        session.sequence += 1
        try:
            await self._client.cardkit_batch_update(
                session.card_id, actions, sequence=session.sequence,
            )
            session._loading_hint_state = status_key
        except FeishuAPIError as e:
            if e.code == CARDKIT_STREAMING_CLOSED:
                session._streaming_closed = True
                return
            _logger.debug("HLS: loading context update failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.debug("HLS: loading context update error: %s", e)

    async def _sync_loading_label(self, session: CardSession) -> None:
        """Push the live model/tool status into the spinner row beside the dots.

        Carries 模型思考中 from the first upstream token until a tool starts,
        then the tool's name, then back again once the tool returns.

        Sent as its own batch_update: a rejected div-text update must not take
        the panel or answer actions down with it.
        """
        if session.interactive_mode:
            return  # whole-card rebuild already carries the label
        if not session._loading_label_supported or session._streaming_closed:
            return
        if not session.card_id or _LOADING_ELEMENT_ID not in session.existing_elements:
            return

        label, status_key, emoji, brand = self._current_loading_status(session)
        if (label == session._loading_label and status_key == session._loading_status_key
                and brand == session._loading_brand):
            return

        actions = [{
            "action": "partial_update_element",
            "params": {
                "element_id": _LOADING_ELEMENT_ID,
                # The row is a fixed two-column column_set. Columns (and the
                # cell inside them) carry no element_id of their own, so the
                # whole row is resent; tag stays column_set on every update.
                "partial_element": _loading_element(
                    label, status_key=status_key, emoji=emoji, brand=brand,
                    text_sizes=session.text_sizes,
                ),
            },
        }]
        session.sequence += 1
        try:
            await self._client.cardkit_batch_update(
                session.card_id, actions, sequence=session.sequence,
            )
            session._loading_label = label
            session._loading_status_key = status_key
            session._loading_brand = brand
        except FeishuAPIError as e:
            if e.code == CARDKIT_STREAMING_CLOSED:
                session._streaming_closed = True
                return
            if is_schema_error(e):
                # Feishu refuses partial updates of a div's text — stop trying
                # and let the panel title carry the status instead.
                _logger.info(
                    "HLS: spinner label unsupported (%s), falling back to panel title card=%s",
                    e, session.card_id[:12],
                )
                session._loading_label_supported = False
                state = session.unified_state
                if state is not None and state.panel_visible:
                    state.panel_dirty = True
                return
            _logger.debug("HLS: spinner label update failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.debug("HLS: spinner label update error: %s", e)

    def _linear_on_thinking(self, session: CardSession, text: str) -> None:
        """Handle a thinking/reasoning delta in linear mode."""
        split = split_reasoning_text(text)
        reasoning = split.get("reasoning_text")
        answer = split.get("answer_text")

        phase_changed = False
        if reasoning:
            phase_changed = session._response_phase != "thinking"
            session._compression_previous_phase = None
            session._response_phase = "thinking"
        elif answer:
            # Same first-token rule as on_answer: interim text counts as the
            # upstream's first response even without reasoning markers.
            session._compression_previous_phase = None
            session._response_phase = "answer"
        elif text:
            # Some providers emit a standalone reasoning marker first (for
            # example ``<think>``). There is no visible content to add yet,
            # but the upstream has already spoken and the waiting hint must
            # yield to the thinking state.
            phase_changed = session._response_phase != "thinking"
            session._compression_previous_phase = None
            session._response_phase = "thinking"

        state = session.unified_state
        if state is None:
            self._schedule_linear_flush(session, force=phase_changed)
            return

        _reasoning_already_tracked = bool(state._current_reasoning)
        if reasoning and self._cfg.show_reasoning and not _reasoning_already_tracked:
            state.on_reasoning_delta(reasoning)
        if answer:
            # Interim thinking calls may carry the answer accumulated so far.
            _new_part = _merge_streamed_answer(state, answer)
            if _new_part:
                _logger.info(
                    "HLS: _linear_on_thinking appends incremental answer "
                    "added=%d total=%d msg=%s",
                    len(_new_part), len(state.answer_text),
                    (session.message_id or "?")[:12],
                )
        if reasoning or answer or phase_changed:
            self._schedule_linear_flush(session, force=phase_changed)

    async def _preservative_seal(
        self,
        session: CardSession,
        *,
        partial: bool = False,
        footer_data: dict | None = None,
        is_error: bool = False,
        is_aborted: bool = False,
        error_message: str = "",
        footer_fields: list[list[str]] | None = None,
        footer_show_label: bool = False,
    ) -> bool:
        """Preservative seal — update panel + add footer + close streaming."""
        assert self._client is not None
        card_id = session.card_id
        assert card_id is not None

        try:
            # Before closing streaming, we MUST flush any remaining dirty
            # "footer appears before content finishes" bug.
            state = session.unified_state
            if state is not None and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty):
                _logger.warning(
                    "preservative seal: dirty data detected at seal time "
                    "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s card=%s — "
                    "flushing before close",
                    state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                    card_id[:12],
                )
                # ── Flush remaining panel content ──
                if (state.panel_dirty or state.tool_steps_dirty) and "panel" in session._creation_stages:
                    panel = self._build_session_panel(
                        session, state,
                        total_elapsed_ms=_footer_elapsed_ms(footer_data),
                    )
                    try:
                        session.sequence += 1
                        await self._client.cardkit_batch_update(
                            session.card_id,
                            [_panel_update_action(panel)],
                            sequence=session.sequence,
                        )
                        state.panel_dirty = False
                        state.tool_steps_dirty = False
                    except FeishuAPIError as e:
                        if e.code == CARDKIT_STREAMING_CLOSED:
                            _logger.info("seal drain: streaming already closed, skipping panel flush")
                            session._streaming_closed = True
                        else:
                            _logger.warning("seal drain panel failed: %s", e)
                        state.panel_dirty = False
                        state.tool_steps_dirty = False

                # ── Flush remaining answer text ──
                if state.answer_dirty and "answer" in session._creation_stages and not session._streaming_closed:
                    content = render_answer_markdown(state.answer_text)
                    try:
                        session.sequence += 1
                        _logger.info(
                            "HLS: seal drain answer text len=%d card=%s",
                            len(content), card_id[:12],
                        )
                        await self._client.cardkit_stream_element(
                            session.card_id, ANSWER_ELEMENT_ID, content,
                            sequence=session.sequence,
                        )
                        state.answer_dirty = False
                    except FeishuAPIError as e:
                        # v1.1.1: 统一 fallback — 300309 和 300313 都改用 batch_update（不带 tag）
                        if e.code == CARDKIT_STREAMING_CLOSED or is_element_not_found_error(e):
                            if e.code == CARDKIT_STREAMING_CLOSED:
                                session._streaming_closed = True
                            _logger.info(
                                "HLS: seal drain answer — %s, falling back to partial_update_element card=%s",
                                "streaming closed" if e.code == CARDKIT_STREAMING_CLOSED else "300313",
                                card_id[:12],
                            )
                            session.sequence += 1
                            await _fallback_write_answer(
                                self._client, session.card_id, content,
                                sequence=session.sequence,
                            )
                        else:
                            _logger.warning("HLS: seal drain answer failed: %s", e)
                        state.answer_dirty = False

            # ── Step 1: Update unified panel to final state (non-streaming) ──
            seal_actions: list[dict[str, Any]] = []
            panel: dict[str, Any] | None = None

            if state is not None:
                state.finalize()

                # ── Bug fix (v1.0.5): Only update panel if it was created ──
                if "panel" in session._creation_stages:
                    panel = self._build_session_panel(
                        session, state,
                        current_reasoning_text="",
                        total_elapsed_ms=_footer_elapsed_ms(footer_data),
                        expanded=self._cfg.panel_expanded,
                        active_tool=None,
                    )
                    seal_actions.append(_panel_update_action(panel))

            # v1.3.1 fix: Do NOT skip this step even when the answer was already fully
            # guard) is a minor visual issue; content truncation is a P0 data-loss bug.
            if state is not None and state.answer_text and "answer" in session._creation_stages:
                optimized_content = render_answer_markdown(state.answer_text, final=True)
                seal_actions.append(_answer_update_action(optimized_content))

            # ── Step 3: Add footer + delete loading elements ──
            seal_actions.extend(
                build_preservative_seal_actions(
                    partial=partial,
                    footer_data=footer_data,
                    is_error=is_error,
                    is_aborted=is_aborted,
                    error_message=error_message,
                    footer_fields=footer_fields,
                    footer_show_label=footer_show_label,
                    existing_elements=session.existing_elements,
                    card_trace_id=session.card_trace_id,
                    text_sizes=session.text_sizes,
                )
            )

            if panel is not None:
                _trim_card_to_budget(
                    _simulated_seal_body(panel, state, seal_actions),
                    text_sizes=session.text_sizes,
                    label="preservative seal",
                )

            if seal_actions:
                session.sequence += 1
                await self._client.cardkit_batch_update(
                    card_id, seal_actions, sequence=session.sequence,
                )

            # When closing streaming, we MUST also update the card's summary
            # bug the user reported.
            # CRITICAL: Only call close_streaming ONCE per card lifecycle.
            # updated to config.summary.content.  The summary MUST be
            seal_summary = _build_seal_summary(state)

            if not session._streaming_closed:
                session.sequence += 1
                _logger.info(
                    "HLS: preservative seal closing streaming card=%s trace=%s seq=%d summary=%s",
                    card_id[:12], session.card_trace_id, session.sequence,
                    repr(seal_summary[:40]) if seal_summary else "(empty)",
                )
                # ── Bug fix (v1.0.3): Pass summary IN close_streaming ──
                # must be in THIS request — passing summary="" and then
                await self._client.cardkit_close_streaming(
                    card_id, sequence=session.sequence, summary=seal_summary,
                )
                session._streaming_closed = True
            else:
                _logger.info(
                    "preservative seal: streaming already closed, skipping close_streaming card=%s",
                    card_id[:12],
                )
                if seal_summary:
                    try:
                        session.sequence += 1
                        await self._client.cardkit_update_summary(
                            card_id, seal_summary, sequence=session.sequence,
                        )
                        _logger.info(
                            "preservative seal: summary updated (streaming already closed) "
                            "card=%s seq=%d summary=%s",
                            card_id[:12], session.sequence,
                            repr(seal_summary[:40]),
                        )
                    except FeishuAPIError as e:
                        _logger.warning(
                            "preservative seal: summary update failed (already closed) "
                            "card=%s error=%s",
                            card_id[:12], e,
                        )

            return True

        except FeishuAPIError as e:
            if e.code == CARDKIT_SEQUENCE_CONFLICT:
                _logger.warning(
                    "preservative seal: sequence conflict, retrying... card=%s seq=%d",
                    card_id[:12], session.sequence,
                )
                for retry in range(2):
                    try:
                        retry_actions: list[dict[str, Any]] = []
                        retry_panel: dict[str, Any] | None = None
                        if state is not None:
                            # ── Bug fix (v1.0.5): Only update panel if it was created ──
                            if "panel" in session._creation_stages:
                                retry_panel = self._build_session_panel(
                                    session, state,
                                    current_reasoning_text="",
                                    total_elapsed_ms=_footer_elapsed_ms(footer_data),
                                    expanded=self._cfg.panel_expanded,
                                    active_tool=None,
                                )
                                retry_actions.append(_panel_update_action(retry_panel))
                            # v1.3.1: same fix as main seal path — always send final
                            # (see v1.3.1 fix comment in main seal path above).
                            if state.answer_text and "answer" in session._creation_stages:
                                optimized_content = render_answer_markdown(state.answer_text, final=True)
                                retry_actions.append(_answer_update_action(optimized_content))
                        retry_actions.extend(
                            build_preservative_seal_actions(
                                partial=partial,
                                footer_data=footer_data,
                                is_error=is_error,
                                is_aborted=is_aborted,
                                error_message=error_message,
                                footer_fields=footer_fields,
                                footer_show_label=footer_show_label,
                                existing_elements=session.existing_elements,
                                card_trace_id=session.card_trace_id,
                                text_sizes=session.text_sizes,
                            )
                        )
                        if retry_panel is not None:
                            _trim_card_to_budget(
                                _simulated_seal_body(retry_panel, state, retry_actions),
                                text_sizes=session.text_sizes,
                                label="preservative seal retry",
                            )
                        # batch_update BEFORE close_streaming (same order as try block)
                        if retry_actions:
                            session.sequence += 1
                            await self._client.cardkit_batch_update(
                                card_id, retry_actions, sequence=session.sequence,
                            )

                        # Close streaming AFTER batch_update
                        if not session._streaming_closed:
                            # Recompute seal_summary for retry (state may have changed)
                            retry_summary = _build_seal_summary(state)
                            session.sequence += 1
                            await self._client.cardkit_close_streaming(
                                card_id, sequence=session.sequence, summary=retry_summary,
                            )
                            session._streaming_closed = True

                        _logger.info(
                            "preservative seal: retry %d succeeded card=%s",
                            retry + 1, card_id[:12],
                        )
                        return True
                    except FeishuAPIError as retry_e:
                        if retry_e.code == CARDKIT_SEQUENCE_CONFLICT:
                            continue
                        # v1.3.4 fix (P1): retry 路径 300309 应与主路径一致 return False
                        if retry_e.code == CARDKIT_STREAMING_CLOSED:
                            session._streaming_closed = True
                            return False
                        raise
                # All retries exhausted
                _logger.warning(
                    "preservative seal: retry exhausted after sequence conflicts card=%s",
                    card_id[:12],
                )
                return False
            if e.code == CARDKIT_STREAMING_CLOSED:
                session._streaming_closed = True
            return False
        except Exception:
            return False

    async def _do_linear_complete(self, session: CardSession) -> bool:
        """Complete the card with the unified panel architecture."""
        if session.guard.should_skip("_do_linear_complete"):
            return False

        # ── Step 1: Wait for any in-progress flush to finish ──
        await session.flush.wait_for_flush()

        if session.interactive_mode:
            return await self._do_interactive_linear_complete(session)

        # without being flushed.  We must drain it ALL here, before
        # the "footer appears before content finishes" bug.
        state = session.unified_state
        _MAX_DRAIN_ROUNDS = 8
        _DRAIN_YIELD_SEC = 0.020  # 20ms yield — enough for worker thread callbacks
        for _drain_round in range(_MAX_DRAIN_ROUNDS):
            if not (
                state is not None
                and session.card_id
                and "answer" in session._creation_stages
                and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty)
            ):
                break  # No dirty data — drain complete

            _logger.info(
                "linear complete: drain round %d/%d "
                "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s msg=%s",
                _drain_round + 1, _MAX_DRAIN_ROUNDS,
                state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                (session.message_id or "?")[:12],
            )
            assert self._client is not None

            # ── Drain panel content ──
            if state.panel_dirty and "panel" in session._creation_stages:
                panel = self._build_session_panel(session, state)
                drain_actions: list[dict[str, Any]] = [_panel_update_action(panel)]
                try:
                    session.sequence += 1
                    await self._client.cardkit_batch_update(
                        session.card_id, drain_actions, sequence=session.sequence,
                    )
                    state.panel_dirty = False
                    state.tool_steps_dirty = False
                except FeishuAPIError as e:
                    if e.code == CARDKIT_STREAMING_CLOSED:
                        # v1.2.0 Y3: drain 阶段也用 _streaming_closed_logged 去重
                        if session._streaming_closed_logged:
                            _logger.debug("drain: streaming already closed (already logged)")
                        else:
                            _logger.info("drain: streaming already closed, skipping")
                            session._streaming_closed_logged = True
                        session._streaming_closed = True
                    elif is_schema_error(e):
                        _logger.error("drain SCHEMA ERROR: %s — detail: %s", e, e.extract_schema_detail())
                        state.panel_dirty = False
                        state.tool_steps_dirty = False
                    else:
                        _logger.warning("drain panel failed: %s", e)

            # ── Drain answer text ──
            if state.answer_dirty and "answer" in session._creation_stages:
                content = render_answer_markdown(state.answer_text)
                try:
                    session.sequence += 1
                    _logger.info(
                        "HLS: drain answer text len=%d msg=%s",
                        len(content), (session.message_id or "?")[:12],
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id, ANSWER_ELEMENT_ID, content,
                        sequence=session.sequence,
                    )
                    state.answer_dirty = False
                except FeishuAPIError as e:
                    # v1.1.1: 统一 fallback — 300309 和 300313 都改用 batch_update（不带 tag）
                    # 之前 300309 直接 skip 答案丢失；300313 的 fallback 带 tag 报 300312
                    if e.code == CARDKIT_STREAMING_CLOSED or is_element_not_found_error(e):
                        if e.code == CARDKIT_STREAMING_CLOSED:
                            session._streaming_closed = True
                        # v1.2.0 Y3: streaming closed 日志去重；300313 仍每次打（非重复事件）
                        if e.code == CARDKIT_STREAMING_CLOSED and session._streaming_closed_logged:
                            pass
                        else:
                            _logger.info(
                                "HLS: drain answer — %s, falling back to partial_update_element msg=%s",
                                "streaming closed" if e.code == CARDKIT_STREAMING_CLOSED else "300313",
                                (session.message_id or "?")[:12],
                            )
                            if e.code == CARDKIT_STREAMING_CLOSED:
                                session._streaming_closed_logged = True
                        session.sequence += 1
                        ok = await _fallback_write_answer(
                            self._client, session.card_id, content,
                            sequence=session.sequence,
                        )
                        if ok:
                            state.answer_dirty = False
                    else:
                        _logger.warning("HLS: drain answer failed: %s", e)

            if _drain_round < _MAX_DRAIN_ROUNDS - 1:
                await asyncio.sleep(_DRAIN_YIELD_SEC)

        # ── Final drain check: log warning if dirty data remains ──
        if state is not None and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty):
            _logger.warning(
                "linear complete: dirty data remains after %d drain rounds "
                "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s msg=%s — "
                "will be flushed by preservative seal before close_streaming",
                _MAX_DRAIN_ROUNDS,
                state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                (session.message_id or "?")[:12],
            )

        # ── Step 3: Mark flush as completed — no more updates accepted ──
        session.flush.mark_completed()

        try:
            await asyncio.wait_for(session._card_ready.wait(), timeout=30.0)
        except TimeoutError:
            _logger.warning("complete: card creation timed out: msg=%s", (session.message_id or "?")[:12])

        if not session.card_id:
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_linear_complete",
            )
            return False

        # ── Step 4: Finalize state ──
        if state:
            state.finalize()

        # ── Build footer data ──
        footer_data = session.footer
        # v1.3.4 fix (P2): bg_review_messages 存在 state 中但从未传给
        if state and state.bg_review_messages:
            if footer_data is None:
                footer_data = {}
            footer_data = {**footer_data, "bg_review_messages": list(state.bg_review_messages)}
        is_aborted = getattr(session, "_was_aborted", False) or session.state == ABORTED
        error_message = getattr(session, "error_message", "")
        # v1.2.0 B1 fix: is_error 必须兼顾 error_message。
        is_error = (
            session.state in (CREATION_FAILED, TERMINATED)
            or (bool(error_message) and not is_aborted)
        )

        # ── Step 5: Preservative seal (the only completion path) ──
        seal_ok = await self._preservative_seal(
            session,
            footer_data=footer_data,
            is_error=is_error,
            is_aborted=is_aborted,
            error_message=error_message,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
        )

        if seal_ok:
            # v1.3.4 fix (P1): 如果会话已被 on_aborted 标记为 ABORTED，
            if session._was_aborted:
                session.state = ABORTED
            else:
                session.state = COMPLETED
            # v1.1.1: 释放重数据（unified_state/text/tool_use），减少内存占用
            # session 留最小元数据等 _prune_stale_sessions 清理
            try:
                self._release_session_data(session)
            except Exception:
                _logger.debug("HLS: release session data failed", exc_info=True)
            # v1.1.0: Record metrics
            try:
                from ..aowen import record_card_completed
                record_card_completed()
            except Exception:
                _logger.debug('metrics: record_card_completed failed', exc_info=True)
        else:
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_linear_complete_seal_failed",
            )
            # v1.1.1: 失败也释放重数据
            try:
                self._release_session_data(session)
            except Exception:
                _logger.debug("HLS: release session data failed", exc_info=True)
            # v1.1.0: Record metrics
            try:
                from ..aowen import record_card_failed
                record_card_failed()
            except Exception:
                _logger.debug('metrics: record_card_failed failed', exc_info=True)

        return seal_ok

    async def _do_interactive_linear_complete(self, session: CardSession) -> bool:
        """通过整卡替换完成使用设备差异字号的普通 IM 卡片。"""
        with session._stream_lock:
            if session._interactive_seal_started or session._disposed:
                return True
            session._interactive_seal_started = True
        session.flush.mark_completed()
        await session.flush.wait_for_flush()
        try:
            await asyncio.wait_for(session._card_ready.wait(), timeout=30.0)
        except TimeoutError:
            _logger.warning(
                "interactive complete: card creation timed out: msg=%s",
                (session.message_id or "?")[:12],
            )

        state = session.unified_state
        if not session.card_msg_id or state is None or self._client is None:
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_interactive_linear_complete",
            )
            return False

        state.finalize()
        # 老大 2026-09-20：正文出完后思考过程自动收回，只留按钮。必须在构建
        # 封口卡之前置回，封口卡就是按这个标志渲染的；放在 enter_terminal 里
        # 不管用 —— 正常完成路径不走 enter_terminal，而且那时卡已经发出去了。
        state.reasoning_expanded = False
        footer_data = session.footer
        if state.bg_review_messages:
            footer_data = {**(footer_data or {}), "bg_review_messages": list(state.bg_review_messages)}
        is_aborted = getattr(session, "_was_aborted", False) or session.state == ABORTED
        error_message = getattr(session, "error_message", "")
        is_error = (
            session.state in (CREATION_FAILED, TERMINATED)
            or (bool(error_message) and not is_aborted)
        )

        message_id = session.card_msg_id
        epoch = session.create_epoch
        card = None
        try:
            while self._interactive_seal_is_current(session, message_id, epoch):
                is_aborted = session._was_aborted or session.state == ABORTED
                card = self._build_interactive_linear_card(
                    session,
                    final=True,
                    footer_data=footer_data,
                    is_error=is_error,
                    is_aborted=is_aborted,
                    error_message=error_message,
                    footer_fields=self._cfg.footer_fields,
                    footer_show_label=self._cfg.footer_show_label,
                )
                session.fallback_card = card
                await self._client.update_card(
                    message_id, card, retry_frequency_limit=True,
                    should_update=lambda is_aborted=is_aborted: (
                        self._interactive_seal_is_current(session, message_id, epoch)
                        and (session._was_aborted or session.state == ABORTED) == is_aborted
                    ),
                )
                if (session._was_aborted or session.state == ABORTED) == is_aborted:
                    break
                # /stop during a backoff invalidates the normal-completion
                # snapshot. Rebuild once with the now-stopped terminal status.
            if not self._interactive_seal_is_current(session, message_id, epoch):
                session.fallback_card = None
                return True
        except Exception as e:
            _logger.warning(
                "interactive card seal failed: %s msg=%s",
                e, message_id[:12],
            )
            if isinstance(e, FeishuAPIError) and is_terminal_api_code(e.code):
                session.guard.terminate("interactive_seal", err=e)
                session.fallback_card = None
                return True
            if not self._interactive_seal_is_current(session, message_id, epoch):
                session.fallback_card = None
                return True
            if card is not None and (session._was_aborted or session.state == ABORTED) != is_aborted:
                # A stop can arrive during the last rejected HTTP request,
                # not only during backoff. Fallback/repair must carry it too.
                card = self._build_interactive_linear_card(
                    session, final=True, footer_data=footer_data,
                    is_error=is_error, is_aborted=True,
                    error_message=error_message,
                    footer_fields=self._cfg.footer_fields,
                    footer_show_label=self._cfg.footer_show_label,
                )
                session.fallback_card = card
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_interactive_linear_complete_seal_failed",
            )
            if isinstance(e, FeishuAPIError) and e.code == IM_FREQUENCY_LIMIT and card:
                self._schedule_interactive_seal_recovery(session, message_id, card)
            self._release_session_data(session)
            return False

        session.fallback_card = None
        self._finish_interactive_seal(session)
        return True

    def _interactive_seal_is_current(
        self, session: CardSession, message_id: str, epoch: int,
    ) -> bool:
        """Never redirect a saved final card to a replacement message/session."""
        current = self._sess_get(session.message_id)
        return (
            not session._disposed
            and session.state != TERMINATED
            and session.card_msg_id == message_id
            and session.create_epoch == epoch
            and (current is None or current is session)
            and not is_unavailable(message_id)
            and not session.guard.should_skip("interactive_seal")
        )

    def _schedule_interactive_seal_recovery(
        self, session: CardSession, message_id: str, card: dict[str, Any],
    ) -> None:
        if session.seal_task is not None or session._disposed:
            return
        task = asyncio.get_running_loop().create_task(
            self._recover_interactive_seal(
                session, message_id, card,
                epoch=session.create_epoch, was_aborted=session._was_aborted,
            ),
        )
        session.seal_task = task
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _recover_interactive_seal(
        self, session: CardSession, message_id: str, card: dict[str, Any],
        *, epoch: int, was_aborted: bool,
    ) -> None:
        """Bounded repair of the original frozen card; never sends more replies."""

        def still_current() -> bool:
            return (
                self._sess_get(session.message_id) is session
                and self._interactive_seal_is_current(session, message_id, epoch)
                and session.state == CREATION_FAILED
                and session._was_aborted == was_aborted
            )

        try:
            for delay in _INTERACTIVE_RECOVERY_DELAYS:
                await asyncio.sleep(delay)
                if not still_current() or self._client is None:
                    return
                try:
                    await self._client.update_card(
                        message_id, card, should_update=still_current,
                    )
                except FeishuAPIError as e:
                    if is_terminal_api_code(e.code):
                        session.guard.terminate("interactive_seal_recovery", err=e)
                        return
                    if e.code != IM_FREQUENCY_LIMIT:
                        _logger.warning("interactive seal recovery rejected", exc_info=True)
                        return
                    continue
                except Exception:
                    _logger.warning("interactive seal recovery failed", exc_info=True)
                    return
                if still_current():
                    self._finish_interactive_seal(session)
                    _logger.info("interactive seal recovered: msg=%s", message_id[:12])
                return
            _logger.warning("interactive seal recovery exhausted: msg=%s", message_id[:12])
        finally:
            session.seal_task = None

    def _finish_interactive_seal(self, session: CardSession) -> None:
        session.fallback_card = None
        session.state = ABORTED if session._was_aborted else COMPLETED
        session.terminal_reason = TerminalReason.ABORT if session._was_aborted else TerminalReason.NORMAL
        session.terminal_source = "interactive_seal"
        try:
            self._release_session_data(session)
        except Exception:
            _logger.debug("HLS: release interactive session data failed", exc_info=True)
        try:
            from ..aowen import record_card_completed
            record_card_completed()
        except Exception:
            _logger.debug("metrics: record_card_completed failed", exc_info=True)

__all__ = [
    "UnifiedControllerMixin",
]
