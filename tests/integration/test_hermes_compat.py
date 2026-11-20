"""Hermes integration compatibility tests.

Verifies that the monkey-patching targets used by hermes-lark-streaming
still exist in the latest Hermes agent source code. These tests run
against the real Hermes source (checked out by the CI workflow).

Two verification strategies are used:

1. **Import-based** (preferred): Import the actual Hermes modules and
   verify classes, methods, and attributes exist at runtime.
2. **AST-based** (fallback): Parse the Hermes Python source files and
   inspect their abstract syntax tree.  Used when Hermes dependencies
   are unavailable and imports fail.

The Hermes source directory is read from the ``HERMES_SRC_DIR`` environment
variable (set by the CI workflow).  If not set, all tests are skipped.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────────


def _hermes_src_dir() -> Path | None:
    """Return the Hermes source directory, or None if not configured."""
    d = os.environ.get("HERMES_SRC_DIR", "").strip()
    if d and Path(d).is_dir():
        return Path(d)
    return None


def _ensure_hermes_on_path(src_dir: Path) -> None:
    """Add the Hermes source directory to sys.path (once)."""
    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _find_source_file(src_dir: Path, module_path: str) -> Path | None:
    """Find a Python source file by dotted module path (e.g. 'gateway.run').

    Searches for both ``<path>.py`` and ``<path>/__init__.py``.
    """
    # Try direct .py file
    py_file = src_dir / (module_path.replace(".", "/") + ".py")
    if py_file.is_file():
        return py_file
    # Try package __init__.py
    init_file = src_dir / module_path.replace(".", "/") / "__init__.py"
    if init_file.is_file():
        return init_file
    return None


def _parse_ast(src_dir: Path, module_path: str) -> ast.Module | None:
    """Parse a Python source file and return its AST, or None if not found."""
    src_file = _find_source_file(src_dir, module_path)
    if src_file is None:
        return None
    try:
        return ast.parse(src_file.read_text(encoding="utf-8"), filename=str(src_file))
    except SyntaxError:
        return None


def _ast_has_class(tree: ast.Module, class_name: str) -> bool:
    """Check whether an AST tree contains a class definition with the given name."""
    return any(
        isinstance(node, ast.ClassDef) and node.name == class_name
        for node in ast.walk(tree)
    )


def _ast_class_has_method(tree: ast.Module, class_name: str, method_name: str) -> bool:
    """Check whether a class in the AST has a method with the given name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == method_name:
                        return True
    return False


