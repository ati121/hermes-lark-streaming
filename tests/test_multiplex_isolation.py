"""Multiplex isolation — two plugin copies in one process must not double-wrap.

Under ``gateway.multiplex_profiles`` Hermes loads a *directory* plugin once per
served profile (``hermes_plugins.hermes_lark_streaming`` for the first home,
``hermes_plugins.hermes_lark_streaming__home_<digest>`` for every other one), so
this package exists several times in one interpreter while the host objects it
patches exist once.  The NAS regression this file guards against: each copy
patched ``GatewayRunner`` again, three wrappers stacked, one inbound message ran
through all three (3× ``feishu inbound ids``, 3× ``HLS: session created``, 3×
``230002`` card-create failures → plain-text fallback).

The tests load the package twice under two different namespaces to reproduce the
per-profile copies, then assert the wrap count stays at one.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_copy(name: str) -> types.ModuleType:
    """Import the plugin package a second/third time under ``name``."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        str(_REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(_REPO_ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sub(copy: types.ModuleType, submodule: str):
    """Import ``<copy>.<submodule>`` (a copy-specific module object)."""
    return importlib.import_module(f"{copy.__name__}.{submodule}")

@pytest.fixture
def host_anchor():
    """Install a stand-in for ``hermes_constants`` (the real shared-state anchor).

    In production that module is already imported when the plugin loads, which is
    what lets the per-profile copies agree on process-wide state.  A live gateway
    always has it; tests must fake it.
    """
    fake = sys.modules.get("hermes_constants")
    created = fake is None
    if created:
        fake = types.ModuleType("hermes_constants")
        sys.modules["hermes_constants"] = fake
    yield fake
    if created:
        sys.modules.pop("hermes_constants", None)


@pytest.fixture
def two_copies(host_anchor):
    """Two independent copies of the plugin package (simulating two profiles)."""
    first = _load_copy("hls_copy_a")
    second = _load_copy("hls_copy_b")
    yield first, second
    for name in ("hls_copy_a", "hls_copy_b"):
        for mod_name in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            sys.modules.pop(mod_name, None)
            sys.modules.pop(mod_name, None)


def _wrap_depth(fn) -> int:
    """Number of nested wrappers on ``fn`` (1 = single wrap, 2 = stacked)."""
    depth = 0
    for _ in range(10):
        wrapped = getattr(fn, "__wrapped__", None)
        if wrapped is None:
            return depth
        depth += 1
        fn = wrapped
    return depth


class _FakeCompat:
    """Minimal HermesCompat stand-in exposing only what the patch targets need."""

    def __init__(self, gateway_runner_class) -> None:
        self.gateway_runner_class = gateway_runner_class
        self.aiagent_class = None
        self.feishu_adapter_class = None
        self.cron_scheduler_module = None
        self.has_cron_scheduler = False
        self.has_conversation_loop = False
        self.conversation_loop_module = None
        self.conversation_loop_func = None


def _make_runner_class() -> type:
    class FakeGatewayRunner:
        async def _handle_message(self, event, *args, **kwargs):
            return "ok"

        async def _handle_message_with_agent(self, event, source, *args, **kwargs):
            return "ok"

        async def _run_agent(self, *args, **kwargs):
            return {}

        async def _run_background_task(self, *args, **kwargs):
            return None

    return FakeGatewayRunner


def test_gateway_runner_is_wrapped_only_once_across_plugin_copies(two_copies) -> None:
    """Second copy must adopt the first copy's wrapper, not stack another one."""
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")
    runner = _make_runner_class()

    assert patching_a._apply_gateway_runner_patches(_FakeCompat(runner)) is True
    assert _wrap_depth(runner._handle_message) == 1

    assert patching_b._apply_gateway_runner_patches(_FakeCompat(runner)) is True
    assert _wrap_depth(runner._handle_message) == 1, "second copy stacked a wrapper"
    assert _wrap_depth(runner._run_agent) == 1
    assert _wrap_depth(runner._run_background_task) == 1

    # The marker lives on the shared class, so every copy sees the same verdict.
    assert getattr(runner, patching_a._GW_CLASS_MARK_ATTR, False)


