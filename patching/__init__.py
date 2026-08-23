"""Runtime monkey patching — replaces AST source injection at import time."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from .. import __version__

try:
    from .hermes_adapter import HermesCompat, _try_import
except ImportError:  # pragma: no cover — fallback for pytest-only path
    from hermes_lark_streaming.patching.hermes_adapter import (  # type: ignore[no-redef]
        HermesCompat,
        _try_import,
    )

__all__ = [
    # Shared state
    '_thread_local_ctx',
    '_logger',
    '_msg_ctx',
    '_started_msg_ids',
    '_started_msg_ids_lock',
    '_gateway_cards',
    '_gateway_cards_lock',
    '_session_contexts',
    '_session_contexts_lock',
    '_gw_runner_patched',
    '_patch_status',
    # v1.4.0: FeishuAdapter patched-class registry (deferred loading fix)
    '_patched_feishu_classes',
    # Functions
    '_get_config',
    '_get_event_message_id',
    '_get_thread_local_ctx',
    '_apply_gateway_runner_patches',
    'apply_patches',
        '_apply_direct_agent_patch',
    # v1.6.2: generalized deferred retry (all targets, not just GatewayRunner)
    '_run_pending_patches',
    '_start_deferred_patch_thread',
    # FeishuAdapter patch helpers
    '_apply_feishu_adapter_patches',
    '_verify_feishu_patch_identity',
    # v1.6.0: hook platform_registry.create_adapter — main-chain fix for deferred loading
    '_wrap_platform_registry_create_adapter',
    '_apply_create_adapter_hook',
    # From gateway
    '_wrap_handle_message',
    '_wrap_handle_message_with_agent',
    '_wrap_run_agent',
    '_wrap_run_background_task',
    '_wrap_cron_deliver',
    '_wrap_run_conversation',
    # From callbacks
    '_maybe_wrap_callbacks',
    # From adapter
    '_classify_gateway_message',
    '_wrap_feishu_adapter_send',
    '_register_gateway_card',
    '_unregister_gateway_card',
    '_wrap_feishu_adapter_edit',
    '_wrap_feishu_adapter_add_reaction',
    '_wrap_feishu_adapter_delete_reaction',
    '_wrap_feishu_adapter_send_clarify',
        '_wrap_handle_card_action_event',
    '_handle_clarify_card_action',
    '_REACTION_STATUS_MAP',
    '_clarify_choices',
    '_clarify_questions',
    '_clarify_card_msg_ids',
    '_clarify_selections',
    '_clarify_answers',
    '_clarify_card_info',
    # From hooks
    'on_feishu_normalize',
    'on_message_started',
    'on_message_completed',
    'on_tool_updated',
    'on_answer_delta',
    'on_thinking_delta',
    'on_compression_started',
    'on_compression_completed',
    'on_model_activity',
    'on_reasoning_delta',
    'on_background_review_message',
    'on_message_aborted',
    'on_message_interrupted',
    'on_cron_deliver',
    '_safe_hook',
]

# Thread-local storage for context propagation into worker threads
_thread_local_ctx = threading.local()
_thread_local_ctx.data = None

_logger = logging.getLogger("hermes_lark_streaming")

def _get_config():
    from ..config import Config
    return Config()

_msg_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "hermes_lark_streaming_msg_ctx", default=None
)

_started_msg_ids: set[str] = set()
_started_msg_ids_lock = threading.Lock()

_gateway_cards: dict[str, dict[str, Any]] = {}
_gateway_cards_lock = threading.Lock()

# Explicit cross-thread context handoff keyed by Hermes session id. Hermes
# executes AIAgent.run_conversation in a worker thread; ContextVar propagation
# has changed across gateway releases, while the session id is stable on both
# sides of that boundary.
_session_contexts: dict[str, dict[str, Any]] = {}
_session_contexts_lock = threading.Lock()

_gw_runner_patched: bool = False

_patch_status: dict[str, Any] = {}

_patched_feishu_classes: set[int] = set()

# v1.6.2: per-target completion flags for the deferred retry loop.
#
# _resolve_modules() no longer force-imports host modules (a blocking import on
# Hermes' plugin-discovery thread deadlocks the gateway — see _try_import), so
# ANY target can now start out unresolved, not just GatewayRunner.  The deferred
# thread retries whatever is still missing.  _wrap_run_conversation and
# _wrap_cron_deliver carry no double-wrap marker of their own, so these flags are
# what keeps a retry from wrapping an already-wrapped callable twice.
_conversation_loop_patched: bool = False
_aiagent_patched: bool = False
_cron_patched: bool = False
_feishu_patched: bool = False
_create_adapter_hooked: bool = False

_deferred_thread: threading.Thread | None = None
_deferred_thread_lock = threading.Lock()

# When both the module-level patch and the direct AIAgent patch are active,
# The guard prevents the second call from injecting the prefix again.

def _get_event_message_id() -> str | None:
    ctx = _msg_ctx.get()
    if ctx is None:
        ctx = _get_thread_local_ctx()
    if ctx is None:
        return None
    # Recovery turns may not have a platform event id. The per-turn
    # message_id is still unique inside the controller and is sufficient to
    # keep all callbacks on the same streaming card.
    return ctx.get("event_message_id") or ctx.get("message_id")

def _get_thread_local_ctx() -> dict | None:
    return getattr(_thread_local_ctx, "data", None)

# These imports must come AFTER shared state is defined to avoid circular

from .gateway import (  # noqa: E402
    _wrap_handle_message,
    _wrap_handle_message_with_agent,
    _wrap_run_agent,
    _wrap_run_background_task,
    _wrap_cron_deliver,
    _wrap_run_conversation,
)
from .callbacks import (  # noqa: E402
    _maybe_wrap_callbacks,
)
from .adapter import (  # noqa: E402
    _classify_gateway_message,
    _wrap_feishu_adapter_send,
    _register_gateway_card,
    _unregister_gateway_card,
    _wrap_feishu_adapter_edit,
    _wrap_feishu_adapter_add_reaction,
    _wrap_feishu_adapter_delete_reaction,
    _wrap_feishu_adapter_send_clarify,
    _wrap_handle_card_action_event,
    _handle_clarify_card_action,
    _REACTION_STATUS_MAP,
    _clarify_choices,
    _clarify_questions,
    _clarify_card_msg_ids,
    _clarify_selections,
    _clarify_answers,
    _clarify_card_info,
)
from .hooks import (  # noqa: E402
    on_feishu_normalize,
    on_message_started,
    on_message_completed,
    on_tool_updated,
    on_answer_delta,
    on_thinking_delta,
    on_compression_started,
    on_compression_completed,
    on_model_activity,
    on_reasoning_delta,
    on_background_review_message,
    on_message_aborted,
    on_message_interrupted,
    on_cron_deliver,
    _safe_hook,
)

# ── Public entry point ─────────────────────────────────────────────

def _apply_gateway_runner_patches(compat: Any | None = None) -> bool:
    """Apply the three critical GatewayRunner method patches.

    ``compat`` lets a caller that already built a :class:`HermesCompat` reuse it
    instead of paying for a second module resolution pass.
    """
    global _gw_runner_patched

    if _gw_runner_patched:
        return True  # Already patched (e.g. immediate path succeeded)

    if compat is None:
        compat = HermesCompat()
    GatewayRunner = compat.gateway_runner_class
    if GatewayRunner is None:
        return False  # Not available yet

    try:
        # Patch each method individually so one missing method
        # doesn't prevent the others from being patched.
        _patched_methods = []
        if hasattr(GatewayRunner, '_handle_message'):
            GatewayRunner._handle_message = _wrap_handle_message(GatewayRunner._handle_message)
            _patched_methods.append('_handle_message')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._handle_message not found, skipping patch")

        if hasattr(GatewayRunner, '_handle_message_with_agent'):
            GatewayRunner._handle_message_with_agent = _wrap_handle_message_with_agent(
                GatewayRunner._handle_message_with_agent
            )
            _patched_methods.append('_handle_message_with_agent')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._handle_message_with_agent not found, skipping patch")

        if hasattr(GatewayRunner, '_run_agent'):
            GatewayRunner._run_agent = _wrap_run_agent(GatewayRunner._run_agent)
            _patched_methods.append('_run_agent')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._run_agent not found, skipping patch")

        try:
            GatewayRunner._run_background_task = _wrap_run_background_task(
                GatewayRunner._run_background_task
            )
            _patched_methods.append('_run_background_task')
        except AttributeError:
            _logger.debug("hermes-lark-streaming: _run_background_task not found, background cards disabled")

        if not _patched_methods:
            _logger.error(
                "hermes-lark-streaming: GatewayRunner patch FAILED — "
                "no methods found. Streaming cards will NOT work."
            )
            return False

        _gw_runner_patched = True
        _logger.info(
            "hermes-lark-streaming: GatewayRunner patched methods: %s",
            ', '.join(_patched_methods),
        )
        return True
    except (ImportError, AttributeError) as e:
        _logger.error(
            "hermes-lark-streaming: GatewayRunner patch FAILED — "
            "gateway.run found but incompatible. "
            "Streaming cards will NOT work. Error: %s", e,
        )
        return False

def _patch_conversation_loop(compat: Any) -> bool:
    """Patch ``agent.conversation_loop.run_conversation`` (module-level patch)."""
    global _conversation_loop_patched
    if _conversation_loop_patched:
        return True
    if not compat.has_conversation_loop:
        return False
    try:
        compat.conversation_loop_module.run_conversation = _wrap_run_conversation(
            compat.conversation_loop_func
        )
        _conversation_loop_patched = True
        _logger.info("hermes-lark-streaming: agent.conversation_loop module patched ✓")
        return True
    except (AttributeError, TypeError) as e:
        _logger.warning(
            "hermes-lark-streaming: agent.conversation_loop found but "
            "patch failed (%s). Falling back to direct AIAgent patch.", e,
        )
        return False

def _patch_cron(compat: Any) -> bool:
    """Patch the cron scheduler's ``_deliver_result``."""
    global _cron_patched
    if _cron_patched:
        return True
    if not compat.has_cron_scheduler:
        return False
    try:
        _cron_mod = compat.cron_scheduler_module
        _cron_mod._deliver_result = _wrap_cron_deliver(_cron_mod._deliver_result)
        _cron_patched = True
        _logger.info(
            "hermes-lark-streaming: cron scheduler patched ✓ (module=%s)",
            getattr(_cron_mod, "__name__", "?"),
        )
        return True
    except (AttributeError, TypeError) as e:
        _logger.debug("hermes-lark-streaming: cron.scheduler patch failed (%s)", e)
        return False

