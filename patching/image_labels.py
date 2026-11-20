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
# The image profile also exposes the local Zimage helper as ``zimage_gen.gen``.
# Keep this allow-list narrow so arbitrary terminal helpers are not relabelled.
_IMAGE_WRAPPER_CALLS = {"zimage_gen.gen"}
_ZIMAGE_MODEL = "z-image-turbo"
_ZIMAGE_ENDPOINT = "/v1/images/generations"
_TIMEOUT_DURATION = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[smhd])?", re.IGNORECASE)
_PYTHON_HEREDOC_RE = re.compile(
    r"^\s*(?:(?:env|exec|command|nohup)\s+)*"
    r"(?:python(?:\d+(?:\.\d+)?)?|py)(?:\.exe)?"
    r"(?:\s+[-A-Za-z0-9_=]+)*\s+<<-?\s*"
    r"(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]{0,63})(?P=quote)\s*$",
    re.IGNORECASE,
)
_GENERIC_HEREDOC_RE = re.compile(
    r"<<(?P<strip>-)?\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]{0,63})(?P=quote)"
)


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


def _calls_image_handler(
    source: str | bytes,
    *,
    allowed_calls: set[str] | None = None,
) -> bool:
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
    calls = allowed_calls or ({_IMAGE_HANDLER} | _IMAGE_WRAPPER_CALLS)

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
        if isinstance(node, ast.Call) and qualified(node.func) in calls:
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
    tokens = list(tokens)
    while tokens:
        while tokens and Path(tokens[0]).name in ("env", "exec", "command", "nohup"):
            tokens = tokens[1:]
        if not tokens or Path(tokens[0]).name != "timeout":
            break
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if option in ("-k", "--kill-after", "--signal"):
                if not tokens:
                    return None
                tokens.pop(0)
            elif option.startswith(("--kill-after=", "--signal=")) or option in (
                "--preserve-status", "--foreground"
            ):
                continue
            else:
                return None
        if not tokens or not _TIMEOUT_DURATION.fullmatch(tokens.pop(0)):
            return None
    if tokens and _PYTHON.fullmatch(Path(tokens[0]).name):
        return tokens[1:]
    return None


def _terminal_python_sources(command: str, cwd: str | None) -> list[str]:
    if len(command) > _SOURCE_LIMIT:
        return []
    directory = Path(cwd) if cwd and Path(cwd).is_absolute() else None
    reads = 0
    sources: list[str] = []
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
            sources.append(tokens[1])
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
                sources.append(source.decode(encoding))
            except (OSError, UnicodeError, SyntaxError, LookupError, ValueError):
                continue
    return sources


def _terminal_calls_image_handler(command: str, cwd: str | None) -> bool:
    return any(_calls_image_handler(source) for source in _terminal_python_sources(command, cwd))


def _terminal_calls_zimage_wrapper(command: str, cwd: str | None) -> bool:
    return any(
        _calls_image_handler(source, allowed_calls=_IMAGE_WRAPPER_CALLS)
        for source in _terminal_python_sources(command, cwd)
    )


def _python_heredoc_sources(command: str) -> list[str]:
    """Extract bounded Python heredocs without interpreting shell syntax."""
    if len(command) > _SOURCE_LIMIT:
        return []
    lines = command.splitlines()
    sources: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _PYTHON_HEREDOC_RE.fullmatch(line)
        generic = _GENERIC_HEREDOC_RE.search(line)
        if match is None and generic is None:
            index += 1
            continue
        delimiter = (match or generic)["delimiter"]
        strip_tabs = bool((match and "<<-" in line) or (generic and generic["strip"]))
        end = index + 1
        while end < len(lines):
            closing = lines[end].lstrip("\t") if strip_tabs else lines[end]
            if closing == delimiter:
                break
            end += 1
        if end >= len(lines):
            index += 1
            continue
        if match is not None:
            source = "\n".join(lines[index + 1:end])
            if len(source) <= _SOURCE_LIMIT:
                sources.append(source)
            if len(sources) >= 4:
                break
        index = end + 1
    return sources


