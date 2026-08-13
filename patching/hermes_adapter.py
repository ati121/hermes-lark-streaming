"""Hermes compatibility adapter — isolates all Hermes internal interface access."""

from __future__ import annotations
import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("hermes_lark_streaming")

_MAIN_THREAD = threading.main_thread()

# Host modules whose in-flight import makes *any* blocking import unsafe from
# a non-main thread.  These are the modules Hermes is executing on MainThread
# while our register() runs on its ``plugin-discovery`` thread.
_HOST_IMPORT_ANCHORS = ("gateway.run", "run_agent", "agent.conversation_loop")

def _module_initializing(name: str) -> bool:
    """Return True when ``name`` is in sys.modules but its body is still running.

    CPython sets ``ModuleSpec._initializing`` for the duration of a module's
    execution.  It is a private attribute, so every read is ``getattr``-guarded:
    if a future CPython drops it we degrade to the old blocking behaviour rather
    than crash.
    """
    mod = sys.modules.get(name)
    if mod is None:
        return False
    spec = getattr(mod, "__spec__", None)
    return bool(getattr(spec, "_initializing", False))

def _host_import_unsafe() -> bool:
    """Return True when a blocking import would risk the discovery deadlock."""
    # Python's per-module import lock is re-entrant for the thread that owns
    # it, so MainThread can never deadlock against itself.
    if threading.current_thread() is _MAIN_THREAD:
        return False
    return any(_module_initializing(name) for name in _HOST_IMPORT_ANCHORS)

def _try_import(module: str, attr: str | None = None) -> Any | None:
    """Resolve a host module (or one of its attributes) without ever blocking.

    Deadlock this exists to prevent (v1.6.2): Hermes' ``PluginManager``
    holds ``_discovery_lock`` while it imports this plugin on its
    ``plugin-discovery`` thread, and MainThread asks for that same lock from
    *inside* ``import gateway.run`` (``model_tools`` calls ``discover_plugins()``
    at import time).  A blocking ``from gateway.run import GatewayRunner`` on
    the discovery thread therefore waits on a module lock MainThread will not
    release until it gets the discovery lock — a hard deadlock that hangs the
    gateway before it ever opens its log file or connects a platform.

    So: read ``sys.modules`` first (never blocks, and returns partially
    initialized modules), and only fall back to a real import when no host
    import is in flight.  Returning ``None`` is always safe — every caller
    treats it as "not available yet" and the deferred patch thread retries.
    """
    mod = sys.modules.get(module)
    if mod is None:
        if _host_import_unsafe():
            _logger.debug(
                "HLS: skipping import of %s — host import in flight on another thread",
                module,
            )
            return None
        try:
            mod = importlib.import_module(module)
        except (ImportError, AttributeError):
            return None
        except Exception:
            _logger.debug("HLS: import of %s failed", module, exc_info=True)
            return None
    if attr is None:
        return mod
    return getattr(mod, attr, None)