def _patch_feishu(compat: Any) -> bool:
    """Patch the resolved ``FeishuAdapter`` class."""
    global _feishu_patched
    if _feishu_patched:
        return True
    FeishuAdapter = compat.feishu_adapter_class
    if FeishuAdapter is None:
        _logger.debug(
            "hermes-lark-streaming: FeishuAdapter not available via HermesCompat yet"
        )
        return False
    _feishu_patched = _apply_feishu_adapter_patches(FeishuAdapter, is_repatch=False)
    return _feishu_patched


# Targets the deferred thread retries, in application order.  Each entry is
# (name, apply_fn, critical) — ``critical`` decides whether a miss at the
# deadline is logged as an error or as informational.
def _patch_targets() -> tuple[tuple[str, Any, bool], ...]:
    return (
        ("gateway_runner", _apply_gateway_runner_patches, True),
        ("conversation_loop", _patch_conversation_loop, False),
        ("aiagent", _apply_direct_agent_patch, False),
        ("cron_scheduler", _patch_cron, False),
        ("feishu_adapter", _patch_feishu, True),
        # v1.6.0: hook platform_registry.create_adapter — main-chain fix for
        # hermes v0.17.0+ bundled platform deferred loading.  The FeishuAdapter
        # class resolved elsewhere may be a "替身" (source-path class A) because
        # the gateway's "真身" (hermes_plugins.feishu_platform.adapter class B)
        # is not loaded yet at apply_patches() time.  v1.4.0 used 2s+10s timer
        # repatch (赌时窗，治标); v1.5.0 replaced it with on-demand repatch
        # inside _wrap_feishu_adapter_send (chicken-and-egg 死结: wrapper only
        # installed on already-patched class, so unpatched 真身 never triggers
        # it; and clarify goes through send_clarify not send, so even a working
        # on-demand check on send cannot save clarify).  v1.6.0 hooks the single
        # public adapter-creation entry — platform_registry.create_adapter — so
        # every FeishuAdapter instance hermes ever creates (initial / reconnect
        # / multiplex) has its class patched BEFORE it is handed to callers.
        ("create_adapter_hook", _apply_create_adapter_hook, True),
    )

