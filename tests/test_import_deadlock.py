"""v1.6.2 regression: host imports must never block on the module import lock.

The bug this guards against hung every gateway on the machine before it opened
its log file or connected a platform:

    MainThread          holds gateway.run's module lock (it is inside
                        ``import gateway.run``, and model_tools calls
                        discover_plugins() at import time)
                        waits for PluginManager._discovery_lock

    plugin-discovery    holds PluginManager._discovery_lock (it is inside
                        discover_and_load() importing this plugin)
                        waits for gateway.run's module lock, because
                        HermesCompat._resolve_modules() did a blocking
                        ``from gateway.run import GatewayRunner``

Neither lock has a timeout, so the process never recovers.  The fix reads
``sys.modules`` first and refuses to start a real import from a non-main thread
while a host module is still initializing.
"""

from __future__ import annotations

import importlib.machinery
import sys
import threading
import types

import pytest

from hermes_lark_streaming.patching.hermes_adapter import (
    HermesCompat,
    _host_import_unsafe,
    _module_initializing,
    _try_import,
)

_FAKE_MODULES = (
    "gateway",
    "gateway.run",
    "agent",
    "agent.conversation_loop",
    "run_agent",
)


@pytest.fixture(autouse=True)
def _clean_fake_modules():
    """Remove the fake host modules before and after each test."""
    saved = {name: sys.modules.pop(name, None) for name in _FAKE_MODULES}
    yield
    for name in _FAKE_MODULES:
        sys.modules.pop(name, None)
        if saved[name] is not None:
            sys.modules[name] = saved[name]


def _make_module(name: str, *, initializing: bool, **attrs) -> types.ModuleType:
    """Build a module whose spec mimics CPython's mid-import state."""
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    mod.__spec__._initializing = initializing
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _call_on_thread(fn, timeout: float = 5.0):
    """Run ``fn`` on a worker thread; fail the test if it blocks."""
    box: dict[str, object] = {}

    def _run() -> None:
        box["result"] = fn()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), (
        "call blocked on a worker thread — this is the deadlock the fix exists "
        "to prevent"
    )
    return box.get("result")


class TestModuleInitializing:
    def test_absent_module_is_not_initializing(self):
        assert _module_initializing("gateway.run") is False

    def test_detects_mid_import_module(self):
        _make_module("gateway.run", initializing=True)
        assert _module_initializing("gateway.run") is True

    def test_finished_module_is_not_initializing(self):
        _make_module("gateway.run", initializing=False)
        assert _module_initializing("gateway.run") is False

    def test_missing_spec_degrades_to_false(self):
        mod = types.ModuleType("gateway.run")
        mod.__spec__ = None
        sys.modules["gateway.run"] = mod
        # No spec means no way to tell — fall back to the old behaviour rather
        # than crash on a future CPython that drops the private attribute.
        assert _module_initializing("gateway.run") is False


class TestHostImportUnsafe:
    def test_safe_when_no_host_import_in_flight(self):
        assert _call_on_thread(_host_import_unsafe) is False

    def test_unsafe_on_worker_thread_during_host_import(self):
        _make_module("gateway.run", initializing=True)
        assert _call_on_thread(_host_import_unsafe) is True

    def test_main_thread_is_always_safe(self):
        """Python's import lock is re-entrant for the thread that owns it."""
        _make_module("gateway.run", initializing=True)
        assert _host_import_unsafe() is False


class TestTryImport:
    def test_returns_attr_from_loaded_module(self):
        sentinel = object()
        _make_module("gateway.run", initializing=False, GatewayRunner=sentinel)
        assert _try_import("gateway.run", "GatewayRunner") is sentinel

    def test_partial_module_without_attr_returns_none(self):
        """The real startup shape: gateway.run exists, GatewayRunner does not.

        ``class GatewayRunner`` sits thousands of lines below the imports that
        trigger plugin discovery, so at discovery time the module object is
        present but the class has not been defined yet.
        """
        _make_module("gateway.run", initializing=True)
        assert _call_on_thread(lambda: _try_import("gateway.run", "GatewayRunner")) is None

    def test_partial_module_itself_is_returned(self):
        """Anchor lookups only need ``__file__``, which a partial module has."""
        mod = _make_module("gateway.run", initializing=True)
        mod.__file__ = "/opt/hermes/gateway/run.py"
        assert _call_on_thread(lambda: _try_import("gateway.run")) is mod

    def test_never_imports_from_worker_while_host_initializing(self):
        """The core assertion: no import attempt, no block, just None."""
        _make_module("gateway.run", initializing=True)
        attempted: list[str] = []

        def _fake_import(name):
            attempted.append(name)
            raise AssertionError("import attempted while host import in flight")

        import hermes_lark_streaming.patching.hermes_adapter as adapter

        original = adapter.importlib.import_module
        adapter.importlib.import_module = _fake_import
        try:
            result = _call_on_thread(lambda: _try_import("run_agent", "AIAgent"))
        finally:
            adapter.importlib.import_module = original

        assert result is None
        assert attempted == []

    def test_missing_module_returns_none(self):
        assert _try_import("hls_module_that_does_not_exist_1f4b", "Thing") is None

    def test_resolves_once_host_import_completes(self):
        """After MainThread finishes, the deferred retry finds the class."""
        _make_module("gateway.run", initializing=True)
        assert _call_on_thread(lambda: _try_import("gateway.run", "GatewayRunner")) is None

        sentinel = object()
        _make_module("gateway.run", initializing=False, GatewayRunner=sentinel)
        assert _call_on_thread(lambda: _try_import("gateway.run", "GatewayRunner")) is sentinel


class TestResolveModulesUnderDeadlockConditions:
    def test_resolve_completes_on_worker_thread(self):
        """HermesCompat() must never hang, whatever the host is mid-import of."""
        _make_module("gateway.run", initializing=True)
        _make_module("run_agent", initializing=True)
        _make_module("agent.conversation_loop", initializing=True)

        compat = _call_on_thread(HermesCompat)
        assert compat is not None
        assert compat.has_gateway_runner is False
        assert compat.has_aiagent is False

    def test_does_not_replace_initializing_conversation_loop(self):
        """Strategy 2 must not overwrite a module the host is still executing.

        ``sys.modules[...] = mod`` on a mid-import module swaps out the object
        Hermes is in the middle of building.
        """
        _make_module("agent", initializing=False)
        cl_mod = _make_module("agent.conversation_loop", initializing=True)
        anchor = _make_module("gateway.run", initializing=True)
        anchor.__file__ = "/opt/hermes/gateway/run.py"

        _call_on_thread(HermesCompat)

        assert sys.modules["agent.conversation_loop"] is cl_mod

    def test_resolves_conversation_loop_when_ready(self):
        def _run_conversation():  # pragma: no cover — identity check only
            return None

        _make_module(
            "agent.conversation_loop",
            initializing=False,
            run_conversation=_run_conversation,
        )

        compat = _call_on_thread(HermesCompat)
        assert compat.has_conversation_loop is True
        assert compat.conversation_loop_func is _run_conversation
