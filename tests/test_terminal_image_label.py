"""Labels for terminal scripts that invoke Hermes image providers internally."""

import shlex
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_lark_streaming.patching import _maybe_wrap_callbacks, _msg_ctx
from hermes_lark_streaming.patching.image_labels import (
    _calls_image_handler,
    _selected_image_model,
    _terminal_calls_image_handler,
    image_display_args,
)
from hermes_lark_streaming.state.tooluse import ToolUseTracker, _tool_display_names

_CALL = 'from tools.image_generation_tool import _handle_image_generate\nresult = _handle_image_generate({"prompt": "a cat"})\n'


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    image = ModuleType("tools.image_generation_tool")
    image._read_configured_image_model = Mock(return_value="gpt-image-2.5-1k")
    image._plugin_provider_name = Mock(return_value=None)
    terminal = ModuleType("tools.terminal_tool")
    terminal._get_env_config = Mock(return_value={"env_type": "local", "cwd": str(tmp_path)})
    terminal.get_session_cwd = Mock(return_value=None)
    monkeypatch.setitem(sys.modules, image.__name__, image)
    monkeypatch.setitem(sys.modules, terminal.__name__, terminal)
    return image, terminal


@pytest.mark.parametrize("source", [
    _CALL,
    'from tools.image_generation_tool import _handle_image_generate as generate\ngenerate({})',
    'import tools.image_generation_tool as image\nimage._handle_image_generate({})',
    'from tools import image_generation_tool as image\nimage._handle_image_generate({})',
    'import tools.image_generation_tool\ntools.image_generation_tool._handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\nif __name__ == "__main__":\n    _handle_image_generate({})',
])
def test_explicit_dispatcher_calls(source):
    assert _calls_image_handler(source)


@pytest.mark.parametrize("source", [
    'from tools.image_generation_tool import _handle_image_generate',
    'print("from tools.image_generation_tool import _handle_image_generate; _handle_image_generate({})")',
    '# from tools.image_generation_tool import _handle_image_generate\n# _handle_image_generate({})',
    'from unrelated import _handle_image_generate\n_handle_image_generate({})',
    'def _handle_image_generate(args): pass\n_handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\ndef unused():\n    _handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\nif False:\n    _handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\n_handle_image_generate = print\n_handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\nfn = lambda: _handle_image_generate({})',
    'from tools.image_generation_tool import _handle_image_generate\nFalse and _handle_image_generate({})',
    'from PIL import Image\nimport shutil\nshutil.copy("image.png", "final.png")',
    'broken syntax (',
])
def test_mentions_and_unexecuted_code_are_not_generation(source):
    assert not _calls_image_handler(source)


@pytest.mark.parametrize("command", [
    'python3 {script}',
    'python3 -u -B {script} > result.json 2>&1; echo "exit=$?"; head -n 35 result.json',
    'cd {cwd} && python3 {script}',
    'cd {cwd} && python3 "custom poster.py"',
    'env python3 {script}',
])
def test_custom_script_names_paths_and_redirects(tmp_path, command):
    script = tmp_path / "custom poster.py"
    script.write_text(_CALL, encoding="utf-8")
    command = command.format(script=shlex.quote(script.as_posix()), cwd=shlex.quote(tmp_path.as_posix()))
    assert _terminal_calls_image_handler(command, None)


@pytest.mark.parametrize("command", [
    'cat {script}', 'head -n 35 {script}', 'cp {script} backup.py',
    'python3 {script} --help', 'python3 {script} -h',
    'python3 -m py_compile {script}', 'echo python3 {script}',
    'sh -c "python3 {script}"', 'python3 -c "print(\'gpt-image-2.5\')"',
])
def test_non_generation_commands_keep_terminal_label(tmp_path, command):
    script = tmp_path / "custom.py"
    script.write_text(_CALL, encoding="utf-8")
    assert not _terminal_calls_image_handler(command.format(script=script.as_posix()), str(tmp_path))


def test_missing_large_invalid_and_relative_files(tmp_path):
    assert not _terminal_calls_image_handler("python3 missing.py", str(tmp_path))
    script = tmp_path / "custom.py"
    script.write_text(_CALL, encoding="utf-8")
    assert not _terminal_calls_image_handler("python3 custom.py", None)
    assert _terminal_calls_image_handler("python3 custom.py", str(tmp_path))
    script.write_text(_CALL + "#" * (128 * 1024), encoding="utf-8")
    assert not _terminal_calls_image_handler("python3 custom.py", str(tmp_path))
    script.write_bytes(b"\xff\xfeinvalid")
    assert not _terminal_calls_image_handler("python3 custom.py", str(tmp_path))