def _run_pending_patches(compat: Any | None = None) -> dict[str, bool]:
    """Attempt every patch target once; return ``{name: applied}``.

    Idempotent — each target either carries its own guard or a module-level
    completion flag, so calling this repeatedly never double-wraps.
    """
    if compat is None:
        compat = HermesCompat()
    results: dict[str, bool] = {}
    for name, fn, _critical in _patch_targets():
        try:
            results[name] = bool(fn(compat))
        except Exception:
            _logger.debug("hermes-lark-streaming: %s patch attempt failed", name, exc_info=True)
            results[name] = False
    return results

def _start_deferred_patch_thread(pending: list[str]) -> None:
    """Retry unfinished patches until they land or the 60s deadline passes.

    Since v1.6.2, host modules are resolved from ``sys.modules`` rather than
    force-imported (a blocking import on Hermes' plugin-discovery thread
    deadlocks the gateway), so at register() time ``gateway.run`` is typically
    still mid-import and ``GatewayRunner`` does not exist yet.  Everything that
    could not be patched synchronously lands here.

    The backoff starts at 0.1s because these patches must be installed before
    the gateway finishes booting and hands the first message (or the first
    ``create_adapter`` call) to an unpatched class.
    """
    global _deferred_thread

    with _deferred_thread_lock:
        if _deferred_thread is not None and _deferred_thread.is_alive():
            return

        def _poll() -> None:
            deadline = time.monotonic() + 60.0
            delay = 0.1
            critical = {n for n, _f, c in _patch_targets() if c}
            while time.monotonic() < deadline:
                time.sleep(delay)
                delay = min(delay * 2.0, 2.0)
                results = _run_pending_patches()
                remaining = [n for n in pending if not results.get(n, False)]
                landed = [n for n in pending if n not in remaining]
                if landed:
                    _logger.info(
                        "hermes-lark-streaming: deferred patches applied ✓ — %s",
                        ", ".join(landed),
                    )
                pending[:] = remaining
                if not pending:
                    return
            missing_critical = [n for n in pending if n in critical]
            if missing_critical:
                _logger.error(
                    "hermes-lark-streaming: patches NOT applied after 60s — %s. "
                    "Streaming cards will NOT work. Please check: 1) Hermes is "
                    "running via gateway mode, 2) Hermes version >= v0.5.0, "
                    "3) Re-run: hermes setup && hermes gateway start",
                    ", ".join(missing_critical),
                )
            else:
                _logger.info(
                    "hermes-lark-streaming: optional patches unavailable in this "
                    "Hermes build — %s", ", ".join(pending),
                )

        _deferred_thread = threading.Thread(
            target=_poll, name="hls-deferred-patch", daemon=True
        )
        _deferred_thread.start()

