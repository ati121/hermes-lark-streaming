"""Labels for ParseHub (``ph``) — the aggregator runs through ``terminal``."""

import pytest
from hermes_lark_streaming.state.tooluse import (
    ToolUseTracker,
    _tool_display_names,
    _tool_emoji,
)


class TestParseHubAlias:
    """老大 2026-11-02：image bot 调 ph 解析/下载，卡片不该再显示成 🖥️ 终端命令."""

    @pytest.mark.parametrize("command,en,zh", [
        ("ph d 'https://www.xiaohongshu.com/x' -o ./downloads", "ParseHub · Parse and download", "ParseHub · 解析并下载"),
        ("ph dl 'https://weibo.com/1/abc'", "ParseHub · Parse and download", "ParseHub · 解析并下载"),
        ("ParseHub download '分享文案'", "ParseHub · Parse and download", "ParseHub · 解析并下载"),
        ("ph p 'https://weibo.com/1/abc'", "ParseHub · Parse link", "ParseHub · 解析链接"),
        ("ph '抖音分享文案'", "ParseHub", "ParseHub"),
        ("ph ls", "ParseHub · Platforms", "ParseHub · 支持平台"),
        ("ph platforms", "ParseHub · Platforms", "ParseHub · 支持平台"),
        ("ph set proxy xhs http://127.0.0.1:7890", "ParseHub · Platform settings", "ParseHub · 平台设置"),
        ("ph --help", "ParseHub · Help", "ParseHub · 查看帮助"),
        ("ph", "ParseHub · Help", "ParseHub · 查看帮助"),
        ("ph -v", "ParseHub · Help", "ParseHub · 查看帮助"),
        ("ph -v d 'link'", "ParseHub · Parse and download", "ParseHub · 解析并下载"),
    ])
    def test_titles_follow_subcommand(self, command: str, en: str, zh: str) -> None:
        assert _tool_display_names("terminal", command) == (en, zh)

    @pytest.mark.parametrize("command", [
        "ph d 'link' -o ./downloads",
        "ph '分享文案'",
        "/opt/data/.local/bin/ph p 'link'",
        "cd /tmp && PH=/opt/data/.local/bin/ph; $PH dl 'link'",
    ])
    def test_parsehub_rows_use_the_spider_and_never_the_terminal_icon(self, command: str) -> None:
        assert _tool_emoji("terminal", command) in {"🕷️", "🗂️", "⚙️", "📖"}
        assert _tool_emoji("terminal", command) != "🖥️"

    @pytest.mark.parametrize("command", [
        "ls -la",
        "grep -rn ph notes.txt",   # 只是参数
        "echo ph d link",          # 只是回显
        "photoshop --version",     # 名字不完全匹配
        "phx d link",              # 不是 ph
    ])
    def test_other_commands_stay_terminal(self, command: str) -> None:
        assert _tool_display_names("terminal", command) == ("Terminal", "终端命令")
        assert _tool_emoji("terminal", command) == "🖥️"

    def test_alias_only_applies_to_terminal(self) -> None:
        assert _tool_display_names("read_file", "ph d link") == ("Read", "读取文件")

    def test_display_step_drops_program_from_detail(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "ph d 'https://weibo.com/1/abc' -o ./downloads")
        tracker.record_end("terminal", output="ok")
        step = tracker.build_display_steps()[0]
        assert step["title_zh"].startswith("ParseHub · 解析并下载")
        assert step["emoji"] == "🕷️"
        assert step["detail"] == "'https://weibo.com/1/abc' -o downloads"
        assert "ph " not in step["detail"]

    def test_spinner_label_follows_alias(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "ph d 'link'")
        assert tracker.last_tool_names == ("ParseHub · Parse and download", "ParseHub · 解析并下载")

    @pytest.mark.parametrize("command,detail", [
        ("ph ls", ""),
        ("ph platforms", ""),
        ("ph d 'link'", "'link'"),
        ("ph p 'link'", "'link'"),
        ("ph set proxy xhs http://127.0.0.1:7890", "proxy xhs http://127.0.0.1:7890"),
        ("ph '分享文案'", "'分享文案'"),
    ])
    def test_subcommand_verb_is_not_repeated_in_the_detail(self, command: str, detail: str) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", command)
        tracker.record_end("terminal", output="ok")
        assert tracker.build_display_steps()[0]["detail"] == detail