def _calls_zimage_api(source: str | bytes) -> bool:
    """Recognize an executed curl/HTTP call to the Zimage generation API.

    The classifier deliberately understands only the small, literal shape used
    by the image profile. It never imports, evaluates, or runs the source.
    """
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(source, str) or len(source) > _SOURCE_LIMIT:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False

    bindings: dict[str, str] = {}
    assignments: dict[str, ast.AST] = {}
    payload_vars: set[str] = set()

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            prefix = qualified(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    def literal_strings(node: ast.AST, seen: set[str] | None = None) -> set[str]:
        """Return literal text reachable through simple assignments."""
        seen = seen or set()
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, ast.Name):
            if node.id in seen or node.id not in assignments:
                return set()
            return literal_strings(assignments[node.id], seen | {node.id})
        if isinstance(node, ast.JoinedStr):
            values: set[str] = set()
            for value in node.values:
                values.update(literal_strings(value, seen))
            return values
        if isinstance(node, ast.FormattedValue):
            return literal_strings(node.value, seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal_strings(node.left, seen) | literal_strings(node.right, seen)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values: set[str] = set()
            for item in node.elts:
                values.update(literal_strings(item, seen))
            return values
        if isinstance(node, ast.Dict):
            values: set[str] = set()
            for key, value in zip(node.keys, node.values):
                if key is not None:
                    values.update(literal_strings(key, seen))
                values.update(literal_strings(value, seen))
            return values
        if isinstance(node, ast.Call):
            values: set[str] = set()
            for arg in node.args:
                values.update(literal_strings(arg, seen))
            for keyword in node.keywords:
                values.update(literal_strings(keyword.value, seen))
            return values
        return set()

    def contains_zimage_payload(node: ast.AST, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if isinstance(node, ast.Name):
            if node.id in payload_vars:
                return True
            if node.id in seen or node.id not in assignments:
                return False
            return contains_zimage_payload(assignments[node.id], seen | {node.id})
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is None:
                    continue
                if "model" in literal_strings(key) and any(
                    value.strip().lower() == _ZIMAGE_MODEL for value in literal_strings(value)
                ):
                    return True
                if contains_zimage_payload(value, seen):
                    return True
            return False
        if isinstance(node, ast.Call):
            return any(contains_zimage_payload(arg, seen) for arg in node.args) or any(
                contains_zimage_payload(keyword.value, seen) for keyword in node.keywords
            )
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return any(contains_zimage_payload(item, seen) for item in node.elts)
        return False

    def assignment_names(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[str]:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [target.id for target in targets if isinstance(target, ast.Name)]

    def is_main_guard(node: ast.If) -> bool:
        test = node.test
        return isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq) and (
            (isinstance(test.left, ast.Name) and test.left.id == "__name__"
             and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
             and test.comparators[0].value == "__main__")
            or (isinstance(test.comparators[0], ast.Name) and test.comparators[0].id == "__name__"
                and isinstance(test.left, ast.Constant) and test.left.value == "__main__")
        )

    def api_call(node: ast.Call) -> bool:
        function = qualified(node.func)
        if function in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
            if not node.args:
                return False
            argv = node.args[0]
            strings = literal_strings(argv)
            lowered = {value.lower() for value in strings}
            return (
                "curl" in lowered
                and any(_ZIMAGE_ENDPOINT in value for value in strings)
                and "post" in lowered
                and any(value.lower() in {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"}
                        for value in strings)
                and contains_zimage_payload(argv)
            )
        if function in {"requests.post", "httpx.post", "httpx.AsyncClient.post"}:
            values = set()
            for arg in node.args:
                values.update(literal_strings(arg))
            for keyword in node.keywords:
                values.update(literal_strings(keyword.value))
            return any(_ZIMAGE_ENDPOINT in value for value in values) and (
                any(contains_zimage_payload(arg) for arg in node.args)
                or any(contains_zimage_payload(keyword.value) for keyword in node.keywords)
            )
        return False

    def scan(body: list[ast.stmt]) -> bool:
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bindings[alias.asname or alias.name.split(".")[0]] = alias.name
                continue
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    bindings[alias.asname or alias.name] = f"{module}.{alias.name}"
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                value = node.value
                if isinstance(node, ast.Assign):
                    for name in assignment_names(node):
                        assignments[name] = value
                        if contains_zimage_payload(value):
                            payload_vars.add(name)
                else:
                    for name in assignment_names(node):
                        assignments[name] = value
                        if contains_zimage_payload(value):
                            payload_vars.add(name)
                if isinstance(value, ast.Call) and api_call(value):
                    return True
                if any(api_call(candidate) for candidate in ast.walk(value) if isinstance(candidate, ast.Call)):
                    return True
                continue
            if isinstance(node, ast.Expr):
                if isinstance(node.value, ast.Call) and api_call(node.value):
                    return True
                if any(api_call(candidate) for candidate in ast.walk(node.value) if isinstance(candidate, ast.Call)):
                    return True
                continue
            if isinstance(node, ast.If) and is_main_guard(node):
                if scan(node.body):
                    return True
                continue
            if isinstance(node, ast.Try):
                if scan(node.body):
                    return True
                continue
            # Function/class bodies and unknown control flow are not known to run.
        return False

    try:
        return scan(tree.body)
    except RecursionError:
        return False


def _terminal_calls_zimage_api(command: str, cwd: str | None = None) -> bool:
    sources = _python_heredoc_sources(command)
    if cwd:
        sources.extend(_terminal_python_sources(command, cwd))
    return any(_calls_zimage_api(source) for source in sources)


def image_display_args(tool_name: str, args: dict[str, Any] | None, session_id: str = "") -> dict[str, Any] | None:
    """Add display-only metadata once, before the tool starts."""
    if tool_name not in ("terminal", "image_generate"):
        return args
    snapshot = dict(args or {})
    if tool_name == "terminal":
        command = snapshot.get("command")
        if not isinstance(command, str) or len(command) > _SOURCE_LIMIT:
            return args
        if _terminal_calls_zimage_api(command):
            snapshot["_hls_image_generate"] = True
            snapshot["_hls_image_detail"] = "Zimage API"
            snapshot["model"] = _ZIMAGE_MODEL
            return snapshot
        if not _python_heredoc_sources(command) and not any(
            _python_arguments(tokens) is not None for tokens in _image_command_segments(command)
        ):
            return args
        # Paths in a remote terminal need not refer to this process's files.
        from tools.terminal_tool import _get_env_config, get_session_cwd

        config = _get_env_config()
        if config.get("env_type") != "local":
            return args
        cwd = snapshot.get("workdir") or get_session_cwd(session_id) or config.get("cwd")
        if _terminal_calls_zimage_api(command, cwd):
            snapshot["_hls_image_generate"] = True
            snapshot["_hls_image_detail"] = "Zimage API"
            snapshot["model"] = _ZIMAGE_MODEL
            return snapshot
        if not any(_python_arguments(tokens) is not None for tokens in _image_command_segments(command)):
            return args
        if _terminal_calls_zimage_wrapper(command, cwd):
            snapshot["_hls_image_generate"] = True
            snapshot["_hls_image_detail"] = "Zimage API"
            snapshot["model"] = _ZIMAGE_MODEL
            return snapshot
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