def apply_patches() -> None:
    """Apply all runtime monkey patches to ``GatewayRunner`` and ``AIAgent``."""
    if getattr(apply_patches, "_applied", False):
        return
    apply_patches._applied = True  # type: ignore[attr-defined]

    _logger.info("hermes-lark-streaming v%s: apply_patches() starting", __version__)

    compat = HermesCompat()
    # ``layout`` is kept for the doctor CLI's ``hermes_layout`` print and
    # for parity with the legacy ``_detect_hermes_layout()`` contract.
    layout = compat.get_layout_report()

    results = _run_pending_patches(compat)
    pending = [name for name, applied in results.items() if not applied]

    if pending:
        _logger.info(
            "hermes-lark-streaming: %s not resolvable yet — starting deferred "
            "patch poll (0.1s→2s backoff, 60s timeout)", ", ".join(pending),
        )
        _start_deferred_patch_thread(pending)

    def _mark(name: str) -> str:
        return "✓" if results.get(name) else ("pending" if name in pending else "✗")

    # ── Summary ──
    # v1.1.0: Record patch status in a structured dict for doctor command
    global _patch_status
    _patch_status = {
        "version": __version__,
        "gateway_runner": _mark("gateway_runner"),
        "conversation_loop": (
            "✓" if results.get("conversation_loop") else "n/a (direct AIAgent)"
        ),
        "aiagent_direct": "applied" if results.get("aiagent") else _mark("aiagent"),
        "cron_scheduler": "✓" if results.get("cron_scheduler") else "n/a",
        "background_task": _mark("gateway_runner"),
        "feishu_adapter": _mark("feishu_adapter"),
        "create_adapter_hook": _mark("create_adapter_hook"),
        "hermes_layout": layout,
    }
    _logger.info(
        "HLS: patch summary v%s — GatewayRunner=%s conversation_loop=%s "
        "AIAgent=%s cron=%s background=%s FeishuAdapter=%s create_adapter_hook=%s layout=%s",
        __version__,
        _patch_status["gateway_runner"],
        _patch_status["conversation_loop"],
        _patch_status["aiagent_direct"],
        _patch_status["cron_scheduler"],
        _patch_status["background_task"],
        _patch_status["feishu_adapter"],
        _patch_status["create_adapter_hook"],
        layout,
    )