def test_gateway_runner_patch_is_idempotent_within_one_copy(two_copies) -> None:
    first, _second = two_copies
    patching_a = _sub(first, "patching")
    runner = _make_runner_class()

    assert patching_a._apply_gateway_runner_patches(_FakeCompat(runner)) is True
    assert patching_a._apply_gateway_runner_patches(_FakeCompat(runner)) is True
    assert _wrap_depth(runner._handle_message) == 1


def test_gateway_runner_single_call_runs_the_body_once(two_copies) -> None:
    """One inbound event must execute the wrapped body exactly once."""
    first, second = two_copies
    calls: list[str] = []

    class CountingRunner:
        async def _handle_message(self, event, *args, **kwargs):
            calls.append("handled")
            return "ok"

    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")
    patching_a._apply_gateway_runner_patches(_FakeCompat(CountingRunner))
    patching_b._apply_gateway_runner_patches(_FakeCompat(CountingRunner))

    import asyncio

    asyncio.run(CountingRunner()._handle_message(object()))
    assert calls == ["handled"], f"event handled {len(calls)} times"


def test_feishu_adapter_is_wrapped_only_once_across_plugin_copies(two_copies) -> None:
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")

    class FakeFeishuAdapter:
        async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
            return "sent"

        async def send_clarify(self, *args, **kwargs):
            return "clarify"

        async def edit_message(self, *args, **kwargs):
            return None

        async def add_reaction(self, *args, **kwargs):
            return None

        async def delete_reaction(self, *args, **kwargs):
            return None

        async def _handle_card_action_event(self, *args, **kwargs):
            return None

    assert patching_a._apply_feishu_adapter_patches(FakeFeishuAdapter) is True
    first_send = FakeFeishuAdapter.send

    assert patching_b._apply_feishu_adapter_patches(FakeFeishuAdapter) is True
    assert FakeFeishuAdapter.send is first_send, "second copy stacked a wrapper"
    assert patching_b._class_marked(FakeFeishuAdapter, patching_b._FEISHU_CLASS_MARK_ATTR)

    # One call must reach the fake adapter body exactly once.
    import asyncio

    assert asyncio.run(FakeFeishuAdapter().send("c", "hi")) == "sent"


def test_module_level_wrap_marker_detects_foreign_wrapper(two_copies) -> None:
    """A wrapper installed by copy A is recognised by copy B."""
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")

    def original(a, b=1):
        return a + b

    wrapped = patching_a._mark_wrapped(lambda *a, **kw: original(*a, **kw))
    assert patching_b._already_wrapped(wrapped) is True
    assert patching_b._already_wrapped(original) is False


def test_shared_store_converges_across_copies_with_host_anchor(two_copies) -> None:
    """With a host module present, both copies resolve the same store object."""
    first, second = two_copies
    runtime_a = _sub(first, "runtime_globals")
    runtime_b = _sub(second, "runtime_globals")
    fake_host = types.ModuleType("hermes_constants")
    sys.modules["hermes_constants"] = fake_host
    try:
        store_a = runtime_a.shared_store("test.bucket")
        store_b = runtime_b.shared_store("test.bucket")
        assert store_a is not None
        assert store_a is store_b
        store_a["k"] = 1
        assert store_b["k"] == 1
        assert runtime_a.anchor_module_name() == "hermes_constants"
    finally:
        sys.modules.pop("hermes_constants", None)


def test_shared_store_falls_back_when_no_host_module(two_copies) -> None:
    """Without a host module each copy keeps its own (pre-multiplex) state."""
    first, second = two_copies
    runtime_a = _sub(first, "runtime_globals")
    runtime_b = _sub(second, "runtime_globals")
    for name in runtime_a._ANCHOR_MODULES:
        sys.modules.pop(name, None)

    local_a: dict = {}
    local_b: dict = {}
    assert runtime_a.shared_dict("test.absent", local_a) is local_a
    assert runtime_b.shared_dict("test.absent", local_b) is local_b
    assert runtime_a.shared_store("test.absent") is None