class HermesCompat:
    """Encapsulates all Hermes internal module access."""
    
    def __init__(self):
        self._detect_version()
        self._resolve_modules()
    
    def _detect_version(self) -> None:
        """Detect Hermes version from various sources."""
        self.hermes_version: str = "unknown"
        
        # Try importlib.metadata
        try:
            from importlib.metadata import version
            self.hermes_version = version("hermes-agent")
        except Exception:
            pass
        
        # Try __version__ attribute
        if self.hermes_version == "unknown":
            try:
                import hermes_cli
                self.hermes_version = getattr(hermes_cli, "__version__", "unknown")
            except Exception:
                pass
        
        _logger.info("HLS: Hermes version detected: %s", self.hermes_version)
    
    def _resolve_modules(self) -> None:
        """Resolve all Hermes internal modules, recording what's available."""
        self.gateway_runner_class: Any | None = None
        self.aiagent_class: Any | None = None
        self.feishu_adapter_class: Any | None = None
        self.cron_scheduler_module: Any | None = None
        self.conversation_loop_module: Any | None = None
        self.conversation_loop_func: Any | None = None
        self.run_agent_module: Any | None = None
        
        # GatewayRunner — via _try_import: a blocking import here deadlocks the
        # gateway when Hermes runs register() on its plugin-discovery thread.
        self.gateway_runner_class = _try_import("gateway.run", "GatewayRunner")
        if self.gateway_runner_class is None:
            _logger.debug("HLS: GatewayRunner not available yet")
        
        # AIAgent
        self.aiagent_class = _try_import("run_agent", "AIAgent")
        if self.aiagent_class is not None:
            self.run_agent_module = sys.modules.get("run_agent")
        else:
            _logger.debug("HLS: AIAgent not available yet")
        
        # FeishuAdapter — 抽取到 _resolve_feishu_adapter()，
        # 便于 resolve_feishu_adapter_class_fresh() 复用（v1.4.0: fix deferred loading patch miss）
        self.feishu_adapter_class = self._resolve_feishu_adapter()
        
        # Cron scheduler
        for mod_name in ("cron.scheduler", "gateway.cron.scheduler"):
            mod = _try_import(mod_name)
            if mod is not None and hasattr(mod, "_deliver_result"):
                self.cron_scheduler_module = mod
                break
        
        # Conversation loop (with namespace collision workaround)
        self._resolve_conversation_loop()
    
    def _resolve_feishu_adapter(self) -> Any | None:
        """Resolve FeishuAdapter class through the gateway's namespace."""
        # 顺序很关键：真身（hermes_plugins.feishu_platform.adapter）优先，确保
        # 如果 deferred loader 已触发，我们能拿到 gateway 实际使用的 class object。
        _feishu_import_paths = [
            "hermes_plugins.feishu_platform.adapter",  # Hermes v0.17+ (gateway runtime 真身)
            "plugins.platforms.feishu.adapter",        # Source path (替身，always available)
            "gateway.platforms.feishu",                # Legacy path (Hermes < v0.17)
        ]
        for _mod_path in _feishu_import_paths:
            cls = _try_import(_mod_path, "FeishuAdapter")
            if cls is not None:
                _logger.debug("HLS: FeishuAdapter resolved via %s", _mod_path)
                return cls
        _logger.debug("HLS: FeishuAdapter not available via any import path")
        return None
    
    def resolve_feishu_adapter_class_fresh(self) -> Any | None:
        """Re-resolve FeishuAdapter class without reusing cached state."""
        return self._resolve_feishu_adapter()
    
    def _resolve_conversation_loop(self) -> None:
        """Resolve agent.conversation_loop, handling Apple Silicon namespace collision."""
        # Strategy 1: sys.modules cache
        cl_mod = sys.modules.get("agent.conversation_loop")
        if cl_mod is not None:
            func = getattr(cl_mod, "run_conversation", None)
            if func is not None:
                self.conversation_loop_module = cl_mod
                self.conversation_loop_func = func
                _logger.debug("HLS: conversation_loop resolved via sys.modules")
                return
        
        # Strategy 2: Anchor-based discovery.
        # Skipped entirely while the host is mid-import of agent.conversation_loop:
        # the sys.modules assignment below would replace the module object Hermes
        # is still executing, corrupting its import.
        if _module_initializing("agent.conversation_loop"):
            _logger.debug(
                "HLS: agent.conversation_loop still initializing — deferring resolution"
            )
            return
        
        for anchor_name in ("gateway.run", "run_agent"):
            anchor = _try_import(anchor_name)
            if anchor is None:
                continue
            anchor_file = getattr(anchor, "__file__", None)
            if not anchor_file:
                continue
            repo_root = Path(anchor_file).resolve().parent
            if anchor_name == "gateway.run":
                repo_root = repo_root.parent
            cl_file = repo_root / "agent" / "conversation_loop.py"
            if not cl_file.is_file():
                continue
            spec = importlib.util.spec_from_file_location("agent.conversation_loop", str(cl_file))
            if spec is None or spec.loader is None:
                continue
            try:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["agent.conversation_loop"] = mod
                spec.loader.exec_module(mod)
                func = getattr(mod, "run_conversation", None)
                if func is not None:
                    self.conversation_loop_module = mod
                    self.conversation_loop_func = func
                    _logger.debug("HLS: conversation_loop resolved via anchor %s", anchor_name)
                    return
            except Exception as e:
                _logger.debug("HLS: anchor-based load failed: %s", e)
        
        # Strategy 3: Standard import
        _mod = _try_import("agent.conversation_loop")
        if _mod is not None:
            _func = getattr(_mod, "run_conversation", None)
            if _func is not None:
                self.conversation_loop_module = _mod
                self.conversation_loop_func = _func
    
    @property
    def has_gateway_runner(self) -> bool:
        return self.gateway_runner_class is not None
    
    @property
    def has_aiagent(self) -> bool:
        return self.aiagent_class is not None
    
    @property
    def has_feishu_adapter(self) -> bool:
        return self.feishu_adapter_class is not None
    
    @property
    def has_cron_scheduler(self) -> bool:
        return self.cron_scheduler_module is not None
    
    @property
    def has_conversation_loop(self) -> bool:
        return self.conversation_loop_func is not None
    
    def get_layout_report(self) -> dict[str, bool]:
        """Return a dict of what's available — for doctor command and logging."""
        return {
            "has_gateway_runner": self.has_gateway_runner,
            "has_aiagent": self.has_aiagent,
            "has_feishu_adapter": self.has_feishu_adapter,
            "has_cron_scheduler": self.has_cron_scheduler,
            "has_conversation_loop": self.has_conversation_loop,
            "hermes_version": self.hermes_version,
        }