def _apply_feishu_adapter_patches(FeishuAdapter, *, is_repatch: bool = False) -> bool:
    """Apply all FeishuAdapter method patches to the given class."""
    if FeishuAdapter is None:
        return False

    cls_id = id(FeishuAdapter)
    if cls_id in _patched_feishu_classes:
        if is_repatch:
            pass
        return True

    try:
        FeishuAdapter.send = _wrap_feishu_adapter_send(FeishuAdapter.send)
        try:
            FeishuAdapter.edit_message = _wrap_feishu_adapter_edit(FeishuAdapter.edit_message)
        except AttributeError:
            _logger.debug("hermes-lark-streaming: FeishuAdapter.edit_message not found, edit interception skipped")
        try:
            FeishuAdapter.add_reaction = _wrap_feishu_adapter_add_reaction(FeishuAdapter.add_reaction)
        except AttributeError:
            try:
                FeishuAdapter._add_reaction = _wrap_feishu_adapter_add_reaction(FeishuAdapter._add_reaction)
            except AttributeError:
                _logger.debug("hermes-lark-streaming: FeishuAdapter.add_reaction/_add_reaction not found, reaction interception skipped")
        try:
            FeishuAdapter.delete_reaction = _wrap_feishu_adapter_delete_reaction(FeishuAdapter.delete_reaction)
        except AttributeError:
            try:
                FeishuAdapter._remove_reaction = _wrap_feishu_adapter_delete_reaction(FeishuAdapter._remove_reaction)
            except AttributeError:
                _logger.debug("hermes-lark-streaming: FeishuAdapter.delete_reaction/_remove_reaction not found, reaction interception skipped")
        # NOTE(v0.15.4): send_image_file / send_image interceptors DELETED (2026-06-09).

        try:
            FeishuAdapter.send_clarify = _wrap_feishu_adapter_send_clarify(FeishuAdapter.send_clarify)
            _logger.info("hermes-lark-streaming: FeishuAdapter.send_clarify patched ✓ (clarify interactive card)")
        except AttributeError:
            _logger.debug("hermes-lark-streaming: FeishuAdapter.send_clarify not found, clarify card skipped")
        try:
            FeishuAdapter._handle_card_action_event = _wrap_handle_card_action_event(FeishuAdapter._handle_card_action_event)
            _logger.info("hermes-lark-streaming: FeishuAdapter._handle_card_action_event patched ✓ (card action /card suppression)")
        except AttributeError:
            _logger.debug("hermes-lark-streaming: FeishuAdapter._handle_card_action_event not found, /card suppression skipped")

        # Record this class as patched AFTER successful patch (only on success,
        # so a failed attempt can be retried later in the deferred stage).
        _patched_feishu_classes.add(cls_id)
        _logger.info(
            "hermes-lark-streaming: FeishuAdapter.send/edit/reaction/image/clarify patched ✓ "
            "(gateway message cards enabled, class_id=%s)",
            cls_id,
        )
        return True
    except AttributeError as e:
        _logger.info("hermes-lark-streaming: FeishuAdapter patch skipped (%s)", e)
        return False

