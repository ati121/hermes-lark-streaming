"""Literal prompt preparation must not hide or impersonate a generator call."""

import subprocess
from pathlib import Path

import pytest
from hermes_lark_streaming.state.tooluse import (
    ToolUseTracker,
    _image_command_segments,
    _tool_brand,
    _tool_display_names,
    _tool_emoji,
)

_GENERATOR = (
    'cd /workspace/scripts && python3 zimage_gen.py "$(cat /tmp/example-input.txt)" '
    '"/tmp/example-output.png" 2048x2048 2>&1 | tail -5'
)


def prepared(body="a synthetic illustration", suffix=_GENERATOR, *, quote="'", delimiter="PROMPT"):
    return f"cat > /tmp/example-input.txt <<{quote}{delimiter}{quote}\n{body}\n{delimiter}\n{suffix}"


def assert_terminal(command):
    assert _tool_display_names("terminal", args={"command": command}) == ("Terminal", "终端命令")
    assert _tool_emoji("terminal", args={"command": command}) == "🖥️"
    assert _tool_brand("terminal", args={"command": command}) is None


@pytest.mark.parametrize("body", [
    "a synthetic illustration",
    'python3 gpt_image_gen.py "example only"\nSCRIPT=/tmp/gpt_image_gen.py',
    "unmatched ' quote; && | $(ignored) `ignored`\n# comment\n```python\npass\n```",
    "cat <<'INNER'\npython3 zimage_gen.py 'example only'\nINNER",
    " PROMPT\nPROMPT # example\nPROMPT-extra\nPROMPT; python3 gpt_image_gen.py example",
])
def test_literal_prompt_body_is_opaque_and_outer_generator_owns_label(body):
    command = prepared(body)
    assert _tool_display_names("terminal", args={"command": command}) == (
        "Zimage · Generate image", "Zimage · 生成图片",
    )
    assert _tool_emoji("terminal", args={"command": command}) == "🎨"


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize(("env_name", "env_value"), [
    ("API_KEY", "fixture-api-value"),
    ("GITEE_AI_KEY", "fixture-gitee-value"),
    ("GITEE_AI_KEY", "'fixture gitee two words'"),
    ("GITEE_AI_KEY", '"fixture gitee two words"'),
    ("GITEE_AI_KEY", "'fixture gitee\nsecond line'"),
    ("GITEE_AI_KEY", '"fixture gitee\nsecond line"'),
    ("EXTRA", "'fixture two words'"),
    ("EXTRA", '"fixture two words"'),
    ("EXTRA", "'fixture\nsecond line'"),
    ("EXTRA", '"fixture\nsecond line"'),
    ("EXTRA", "'python3 zimage_gen.py fixture-prefix-only & (example)'"),
])
def test_inline_environment_prefix_and_quoted_delimiter_keep_generator_label(quote, env_name, env_value):
    env_prefix = f"{env_name}={env_value}"
    command = prepared(suffix=_GENERATOR.replace("python3", env_prefix + " python3"), quote=quote)
    tracker = ToolUseTracker()
    tracker.record_start("terminal", command[:40], args={"command": command})
    assert tracker.status_tool_names == ("Zimage · Generate image", "Zimage · 生成图片")
    assert tracker.status_tool_emoji == "🎨"
    tracker.record_end("terminal", output="synthetic result")
    step = tracker.build_display_steps()[0]
    assert step["title_zh"].startswith("Zimage · 生成图片")
    assert step["emoji"] == "🎨"
    assert " ".join(env_value.strip("'\"").split()) not in step["detail"]
    assert "fixture" not in step["detail"]
    assert not any(prefix in step["detail"] for prefix in ("API_KEY", "GITEE_AI_KEY", "EXTRA=", "cd ", "python3"))
    assert "example-input.txt" in step["detail"]
    assert "example-output.png" in step["detail"]