def test_controller_registry_is_shared_across_copies(two_copies, tmp_path) -> None:
    """Both copies must hand back the SAME controller for the same profile home."""
    first, second = two_copies
    controller_a = _sub(first, "controller")
    controller_b = _sub(second, "controller")
    home = tmp_path / "profile-a"
    home.mkdir()

    ctrl_a = controller_a.get_controller(home)
    ctrl_b = controller_b.get_controller(home)
    assert ctrl_a is ctrl_b, "duplicate controllers per home would double-create cards"
    assert ctrl_a._profile_home == home.resolve()

    other_home = tmp_path / "profile-b"
    other_home.mkdir()
    ctrl_other = controller_b.get_controller(other_home)
    assert ctrl_other is not ctrl_a
    assert ctrl_other._profile_home == other_home.resolve()


def test_patch_context_state_is_shared_across_copies(two_copies) -> None:
    """A wrapper installed by copy A must see turn context written by copy B.

    Only one copy wins the race to wrap each shared host object, so the objects
    its wrapper reads (started ids, gateway card registry, session contexts) must
    be process-wide.
    """
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")

    assert patching_a._started_msg_ids is patching_b._started_msg_ids
    assert patching_a._gateway_cards is patching_b._gateway_cards
    assert patching_a._session_contexts is patching_b._session_contexts
    assert patching_a._started_msg_ids_lock is patching_b._started_msg_ids_lock

    patching_a._started_msg_ids.add("msg-shared")
    assert "msg-shared" in patching_b._started_msg_ids
    patching_b._session_contexts["sess-1"] = {"message_id": "m"}
    assert patching_a._session_contexts["sess-1"]["message_id"] == "m"
    patching_a._gateway_cards["card-1"] = {"chat_id": "c"}
    assert "card-1" in patching_b._gateway_cards

    patching_a._started_msg_ids.discard("msg-shared")
    patching_b._session_contexts.pop("sess-1", None)
    patching_b._gateway_cards.pop("card-1", None)


def test_class_marker_is_not_inherited(two_copies) -> None:
    """子类不能因为继承到标记而被当成"已补丁".

    ``_class_marked`` 曾经用 ``getattr``，于是宿主变体/测试替身继承已打补丁的
    FeishuAdapter 时会被判为已处理、静默不包装（卡片降级纯文本却打印成功日志）。
    """
    first, _ = two_copies
    patching = _sub(first, "patching")

    class Base:
        pass

    class Derived(Base):
        pass

    patching._mark_class(Base, patching._FEISHU_CLASS_MARK_ATTR, "/home/a")
    assert patching._class_marked(Base, patching._FEISHU_CLASS_MARK_ATTR) is True
    assert patching._class_marked(Derived, patching._FEISHU_CLASS_MARK_ATTR) is False


def test_get_config_binds_the_current_profile_home(two_copies, tmp_path, monkeypatch) -> None:
    """``_get_config()`` 必须读当前 profile 的 config.yaml.

    未绑定的单例会把第一个 profile 的 ``gateway_cards`` 缓存给所有人：A 里关掉
    gateway_cards，B 的卡片也跟着降级成纯文本。
    """
    first, _ = two_copies
    patching = _sub(first, "patching")
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home, gateway_cards in ((home_a, True), (home_b, False)):
        home.mkdir()
        (home / "config.yaml").write_text(
            f"hermes_lark_streaming:\n  gateway_cards: {'true' if gateway_cards else 'false'}\n",
            encoding="utf-8",
        )

    config_pkg = _sub(first, "config")
    monkeypatch.setattr(config_pkg, "hermes_home", lambda: home_a)
    assert patching._get_config().gateway_cards is True

    monkeypatch.setattr(config_pkg, "hermes_home", lambda: home_b)
    assert patching._get_config().gateway_cards is False