def _verify_feishu_patch_identity(adapter_instance: Any) -> bool:
    """Verify that an adapter instance's class has been patched by HLS."""
    if adapter_instance is None:
        return False
    cls = type(adapter_instance)
    cls_id = id(cls)
    if cls_id in _patched_feishu_classes:
        return True
    _logger.error(
        "HLS: FeishuAdapter identity mismatch! adapter instance class id=%s "
        "not in patched classes %s. Clarify/delegate cards will fall back to "
        "text. Run /aowen doctor.",
        cls_id, sorted(_patched_feishu_classes),
    )
    return False


def _wrap_platform_registry_create_adapter(orig_create_adapter: Callable) -> Callable:
    """v1.6.0: wrap platform_registry.create_adapter so every adapter instance
    hermes creates has its class patched BEFORE it reaches callers.

    This is the main-chain fix for hermes v0.17.0+ bundled platform deferred
    loading.  See _apply_create_adapter_hook docstring for the full rationale.
    """

    def _wrapped(name, config):
        adapter = orig_create_adapter(name, config)
        if adapter is None:
            return adapter
        # Only patch feishu adapters — other platforms (irc/telegram/...) are
        # none of our business.  ``name`` is platform.value ("feishu"/"lark");
        # also sniff the class module as a belt-and-suspenders match.
        _is_feishu = False
        if isinstance(name, str) and name.lower() in ("feishu", "lark"):
            _is_feishu = True
        else:
            _cls_mod = getattr(type(adapter), "__module__", "") or ""
            if "feishu" in _cls_mod.lower():
                _is_feishu = True
        if not _is_feishu:
            return adapter
        cls = type(adapter)
        cls_id = id(cls)
        if cls_id in _patched_feishu_classes:
            return adapter
        try:
            _apply_feishu_adapter_patches(cls, is_repatch=True)
            _logger.info(
                "HLS: FeishuAdapter class patched at create_adapter hook "
                "(class_id=%s, deferred loading intercepted, name=%s)",
                cls_id, name,
            )
        except Exception as e:
            _logger.warning(
                "HLS: create_adapter hook patch failed (class_id=%s name=%s): %s",
                cls_id, name, e,
            )
        return adapter

    _wrapped._hls_create_adapter_wrapped = True  # type: ignore[attr-defined]
    return _wrapped


def _apply_create_adapter_hook(compat: Any | None = None) -> bool:
    """v1.6.0: install the platform_registry.create_adapter hook.

    Why this is the main-chain fix (not a fallback/compat shim):

    - hermes v0.17.0+ loads bundled platforms (feishu/telegram/...) via a
      *deferred loader*: the real FeishuAdapter class object is only created
      when the gateway first asks for it, which happens AFTER the plugin's
      apply_patches() runs at startup.  So apply_patches() sees only the
      source-path "替身" class A, patches it, but the gateway later builds a
      different "真身" class B object (same source, different module object →
      different class object) and uses class B instances.  Class B is never
      patched → clarify/delegate/cards fall back to hermes' plain-text
      BasePlatformAdapter defaults.

    - v1.4.0 fixed this with a 2s+10s timer that re-resolved & re-patched
      class B.  That works but bets on a time window (fragile if hermes ever
      loads slower) and is a "兜底" pattern, not a main-chain fix.

    - v1.5.0 replaced the timer with an on-demand repatch inside
      _wrap_feishu_adapter_send.  That has a chicken-and-egg deadlock: the
      on-demand check is itself a wrapper installed only on already-patched
      classes, so the unpatched 真身 never runs it; and clarify dispatches
      through send_clarify (not send), so even a working on-demand check on
      send cannot save clarify.

    - v1.6.0 hooks the single PUBLIC adapter-creation entry,
      platform_registry.create_adapter (gateway/platform_registry.py:278),
      which ALL four adapter-creation paths (initial / reconnect / multiplex
      start / multiplex reconnect) funnel through.  Every FeishuAdapter
      instance hermes ever builds has its class patched before it is returned
      to callers — no timer, no chicken-and-egg, covers send_clarify and
      every other method.  platform_registry.py had ZERO commits between
      hermes v0.17.0 and v0.19.0 (22 days, 2976 commits), so this hook point
      is extremely stable.

    Returns True if the hook was installed (or was already installed).
    """
    global _create_adapter_hooked

    # v1.6.2: via _try_import — gateway.platform_registry does not import
    # gateway.run and is only imported lazily inside host functions, so this
    # still resolves synchronously at register() time in practice.  Going
    # through _try_import means it can never block on a module lock held by
    # MainThread if that ever changes.
    _pr = _try_import("gateway.platform_registry", "platform_registry")
    if _pr is None:
        # debug, not info: the deferred loop retries this up to ~35 times.
        _logger.debug(
            "hermes-lark-streaming: platform_registry not available yet, "
            "create_adapter hook deferred (will retry)"
        )
        return False

    _current = getattr(_pr, "create_adapter", None)
    if _current is None:
        _logger.debug(
            "hermes-lark-streaming: platform_registry.create_adapter missing, "
            "create_adapter hook skipped"
        )
        return False

    if getattr(_current, "_hls_create_adapter_wrapped", False):
        _create_adapter_hooked = True
        return True  # already wrapped

    _pr.create_adapter = _wrap_platform_registry_create_adapter(_current)
    _create_adapter_hooked = True
    _logger.info(
        "hermes-lark-streaming: platform_registry.create_adapter hooked ✓ "
        "(main-chain deferred-loading fix — every FeishuAdapter instance gets "
        "its class patched at creation)"
    )
    return True

