"""Snapshot image-generation labels without running terminal scripts."""

from __future__ import annotations

import ast
import importlib
import io
import logging
import os
import re
import stat
import sys
import tokenize
from pathlib import Path
from typing import Any

from ..state.tooluse import _PYTHON_SCRIPT_FLAGS, _image_command_segments

_logger = logging.getLogger(__name__)
_SOURCE_LIMIT = 128 * 1024
_PYTHON = re.compile(r"(?:python(?:\d+(?:\.\d+)?)?|py)(?:\.exe)?", re.IGNORECASE)
_IMAGE_MODULE = "tools.image_generation_tool"
_IMAGE_HANDLER = _IMAGE_MODULE + "._handle_image_generate"


def _selected_image_model() -> str:
    """Read the active profile; never discover plugins or invoke generation."""
    image_tool = importlib.import_module("tools.image_generation_tool")

    model = image_tool._read_configured_image_model() or ""
    provider_name = getattr(image_tool, "_plugin_provider_name", lambda: None)()
    if provider_name:
        try:
            from agent.image_gen_registry import get_provider
        except ImportError:
            return str(model)

        provider = get_provider(provider_name)
        if provider is not None:
            # Octopus selects a resolution tier from scoped env / nested config
            # before image_gen.model. Use its loaded, read-only config helpers.
            if provider_name == "octopus":
                module = sys.modules.get(type(provider).__module__)
                resolve = getattr(module, "_resolve_model", None)
                wire_model = getattr(module, "_resolve_api_model", None)
                if callable(resolve) and callable(wire_model):
                    return str(wire_model(resolve()[0]) or "")
            model = model or provider.default_model() or ""
    return str(model)


def _calls_image_handler(source: str | bytes) -> bool:
    """Recognize direct module-level calls and main guards, not mentions.

    This is intentionally conservative: arbitrary function/control flow is
    not evaluated. Imports, strings, comments and uncalled definitions alone
    cannot turn a terminal row into generation.
    """
    if len(source) > _SOURCE_LIMIT:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    bindings: dict[str, str] = {}

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return bindings.get(node.id, "")
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    def has_call(node: ast.AST) -> bool:
        if isinstance(node, (
            ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            ast.BoolOp, ast.IfExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
        )):
            return False
        if isinstance(node, ast.Call) and qualified(node.func) == _IMAGE_HANDLER:
            return True
        return any(has_call(child) for child in ast.iter_child_nodes(node))

    def statements(body: list[ast.stmt]) -> bool:
        for node in body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name.split(".")[0]
                    bindings[key] = alias.name if alias.asname else key
            elif isinstance(node, ast.If):
                # Only this branch is known to run when python executes a file.
                if ast.dump(node.test) == ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body):
                    if statements(node.body):
                        return True
                else:
                    # Unknown branches may rebind an imported handler.
                    for child in ast.walk(node):
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                            bindings.pop(child.id, None)
            elif isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if has_call(node):
                    return True
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                        bindings.pop(child.id, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.pop(node.name, None)
            else:
                # Unsupported flow (loops, try, with, etc.) can change imports.
                bindings.clear()
        return False

    try:
        return statements(tree.body)
    except RecursionError:
        return False


def _read_script(path: Path) -> bytes:
    """Read a bounded regular file; pipes/devices must never block a callback."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _SOURCE_LIMIT:
            return b""
        return stream.read(_SOURCE_LIMIT + 1)


def _python_arguments(tokens: list[str]) -> list[str] | None:
    while tokens and tokens[0] in ("env", "exec", "command", "nohup"):
        tokens = tokens[1:]
    if tokens and _PYTHON.fullmatch(Path(tokens[0]).name):
        return tokens[1:]
    return None


def _terminal_calls_image_handler(command: str, cwd: str | None) -> bool:
    if len(command) > _SOURCE_LIMIT:
        return False
    directory = Path(cwd) if cwd and Path(cwd).is_absolute() else None
    reads = 0
    for tokens in _image_command_segments(command):
        if not tokens:
            continue
        if tokens[0] == "cd":
            target = tokens[1:] if len(tokens) > 1 and tokens[1] != "--" else tokens[2:]
            if len(target) != 1 or any(c in target[0] for c in "$`~*"):
                directory = None
            else:
                path = Path(target[0])
                directory = path if path.is_absolute() else directory / path if directory else None
            continue
        tokens = _python_arguments(tokens)
        if tokens is None:
            continue
        while tokens and tokens[0] in _PYTHON_SCRIPT_FLAGS:
            tokens = tokens[1:]
        if not tokens or any(arg in ("--help", "-h") for arg in tokens[1:]):
            continue
        if tokens[0] == "-c" and len(tokens) > 1:
            if _calls_image_handler(tokens[1]):
                return True
        elif not tokens[0].startswith("-") and tokens[0].endswith(".py"):
            if any(c in tokens[0] for c in "$`~*") or reads >= 4:
                continue
            path = Path(tokens[0])
            if not path.is_absolute():
                if directory is None:
                    continue
                path = directory / path
            reads += 1
            try:
                source = _read_script(path)
                # Validate declared encoding before AST parsing, including BOMs.
                encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
                if _calls_image_handler(source.decode(encoding)):
                    return True
            except (OSError, UnicodeError, SyntaxError, LookupError, ValueError):
                continue
    return False


def image_display_args(tool_name: str, args: dict[str, Any] | None, session_id: str = "") -> dict[str, Any] | None:
    """Add display-only metadata once, before the tool starts."""
    if tool_name not in ("terminal", "image_generate"):
        return args
    snapshot = dict(args or {})
    if tool_name == "terminal":
        command = snapshot.get("command")
        if not isinstance(command, str) or len(command) > _SOURCE_LIMIT:
            return args
        if not any(_python_arguments(tokens) is not None for tokens in _image_command_segments(command)):
            return args
        # Paths in a remote terminal need not refer to this process's files.
        from tools.terminal_tool import _get_env_config, get_session_cwd

        config = _get_env_config()
        if config.get("env_type") != "local":
            return args
        cwd = snapshot.get("workdir") or get_session_cwd(session_id) or config.get("cwd")
        if not _terminal_calls_image_handler(command, cwd):
            return args
        snapshot["_hls_image_generate"] = True
    try:
        model = snapshot.get("model") or _selected_image_model()
        if model:
            snapshot["model"] = model
    except Exception:
        # Missing/version-specific metadata should still allow a generic image
        # label, without breaking the original progress callback.
        _logger.debug("HLS: image model metadata unavailable", exc_info=True)
    return snapshot