def _ast_module_has_function(tree: ast.Module, func_name: str) -> bool:
    """Check whether an AST tree contains a top-level function with the given name."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return True
    return False


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def hermes_src() -> Path:
    """Provide the Hermes source directory, skipping if not available."""
    d = _hermes_src_dir()
    if d is None:
        pytest.skip("HERMES_SRC_DIR not set or not a directory — skipping integration tests")
    return d


@pytest.fixture(scope="session", autouse=True)
def _setup_path(hermes_src: Path) -> None:
    """Ensure Hermes source is on sys.path for all integration tests."""
    _ensure_hermes_on_path(hermes_src)


# ── Tests: Source discovery ───────────────────────────────────────────


class TestHermesSourceDiscovery:
    """Verify that the Hermes source tree is accessible and has expected layout."""

    def test_hermes_src_dir_exists(self, hermes_src: Path) -> None:
        """The Hermes source directory should exist."""
        assert hermes_src.is_dir(), f"Hermes source directory not found: {hermes_src}"

    def test_hermes_src_has_gateway_package(self, hermes_src: Path) -> None:
        """The Hermes source should contain a 'gateway' package."""
        gw_dir = hermes_src / "gateway"
        gw_init = gw_dir / "__init__.py"
        assert gw_dir.is_dir() or gw_init.is_file(), (
            "No 'gateway' package found in Hermes source"
        )

    def test_hermes_src_has_run_agent(self, hermes_src: Path) -> None:
        """The Hermes source should contain run_agent.py (AIAgent)."""
        ra_file = hermes_src / "run_agent.py"
        assert ra_file.is_file(), "No 'run_agent.py' found in Hermes source"


# ── Tests: GatewayRunner ─────────────────────────────────────────────


class TestGatewayRunner:
    """Verify that GatewayRunner class and its patched methods still exist."""

    def test_gateway_runner_class_exists(self, hermes_src: Path) -> None:
        """GatewayRunner class should exist in gateway.run."""
        # Try import first
        try:
            from gateway.run import GatewayRunner  # noqa: F401

            return  # Import succeeded, class exists
        except (ImportError, AttributeError):
            pass
        # Fallback: AST analysis
        tree = _parse_ast(hermes_src, "gateway.run")
        assert tree is not None, "gateway/run.py not found in Hermes source"
        assert _ast_has_class(tree, "GatewayRunner"), (
            "GatewayRunner class not found in gateway/run.py (AST analysis)"
        )

    def test_gateway_runner_has_handle_message(self, hermes_src: Path) -> None:
        """GatewayRunner should have _handle_message method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_handle_message"), (
                "GatewayRunner._handle_message not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "gateway.run")
        assert tree is not None, "gateway/run.py not found"
        assert _ast_class_has_method(tree, "GatewayRunner", "_handle_message"), (
            "GatewayRunner._handle_message not found (AST analysis)"
        )

    def test_gateway_runner_has_handle_message_with_agent(self, hermes_src: Path) -> None:
        """GatewayRunner should have _handle_message_with_agent method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_handle_message_with_agent"), (
                "GatewayRunner._handle_message_with_agent not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "gateway.run")
        assert tree is not None, "gateway/run.py not found"
        assert _ast_class_has_method(tree, "GatewayRunner", "_handle_message_with_agent"), (
            "GatewayRunner._handle_message_with_agent not found (AST analysis)"
        )

    def test_gateway_runner_has_run_agent(self, hermes_src: Path) -> None:
        """GatewayRunner should have _run_agent method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_run_agent"), (
                "GatewayRunner._run_agent not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "gateway.run")
        assert tree is not None, "gateway/run.py not found"
        assert _ast_class_has_method(tree, "GatewayRunner", "_run_agent"), (
            "GatewayRunner._run_agent not found (AST analysis)"
        )


# ── Tests: AIAgent ───────────────────────────────────────────────────