def _apply_direct_agent_patch(compat: Any | None = None) -> bool:
    """Directly patch AIAgent.run_conversation as belt-and-suspenders.

    Returns True once the patch is installed, so the deferred retry loop can
    stop chasing it.
    """
    global _aiagent_patched

    if compat is None:
        compat = HermesCompat()
    AIAgent = compat.aiagent_class
    if AIAgent is None:
        _logger.debug("hermes-lark-streaming: AIAgent.run_conversation direct patch deferred (run_agent not yet loaded)")
        return False

    try:
        _orig_method = AIAgent.run_conversation

        # Guard: skip if already patched
        if getattr(_orig_method, "_hls_direct_patched", False):
            _aiagent_patched = True
            _logger.debug("hermes-lark-streaming: AIAgent.run_conversation already directly patched, skip")
            return True

        # v1.3.4 fix (P1): inspect.signature 可能对 C 扩展/wrapped callable 抛异常
        import inspect
        try:
            _has_persist_ts = "persist_user_timestamp" in inspect.signature(_orig_method).parameters
        except (ValueError, TypeError):
            _has_persist_ts = False

        def _patched_run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            task_id=None,
            stream_callback=None,
            persist_user_message=None,
            persist_user_timestamp=None,
            **kwargs,
        ):
            # v1.3.0: inject_time removed — Hermes v0.17.0+ has built-in
            # gateway.message_timestamps.enabled for this purpose.

            _maybe_wrap_callbacks(self)
            try:
                # 用关键字参数传递，兼容有/无 persist_user_timestamp 的 Hermes 版本
                # 如果原方法不支持 persist_user_timestamp，它会被 **kwargs 吞掉
                call_kwargs = {
                    "system_message": system_message,
                    "conversation_history": conversation_history,
                    "task_id": task_id,
                    "stream_callback": stream_callback,
                    "persist_user_message": persist_user_message,
                }
                # v1.3.0 perf: cache inspect.signature result at wrap time
                # (the signature never changes at runtime — was ~10-50μs/message wasted)
                if _has_persist_ts:
                    call_kwargs["persist_user_timestamp"] = persist_user_timestamp
                call_kwargs.update(kwargs)
                return _orig_method(self, user_message, **call_kwargs)
            finally:
                pass  # v1.3.0: inject_time guard removed

        _patched_run_conversation._hls_direct_patched = True
        AIAgent.run_conversation = _patched_run_conversation
        _aiagent_patched = True
        _logger.info("hermes-lark-streaming: AIAgent.run_conversation patched directly")
        return True
    except AttributeError as e:
        _logger.debug("hermes-lark-streaming: AIAgent.run_conversation direct patch deferred (run_agent not yet loaded: %s)", e)
        return False
