"""Labels for terminal scripts that invoke Hermes image providers internally."""

import shlex
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_lark_streaming.patching import _maybe_wrap_callbacks, _msg_ctx
from hermes_lark_streaming.patching.image_labels import (
    _calls_image_handler,
    _calls_zimage_api,
    _selected_image_model,
    _terminal_calls_image_handler,
    _terminal_calls_zimage_api,
    image_display_args,
)
from hermes_lark_streaming.state.tooluse import ToolUseTracker, _tool_display_names

_CALL = 'from tools.image_generation_tool import _handle_image_generate\nresult = _handle_image_generate({"prompt": "a cat"})\n'
_ZIMAGE_WRAPPER_CALL = (
    'from zimage_gen import gen\n'
    'gen("a cat", tag="zimage-test", size="1536x2048", steps=9)\n'
)

_ZIMAGE_API = '''import json, subprocess
model = "z-image-turbo"
prompt = open("/tmp/prompt.txt").read()
payload = json.dumps({"prompt": prompt, "model": model})
endpoint = "https://example.invalid/v1/images/generations"
subprocess.run(["curl", endpoint, "-X", "POST", "-H", "Content-Type: application/json", "-d", payload], check=True)
'''


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
    _ZIMAGE_WRAPPER_CALL,
    'import zimage_gen as zi\nzi.gen("a cat")',
    'from zimage_gen import gen as generate\ngenerate("a cat")',
])
def test_zimage_helper_calls_are_generation(source):
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
    'from zimage_gen import gen\nprint(gen)',
    'from zimage_gen import gen\ndef unused():\n    gen("a cat")',
    'from unrelated import gen\ngen("a cat")',
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


def test_custom_zimage_helper_script_is_generation(runtime, tmp_path):
    script = tmp_path / "z_run8.py"
    script.write_text(_ZIMAGE_WRAPPER_CALL, encoding="utf-8")
    command = f"cd {shlex.quote(tmp_path.as_posix())} && timeout 300 python3 z_run8.py"
    assert _terminal_calls_image_handler(command, str(tmp_path))
    snapshot = image_display_args("terminal", {"command": command})
    assert snapshot is not None
    assert snapshot["_hls_image_generate"] is True
    assert snapshot["_hls_image_detail"] == "Zimage API"
    assert snapshot["model"] == "z-image-turbo"
    assert _tool_display_names("terminal", args=snapshot) == (
        "Zimage · Generate image", "Zimage · 生成图片",
    )


def test_timeout_wrapper_without_python_stays_terminal(runtime):
    command = 'timeout 300 echo "from zimage_gen import gen; gen(\\"a cat\\")"'
    assert not _terminal_calls_image_handler(command, None)
    assert image_display_args("terminal", {"command": command}) == {"command": command}


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


def test_python_heredoc_curl_zimage_api_is_labelled_without_execution():
    command = "python3 - <<'PY'\n" + _ZIMAGE_API + "PY\n"
    assert _calls_zimage_api(_ZIMAGE_API)
    assert _terminal_calls_zimage_api(command)
    snapshot = image_display_args("terminal", {"command": command})
    assert snapshot is not None
    assert snapshot["_hls_image_generate"] is True
    assert snapshot["_hls_image_detail"] == "Zimage API"
    assert snapshot["model"] == "z-image-turbo"
    assert _tool_display_names("terminal", args=snapshot) == (
        "Zimage · Generate image", "Zimage · 生成图片",
    )


def test_prompt_heredoc_before_python_heredoc_is_labelled():
    command = (
        "cat > /tmp/prompt.txt <<'PROMPT'\nprivate prompt\nPROMPT\n"
        "python3 - <<'PY'\n" + _ZIMAGE_API + "PY\n"
    )
    assert _terminal_calls_zimage_api(command)
    snapshot = image_display_args("terminal", {"command": command})
    tracker = ToolUseTracker()
    tracker.record_start("terminal", command[:40], args=snapshot)
    tracker.record_end("terminal", output="generated")
    row = tracker.build_display_steps()[0]
    assert row["title_zh"].startswith("Zimage · 生成图片")
    assert row["emoji"] == "🎨"
    assert row["detail"] == "Zimage API"
    assert "private prompt" not in row["detail"]
    assert "Content-Type" not in row["detail"]


def test_python_heredoc_inside_prompt_body_is_opaque():
    command = (
        "cat > /tmp/prompt.txt <<'PROMPT'\n"
        "python3 - <<'PY'\n"
        + _ZIMAGE_API
        + "PY\nPROMPT\n"
    )
    assert not _terminal_calls_zimage_api(command)
    assert image_display_args("terminal", {"command": command}) == {"command": command}


def test_custom_python_script_curl_zimage_api_is_labelled(runtime, tmp_path):
    script = tmp_path / "z_run1.py"
    script.write_text(_ZIMAGE_API, encoding="utf-8")
    command = f"cd {shlex.quote(tmp_path.as_posix())} && python3 z_run1.py"
    assert _terminal_calls_zimage_api(command, str(tmp_path))
    snapshot = image_display_args("terminal", {"command": command})
    assert snapshot is not None
    assert snapshot["_hls_image_generate"] is True
    assert snapshot["_hls_image_detail"] == "Zimage API"


@pytest.mark.parametrize("source", [
    "# z-image-turbo in a comment\nprint('subprocess.run curl /v1/images/generations POST')",
    "def unused():\n    " + _ZIMAGE_API.replace("\n", "\n    "),
    "if False:\n    " + _ZIMAGE_API.replace("\n", "\n    "),
    _ZIMAGE_API.replace('"z-image-turbo"', '"flux"'),
    _ZIMAGE_API.replace("/v1/images/generations", "/v1/images/edits"),
    _ZIMAGE_API.replace('"-X", "POST"', '"-X", "GET"'),
])
def test_zimage_api_detector_rejects_mentions_and_non_generation_calls(source):
    assert not _calls_zimage_api(source)
    assert not _terminal_calls_zimage_api("python3 - <<'PY'\n" + source + "PY\n")