class TestAIAgent:
    """Verify that AIAgent class and its callback attributes still exist."""

    def test_aiagent_class_exists(self, hermes_src: Path) -> None:
        """AIAgent class should exist in run_agent module."""
        try:
            from run_agent import AIAgent  # noqa: F401

            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "run_agent")
        assert tree is not None, "run_agent.py not found in Hermes source"
        assert _ast_has_class(tree, "AIAgent"), (
            "AIAgent class not found in run_agent.py (AST analysis)"
        )

    def test_aiagent_has_run_conversation(self, hermes_src: Path) -> None:
        """AIAgent should have run_conversation method."""
        try:
            from run_agent import AIAgent

            assert hasattr(AIAgent, "run_conversation"), (
                "AIAgent.run_conversation not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "run_agent")
        assert tree is not None, "run_agent.py not found"
        assert _ast_class_has_method(tree, "AIAgent", "run_conversation"), (
            "AIAgent.run_conversation not found (AST analysis)"
        )

    def test_aiagent_callback_attributes(self, hermes_src: Path) -> None:
        """Find the streaming callbacks in the constructor or delivery code."""
        callback_attrs = {
            "stream_delta_callback",
            "interim_assistant_callback",
            "tool_progress_callback",
            "reasoning_callback",
        }
        # Older releases set callbacks in AIAgent or the conversation loop.
        # Hermes v0.23 delegates initialization and delivery to these modules.
        modules = (
            "run_agent", "agent.conversation_loop", "agent.agent_init",
            "agent.stream_delivery", "gateway.run_turn_runner",
        )
        found_attrs: set[str] = set()
        for module in modules:
            tree = _parse_ast(hermes_src, module)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in callback_attrs:
                    found_attrs.add(node.attr)
                elif isinstance(node, ast.arg) and node.arg in callback_attrs:
                    found_attrs.add(node.arg)

        assert found_attrs & {"stream_delta_callback", "interim_assistant_callback"}, (
            f"No answer streaming callback found in {modules}"
        )
        assert "tool_progress_callback" in found_attrs, f"No tool progress callback found in {modules}"


# ── Tests: FeishuAdapter ─────────────────────────────────────────────

# v1.4.0: FeishuAdapter 实际定义位置 —— hermes v0.17.0+ 在 plugins.platforms.feishu.adapter
# （源码路径），旧版在 gateway.platforms.feishu。AST fallback 按顺序尝试所有候选路径。
_FEISHU_ADAPTER_MODULE_CANDIDATES = [
    "gateway.platforms.feishu",         # Legacy path (Hermes < v0.17)
    "plugins.platforms.feishu.adapter", # v0.17+ source path (always available)
]


def _resolve_feishu_adapter_ast(hermes_src: Path) -> tuple[ast.Module | None, str | None]:
    """Return (ast_tree, module_path) for the first candidate where FeishuAdapter source is found.

    v1.4.0: 抽取为独立 helper —— hermes v0.17.0+ 重构后 FeishuAdapter 实际定义在
    plugins.platforms.feishu.adapter（不再是 gateway.platforms.feishu）。多个 TestFeishuAdapter
    / TestMonkeyPatchTargets 测试都需要按顺序尝试候选路径，避免在 v0.17.0+ 误报缺失。
    """
    for mod_path in _FEISHU_ADAPTER_MODULE_CANDIDATES:
        tree = _parse_ast(hermes_src, mod_path)
        if tree is not None:
            return tree, mod_path
    return None, None


class TestFeishuAdapter:
    """Verify that FeishuAdapter class and its patched methods still exist."""

    def test_feishu_adapter_class_exists(self, hermes_src: Path) -> None:
        """FeishuAdapter class should exist in gateway.platforms.feishu or plugins.platforms.feishu.adapter."""
        # Try import first (legacy path only — v0.17+ source path requires lark-oapi deps)
        try:
            from gateway.platforms.feishu import FeishuAdapter  # noqa: F401

            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST analysis — v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            "FeishuAdapter source not found in any candidate path: "
            f"{_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_has_class(tree, "FeishuAdapter"), (
            f"FeishuAdapter class not found in {resolved_path} (AST analysis)"
        )

    def test_feishu_adapter_has_send(self, hermes_src: Path) -> None:
        """FeishuAdapter should have a 'send' method."""
        try:
            from gateway.platforms.feishu import FeishuAdapter

            assert hasattr(FeishuAdapter, "send"), "FeishuAdapter.send not found"
            return
        except (ImportError, AttributeError):
            pass
        # v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            f"FeishuAdapter source not found in candidates: {_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_class_has_method(tree, "FeishuAdapter", "send"), (
            f"FeishuAdapter.send not found in {resolved_path} (AST analysis)"
        )

    def test_feishu_adapter_has_edit_message(self, hermes_src: Path) -> None:
        """FeishuAdapter should have an 'edit_message' method."""
        try:
            from gateway.platforms.feishu import FeishuAdapter

            assert hasattr(FeishuAdapter, "edit_message"), (
                "FeishuAdapter.edit_message not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        # v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            f"FeishuAdapter source not found in candidates: {_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_class_has_method(tree, "FeishuAdapter", "edit_message"), (
            f"FeishuAdapter.edit_message not found in {resolved_path} (AST analysis)"
        )


# ── Tests: Monkey-patching targets ───────────────────────────────────


class TestMonkeyPatchTargets:
    """Comprehensive check of all monkey-patching targets.

    These are the specific classes, methods, and functions that
    hermes-lark-streaming patches at runtime. If any of them are
    renamed or removed, the plugin will break silently.
    """

    # (module_path, class_name, method_name)
    CLASS_METHOD_TARGETS = [
        ("gateway.run", "GatewayRunner", "_handle_message"),
        ("gateway.run", "GatewayRunner", "_handle_message_with_agent"),
        ("gateway.run", "GatewayRunner", "_run_agent"),
        ("run_agent", "AIAgent", "run_conversation"),
        ("gateway.platforms.feishu", "FeishuAdapter", "send"),
        ("gateway.platforms.feishu", "FeishuAdapter", "edit_message"),
        # v1.4.0: send_clarify / _on_card_action_trigger 从 OPTIONAL 升级到 REQUIRED。
        # 这两个方法是 clarify 卡片链路的关键 patch 点，hermes 升级若移除/改名
        # 会导致插件 patch 静默失败 → clarify 退回 BasePlatformAdapter 纯文本
        # （正是 v1.4.0 修复的根因，详见 worklog Task 2-b）。必须强制校验。
        # 注意：send_clarify 在 hermes v0.17.0 里是 FeishuAdapter 继承自
        # BasePlatformAdapter 的方法（不在 FeishuAdapter class body 内定义），
        # AST fallback 需要查 BasePlatformAdapter 继承链（见 test_required_class_method_exists）。
        ("gateway.platforms.feishu", "FeishuAdapter", "send_clarify"),
        ("gateway.platforms.feishu", "FeishuAdapter", "_on_card_action_trigger"),
    ]

    # (module_path, function_name) — module-level functions patched
    FUNCTION_TARGETS = [
        ("agent.conversation_loop", "run_conversation"),
    ]

    # Optional targets — missing ones are warnings, not failures
    OPTIONAL_CLASS_METHOD_TARGETS = [
        ("gateway.run", "GatewayRunner", "_run_background_task"),
        # Reaction methods: Hermes renamed public → private in newer versions
        # At least one of each pair should exist for the feature to work
        ("gateway.platforms.feishu", "FeishuAdapter", "add_reaction"),       # old public name
        ("gateway.platforms.feishu", "FeishuAdapter", "_add_reaction"),      # new private name
        ("gateway.platforms.feishu", "FeishuAdapter", "delete_reaction"),    # old public name
        ("gateway.platforms.feishu", "FeishuAdapter", "_remove_reaction"),   # new private name
    ]

    # v1.4.0: FeishuAdapter 实际定义位置 —— 复用模块级 _FEISHU_ADAPTER_MODULE_CANDIDATES
    # （见 TestFeishuAdapter 上方定义）。AST fallback 按顺序尝试所有候选路径。
    _FEISHU_ADAPTER_FALLBACK_PATHS = _FEISHU_ADAPTER_MODULE_CANDIDATES

    # v1.4.0: FeishuAdapter 继承自 BasePlatformAdapter，send_clarify 等方法
    # 定义在 base class 不在 FeishuAdapter class body。AST fallback 找不到时
    # 需要查继承链。BasePlatformAdapter 位于 gateway.platforms.base。
    _BASE_PLATFORM_ADAPTER_FALLBACK_PATHS = [
        "gateway.platforms.base",
    ]

    @pytest.mark.parametrize(
        "module_path, class_name, method_name",
        CLASS_METHOD_TARGETS,
        ids=[f"{c}.{m}" for _, c, m in CLASS_METHOD_TARGETS],
    )
    def test_required_class_method_exists(
        self, hermes_src: Path, module_path: str, class_name: str, method_name: str,
    ) -> None:
        """Required class method must exist (import or AST).

        v1.4.0: AST fallback 增强 —— 对 FeishuAdapter 方法支持：
          1. 多路径回退：gateway.platforms.feishu（legacy）→ plugins.platforms.feishu.adapter
             （v0.17+ 真实路径），任意一个找到 class 即算 class 存在。
          2. 继承链回退：send_clarify 等方法定义在 BasePlatformAdapter（gateway.platforms.base），
             不在 FeishuAdapter class body 内。若 FeishuAdapter class body 没有该方法，
             再尝试在 BasePlatformAdapter 里找，找到即算通过。
        这两个增强确保测试在 hermes v0.17.0+ 重构后仍能正确校验 REQUIRED patch 点。
        """
        # Try import
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name, None)
            assert cls is not None, f"Class {class_name} not found in {module_path}"
            assert hasattr(cls, method_name), (
                f"{class_name}.{method_name} not found in {module_path}"
            )
            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST — first try the declared module_path
        tree = _parse_ast(hermes_src, module_path)
        if tree is not None and _ast_class_has_method(tree, class_name, method_name):
            return  # Found in declared path's class body
        # v1.4.0: 多路径回退（仅对 FeishuAdapter —— 其他 class 直接跳过）
        if class_name == "FeishuAdapter":
            for alt_path in self._FEISHU_ADAPTER_FALLBACK_PATHS:
                if alt_path == module_path:
                    continue  # already tried above
                alt_tree = _parse_ast(hermes_src, alt_path)
                if alt_tree is not None and _ast_class_has_method(alt_tree, class_name, method_name):
                    return  # Found in alternative FeishuAdapter path
            # v1.4.0: 继承链回退 —— FeishuAdapter 继承自 BasePlatformAdapter，
            # send_clarify 等方法可能在 base class 里定义（不在 FeishuAdapter class body）
            for base_path in self._BASE_PLATFORM_ADAPTER_FALLBACK_PATHS:
                base_tree = _parse_ast(hermes_src, base_path)
                if base_tree is not None and _ast_class_has_method(
                    base_tree, "BasePlatformAdapter", method_name,
                ):
                    return  # Found in BasePlatformAdapter (inherited)
        # Nothing found — fail with clear message
        assert tree is not None, (
            f"Source for {module_path} not found (also tried fallback paths "
            f"{self._FEISHU_ADAPTER_FALLBACK_PATHS} for FeishuAdapter)"
        )
        assert _ast_class_has_method(tree, class_name, method_name), (
            f"{class_name}.{method_name} not found in {module_path} (AST analysis); "
            f"also checked FeishuAdapter fallback paths + BasePlatformAdapter inheritance chain"
        )

    @pytest.mark.parametrize(
        "module_path, func_name",
        FUNCTION_TARGETS,
        ids=[f"{m}.{f}" for m, f in FUNCTION_TARGETS],
    )
    def test_required_function_exists(
        self, hermes_src: Path, module_path: str, func_name: str,
    ) -> None:
        """Required module-level function must exist (import or AST)."""
        # Try import
        try:
            mod = __import__(module_path, fromlist=[func_name])
            assert hasattr(mod, func_name), (
                f"Function {func_name} not found in {module_path}"
            )
            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST
        tree = _parse_ast(hermes_src, module_path)
        assert tree is not None, f"Source for {module_path} not found"
        assert _ast_module_has_function(tree, func_name), (
            f"Function {func_name} not found in {module_path} (AST analysis)"
        )

    @pytest.mark.parametrize(
        "module_path, class_name, method_name",
        OPTIONAL_CLASS_METHOD_TARGETS,
        ids=[f"{c}.{m} (optional)" for _, c, m in OPTIONAL_CLASS_METHOD_TARGETS],
    )
    def test_optional_class_method_exists(
        self, hermes_src: Path, module_path: str, class_name: str, method_name: str,
    ) -> None:
        """Optional class method — warn if missing, but don't fail."""
        # Try import
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name, None)
            if cls is not None and hasattr(cls, method_name):
                return  # Found, all good
        except (ImportError, AttributeError):
            pass
        # Fallback: AST
        tree = _parse_ast(hermes_src, module_path)
        if tree is not None and _ast_class_has_method(tree, class_name, method_name):
            return  # Found via AST
        # Not found — issue a warning (not a failure)
        pytest.skip(
            f"Optional target {class_name}.{method_name} not found in {module_path} "
            f"— plugin feature will be degraded but not broken"
        )