# ── Remaining patch targets: same dedup contract as the two above ──────
#
# GatewayRunner and FeishuAdapter are covered by the tests above because they
# are the two targets that visibly stacked in production.  The other four patch
# their target in place (a module-level function, a class method, or a host
# module attribute) and carry their marker on that shared object, so they rely
# on the same "second copy adopts, never re-wraps" rule — and would be just as
# damaging if that regressed: two wrappers means one inbound message handled
# twice, which is the regression this file exists to catch.


def _make_cron_module():
    class FakeCronModule:
        def _deliver_result(self, job, content, adapters=None, loop=None, **kwargs):
            return "delivered"

    return FakeCronModule()


def _make_conversation_loop():
    class FakeConversationLoop:
        def run_conversation(self, *args, **kwargs):
            return "conversed"

    return FakeConversationLoop()


def test_conversation_loop_is_wrapped_only_once_across_copies(two_copies) -> None:
    """Module-level run_conversation must not collect one wrapper per copy."""
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")
    module = _make_conversation_loop()

    compat = _FakeCompat(None)
    compat.has_conversation_loop = True
    compat.conversation_loop_module = module
    compat.conversation_loop_func = module.run_conversation

    assert patching_a._patch_conversation_loop(compat) is True
    first_wrapper = module.run_conversation
    assert patching_a._already_wrapped(first_wrapper) is True

    assert patching_b._patch_conversation_loop(compat) is True
    assert module.run_conversation is first_wrapper, "second copy stacked a wrapper"


def test_cron_deliver_is_wrapped_only_once_across_copies(two_copies) -> None:
    """cron._deliver_result must not collect one wrapper per copy."""
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")
    module = _make_cron_module()

    compat = _FakeCompat(None)
    compat.has_cron_scheduler = True
    compat.cron_scheduler_module = module

    assert patching_a._patch_cron(compat) is True
    first_wrapper = module._deliver_result
    assert patching_a._already_wrapped(first_wrapper) is True

    assert patching_b._patch_cron(compat) is True
    assert module._deliver_result is first_wrapper, "second copy stacked a wrapper"


def test_create_adapter_hook_is_installed_only_once_across_copies(two_copies) -> None:
    """The platform_registry.create_adapter hook notches in once, too."""
    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")

    class FakePlatformRegistry:
        def create_adapter(self, name, config):
            return None

    registry = FakePlatformRegistry()
    import types as _types

    # The real host module exposes a singleton at ``platform_registry``, not a
    # module-level ``create_adapter``; ``_apply_create_adapter_hook`` reads the
    # attribute off that singleton and rebinds it there.
    fake_host = _types.ModuleType("gateway.platform_registry")
    fake_host.platform_registry = registry
    sys.modules["gateway.platform_registry"] = fake_host
    try:
        assert patching_a._apply_create_adapter_hook() is True
        installed = registry.create_adapter
        assert getattr(installed, "_hls_create_adapter_wrapped", False) is True

        assert patching_b._apply_create_adapter_hook() is True
        assert registry.create_adapter is installed, "second copy re-hooked"
    finally:
        sys.modules.pop("gateway.platform_registry", None)


def test_aiagent_direct_patch_is_applied_only_once_across_copies(two_copies) -> None:
    """AIAgent.run_conversation carries a marker so copies cannot re-wrap it."""

    def _make_agent_class():
        class FakeAIAgent:
            def run_conversation(self, user_message, *args, **kwargs):
                return "done"

        return FakeAIAgent

    first, second = two_copies
    patching_a = _sub(first, "patching")
    patching_b = _sub(second, "patching")
    agent_class = _make_agent_class()

    compat = _FakeCompat(None)
    compat.aiagent_class = agent_class

    assert patching_a._apply_direct_agent_patch(compat) is True
    installed = agent_class.run_conversation
    assert getattr(installed, "_hls_direct_patched", False) is True

    assert patching_b._apply_direct_agent_patch(compat) is True
    assert agent_class.run_conversation is installed, "second copy re-wrapped"