def test_display_detail_only_uses_the_real_outer_call():
    command = prepared('python3 zimage_gen.py "body-only example"\nSCRIPT=/tmp/ignored.py')
    tracker = ToolUseTracker()
    tracker.record_start("terminal", command[:40], args={"command": command})
    detail = tracker.build_display_steps()[0]["detail"]
    assert "body-only example" not in detail
    assert "ignored.py" not in detail
    assert "example-input.txt" in detail


@pytest.mark.parametrize("suffix", [
    "",
    "cat /tmp/example-input.txt",
    'echo "python3 zimage_gen.py example"',
    'printf "%s" "python3 zimage_gen.py example"',
    'python3 "$SCRIPT" example',
    'python3 unknown_generator.py example',
    "python3 zimage_gen.py --help",
    "python3 zimage_gen.py",
    'make_image() {\npython3 zimage_gen.py "example"\n}',
    'if false; then python3 zimage_gen.py "example"; fi',
    'false && python3 zimage_gen.py "example"',
    "python3 zimage_gen.py example & echo ignored",
    "python3 zimage_gen.py example&echo ignored",
    'sh -c \'python3 zimage_gen.py "example"\'',
    'cd "$SCRIPT_DIR" && python3 zimage_gen.py example',
    "cat > /tmp/second-input.txt <<'SECOND'\nexample\nSECOND\n" + _GENERATOR,
])
def test_prompt_preparation_without_a_simple_outer_generator_stays_terminal(suffix):
    assert_terminal(prepared('python3 zimage_gen.py "body-only"\nSCRIPT=/tmp/zimage_gen.py', suffix))


@pytest.mark.parametrize("command", [
    "cat > /tmp/example-input.txt <<PROMPT\nexample\nPROMPT\n" + _GENERATOR,
    "cat > /tmp/example-input.txt <<-'PROMPT'\nexample\nPROMPT\n" + _GENERATOR,
    "cat > /tmp/example-input.txt <<<'example'\n" + _GENERATOR,
    "cat > \"$INPUT\" <<'PROMPT'\nexample\nPROMPT\n" + _GENERATOR,
    "cat > /tmp/example-input.txt <<'PROMPT' > /tmp/extra.txt\nexample\nPROMPT\n" + _GENERATOR,
    "cat > /tmp/example-input.txt <<'PROMPT' <<'SECOND'\nexample\nPROMPT\nSECOND\n" + _GENERATOR,
    "if false; then\n" + prepared() + "\nfi",
    "make_image() {\n" + prepared() + "\n}",
    'echo "' + prepared().replace('"', '\\"') + '"',
])
def test_unknown_or_nonexecuting_heredoc_shapes_stay_terminal(command):
    assert_terminal(command)


@pytest.mark.parametrize("near_terminator", [" PROMPT", "PROMPT # example", "PROMPT-extra", "PROMPT; echo example"])
def test_unclosed_heredoc_never_treats_its_body_as_a_generator(near_terminator):
    command = f"cat > /tmp/example-input.txt <<'PROMPT'\n{near_terminator}\n{_GENERATOR}"
    assert_terminal(command)


def test_oversized_prompt_preparation_keeps_the_existing_fallback():
    assert_terminal(prepared("x" * (128 * 1024)))


def test_known_gpt_script_uses_its_existing_help_rule_after_preparation():
    command = prepared(suffix="python3 gpt_image_gen.py --help")
    assert _tool_display_names("terminal", args={"command": command}) == ("GPT · Help", "GPT · 查看帮助")
    assert _tool_emoji("terminal", args={"command": command}) == "📖"


def test_detection_never_reads_prompt_files_or_runs_shell(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("display detection must not perform file or shell operations")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert _tool_display_names("terminal", args={"command": prepared()})[1] == "Zimage · 生成图片"


def test_native_handler_tokenizer_still_rejects_heredocs():
    assert _image_command_segments(prepared()) == []