def test_remote_backend_never_reads_host_script(runtime, monkeypatch, tmp_path):
    _, terminal = runtime
    terminal._get_env_config.return_value["env_type"] = "ssh"
    read = Mock(side_effect=AssertionError("must not read host files"))
    monkeypatch.setattr("hermes_lark_streaming.patching.image_labels._read_script", read)
    args = {"command": "python3 custom.py"}
    assert image_display_args("terminal", args) is args
    read.assert_not_called()


def test_non_python_command_does_not_load_terminal_or_model_config(runtime):
    image, terminal = runtime
    args = {"command": "cp image.png final.png"}
    assert image_display_args("terminal", args) is args
    terminal._get_env_config.assert_not_called()
    image._read_configured_image_model.assert_not_called()


def test_callback_snapshot_survives_script_deletion_and_model_changes(runtime, monkeypatch, tmp_path):
    image, terminal = runtime
    script = tmp_path / "custom.py"
    marker = tmp_path / "must-not-execute"
    script.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n' + _CALL, encoding="utf-8")
    args = {"command": f'cd {shlex.quote(tmp_path.as_posix())} && python3 custom.py > result.json 2>&1; head -n 35 result.json'}
    original = Mock()
    agent = SimpleNamespace(tool_progress_callback=original, session_id="test-image-session")
    tracker = ToolUseTracker()

    def update(**event):
        if event["status"] == "started":
            tracker.record_start(event["tool_name"], event["detail"], event["tool_args"])
        else:
            tracker.record_end(event["tool_name"])
        return True

    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.on_tool_updated", update)
    token = _msg_ctx.set({"message_id": "test-image-message"})
    try:
        _maybe_wrap_callbacks(agent)
        agent.tool_progress_callback("tool.started", "terminal", args["command"][:40], args)
        assert tracker.last_tool_names == ("GPT · Generate image", "GPT · 生成图片")
        assert tracker.last_tool_emoji == "🎨"
        assert not marker.exists()
        assert set(args) == {"command"}
        terminal.get_session_cwd.assert_called_once_with("test-image-session")
        script.unlink()
        image._read_configured_image_model.return_value = "flux"
        agent.tool_progress_callback("tool.completed", "terminal", "done")
        row = tracker.build_display_steps()[0]
        assert row["title_zh"].startswith("GPT · 生成图片")
        assert row["status"] == "success"
        assert row["name"] == "terminal"
        original.assert_not_called()
    finally:
        _msg_ctx.reset(token)


def test_inline_python_and_unknown_model(runtime):
    image, _ = runtime
    image._read_configured_image_model.return_value = None
    args = {"command": "python3 -c " + shlex.quote(_CALL)}
    snapshot = image_display_args("terminal", args)
    assert _tool_display_names("terminal", args=snapshot) == ("Generate image", "生成图片")
    assert args == {"command": "python3 -c " + shlex.quote(_CALL)}


def test_octopus_resolves_actual_model_before_top_level(runtime, monkeypatch):
    image, _ = runtime
    image._plugin_provider_name.return_value = "octopus"
    image._read_configured_image_model.return_value = "flux"
    module = ModuleType("test_octopus_provider")
    module._resolve_model = Mock(return_value=("gpt-image-2.5-4k", {}))
    module._resolve_api_model = Mock(return_value="gpt-image-2.5")
    provider_type = type("Provider", (), {"__module__": module.__name__})
    registry = ModuleType("agent.image_gen_registry")
    registry.get_provider = Mock(return_value=provider_type())
    monkeypatch.setitem(sys.modules, registry.__name__, registry)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert _selected_image_model() == "gpt-image-2.5"
    module._resolve_api_model.assert_called_once_with("gpt-image-2.5-4k")
    # Re-read on every call so a profile/config change cannot retain old labels.
    module._resolve_api_model.return_value = "gpt-image-2"
    assert _selected_image_model() == "gpt-image-2"


def test_provider_default_when_configured_model_missing(runtime, monkeypatch):
    image, _ = runtime
    image._plugin_provider_name.return_value = "other"
    image._read_configured_image_model.return_value = None
    registry = ModuleType("agent.image_gen_registry")
    registry.get_provider = Mock(return_value=SimpleNamespace(default_model=lambda: "gpt-image-1"))
    monkeypatch.setitem(sys.modules, registry.__name__, registry)
    assert _selected_image_model() == "gpt-image-1"


def test_callback_fallback_keeps_original_arguments(runtime, monkeypatch):
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.on_tool_updated", Mock(return_value=False))
    original = Mock()
    agent = SimpleNamespace(tool_progress_callback=original)
    args = {"command": "python3 -c " + shlex.quote(_CALL)}
    token = _msg_ctx.set({"message_id": "test-fallback"})
    try:
        _maybe_wrap_callbacks(agent)
        agent.tool_progress_callback("tool.started", "terminal", "preview", args)
        original.assert_called_once_with("tool.started", "terminal", "preview", args)
        assert set(args) == {"command"}
    finally:
        _msg_ctx.reset(token)