class TestHermesTurnSignatures:
    @pytest.mark.parametrize("field", ["persist_user_timestamp", "persist_user_message"])
    def test_run_agent_accepts_turn_keyword(self, hermes_src: Path, field: str) -> None:
        """Hermes may accept turn keywords on an inner call behind a wrapper."""
        from gateway.run import GatewayRunner

        public = inspect.signature(GatewayRunner._run_agent)
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in public.parameters.values())
        inner = getattr(GatewayRunner, "_run_agent_inner", None)
        target = inspect.signature(inner) if field not in public.parameters and accepts_kwargs and inner else public
        if field == "persist_user_message" and field not in target.parameters:
            target_accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in target.parameters.values()
            )
            if not target_accepts_kwargs:
                pytest.skip("This Hermes release does not support an explicit persisted user message")
        try:
            public.bind_partial(**{field: None})
            target.bind_partial(**{field: None})
        except TypeError as error:
            pytest.fail(f"GatewayRunner._run_agent does not accept {field}: {error}")

    def test_run_conversation_still_callable(self, hermes_src: Path) -> None:
        """run_conversation 仍可调用（v0.17.0 重构后验证入口没变）."""
        try:
            from agent.conversation_loop import run_conversation  # noqa: F401
            return
        except (ImportError, AttributeError):
            pass
        from run_agent import AIAgent
        assert hasattr(AIAgent, "run_conversation"), (
            "AIAgent.run_conversation 不存在（v0.17.0 重构可能移除了入口）"
        )
