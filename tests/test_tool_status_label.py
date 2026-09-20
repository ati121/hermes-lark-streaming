"""正在调用 XXX 工具 — spinner label, Chinese tool names, and the panel fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_lark_streaming.cardkit import (
    _LOADING_ELEMENT_ID,
    _loading_element,
    _loading_status_text,
    build_panel_header,
    _build_tool_step_title,
)
from hermes_lark_streaming.feishu import CARDKIT_SCHEMA_ERROR, FeishuAPIError
from hermes_lark_streaming.state.tooluse import (
    _DEFAULT_TOOL_EMOJI,
    _TOOL_SPECS,
    _humanize_tool_name,
    _resolve_tool_descriptor,
    _tool_display_names,
    _tool_emoji,
    ToolUseTracker,
)


class TestRealHermesToolNames:
    """Names observed in production on DXP4800 (agent.log)."""

    def test_terminal_is_the_most_common_tool(self) -> None:
        en, zh = _tool_display_names("terminal")
        assert (en, zh) == ("Terminal", "终端命令")

    def test_skill_view_and_manage_are_distinct(self) -> None:
        assert _tool_display_names("skill_view")[1] == "查看技能"
        assert _tool_display_names("skill_manage")[1] == "管理技能"

    def test_search_files_is_not_web_search(self) -> None:
        desc = _resolve_tool_descriptor("search_files")
        assert desc is not None
        assert desc["title_zh"] == "搜索文件"
        assert desc["icon"] == "doc-search_outlined"
        assert _resolve_tool_descriptor("web_search")["title_zh"] == "联网搜索"

    @pytest.mark.parametrize(
        ("name", "zh"),
        [
            ("read_file", "读取文件"),
            ("write_file", "写入文件"),
            ("patch", "修改文件"),
            ("memory", "Hermes · 内置记忆"),
            ("vision_analyze", "图像分析"),
            ("browser_exec", "浏览器执行"),
            ("delegate_task", "派发子任务"),
            ("execute_code", "执行代码"),
            ("todo", "待办清单"),
            ("clarify", "追问确认"),
            ("tool_search", "工具检索"),
        ],
    )
    def test_chinese_names(self, name: str, zh: str) -> None:
        assert _tool_display_names(name)[1] == zh
# Mirrors ``_HERMES_CORE_TOOLS`` in Hermes's ``toolsets.py`` — the shared core
# every messaging platform bundle carries (``hermes-feishu`` = core + feishu
# tools). Module level, not a class body, so the mutable default isn't a class
# attribute (ruff RUF012).
_HERMES_CORE_TOOL_NAMES: tuple[str, ...] = (
    "web_search", "web_extract", "terminal", "process_manage",
    "read_file", "write_file", "patch", "search_files",
    "vision_analyze", "image_generate",
    "skills_list", "skill_view", "skill_manage",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_get_images", "browser_vision",
    "browser_console", "browser_cdp", "browser_dialog",
    "browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
    "browser_vault_save_login", "browser_vault_enter_code",
    "browser_exec",
    "text_to_speech", "todo_list", "memory", "session_search",
    "clarify", "execute_code", "delegate_task", "cronjob_manage",
    "ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service",
    "kanban_show", "kanban_list", "kanban_complete", "kanban_block",
    "kanban_request_review", "kanban_request_changes", "kanban_heartbeat",
    "kanban_comment", "kanban_create", "kanban_link", "kanban_unblock",
    "kanban_attach", "kanban_attach_url", "kanban_attachments",
    "computer_use", "manage_connections",
)

# The five browser-vault tools the bare ``browser`` prefix alias used to
# swallow into a single "Browser" row.
_BROWSER_VAULT_TOOL_NAMES: tuple[str, ...] = (
    "browser_vault_list",
    "browser_vault_unlock",
    "browser_vault_fill",
    "browser_vault_save_login",
    "browser_vault_enter_code",
)

# Tool-name prefixes whose members must each carry a distinct emoji.
_EMOJI_FAMILY_PREFIXES: tuple[str, ...] = ("kanban_", "spotify_", "yb_")


class TestHermesCoreToolNamesHaveChineseLabels:
    """Every tool the gateway can run must have a Chinese name.

    The original table was written against older Hermes builds, so the live
    names (``todo_list``/``cronjob_manage``/``process_manage``) fell through to
    ``_humanize_tool_name`` and rendered English inside a zh_cn card. This is
    the guard that says so out loud when Hermes renames the next one.

    The list mirrors ``_HERMES_CORE_TOOLS`` in Hermes's ``toolsets.py`` — the
    shared core every messaging platform bundle carries
    (``hermes-feishu`` = core + feishu tools).
    """

    @pytest.mark.parametrize("name", _HERMES_CORE_TOOL_NAMES)
    def test_every_core_tool_has_a_chinese_name(self, name: str) -> None:
        en, zh = _tool_display_names(name)
        assert zh != en, f"{name} 没有中文名，卡片会显示英文 {en!r}"
        assert zh != name, f"{name} 退回了原始工具名"

    @pytest.mark.parametrize("name", _HERMES_CORE_TOOL_NAMES)
    def test_every_core_tool_has_an_emoji(self, name: str) -> None:
        assert _tool_emoji(name) != _DEFAULT_TOOL_EMOJI, (
            f"{name} 只有兜底 emoji，说明没配 _TOOL_EMOJI_BY_NAME"
        )
        # Also guards the handful that share a token whose fallback emoji is
        # itself _DEFAULT_TOOL_EMOJI (the six on setting-inter_outlined).
        assert _tool_emoji(name) != "🔧" or name == "setup_mcp", name


class TestBrowserVaultToolsAreDistinct:
    """Regression: the bare ``browser`` prefix alias swallowed these five.

    ``_TOOL_DESCRIPTORS`` matches aliases by prefix, so before the exact specs
    existed every ``browser_vault_*`` resolved to the legacy
    ``{"aliases": ["browser", ...], "title": "Browser"}`` entry — the card said
    "Browser" five times and never said which half of the flow ran.
    """

    NAMES = _BROWSER_VAULT_TOOL_NAMES

    def test_no_longer_collapses_to_bare_browser(self) -> None:
        for name in self.NAMES:
            en, zh = _tool_display_names(name)
            assert en != "Browser", name
            assert zh != "Browser", name

    def test_labels_are_all_different(self) -> None:
        labels = [_tool_display_names(n)[1] for n in self.NAMES]
        assert len(set(labels)) == len(labels), labels

    def test_emoji_are_all_different(self) -> None:
        marks = [_tool_emoji(n) for n in self.NAMES]
        assert len(set(marks)) == len(marks), marks

    def test_list_still_reads_as_a_vault_tool(self) -> None:
        assert _tool_display_names("browser_vault_list")[1] == "浏览器 · 凭据列表"


class TestNewToolEmojiAreUniqueWithinFamily:
    """Two rows in one family must never share a mark."""

    FAMILIES = _EMOJI_FAMILY_PREFIXES

    @pytest.mark.parametrize("family", sorted(FAMILIES))
    def test_no_repeats(self, family: str) -> None:
        names = [n for n in _TOOL_SPECS if n.startswith(family)]
        assert names, f"{family} 家族一个 spec 都没有"
        marks = [_tool_emoji(n) for n in names]
        dupes = {m for m in marks if marks.count(m) > 1}
        assert not dupes, f"{family} 家族内 emoji 重复: {sorted(dupes)}"


class TestNewToolLabelsAndEmoji:
    """Spot-check the names a card actually shows."""

    @pytest.mark.parametrize(
        ("name", "zh", "emoji"),
        [
            ("process_manage", "进程管理", "⚙️"),
            ("todo_list", "待办清单", "📋"),
            ("cronjob_manage", "定时任务", "⏰"),
            ("manage_connections", "管理连接账户", "🔌"),
            ("computer_use", "电脑操作", "🖱️"),
            ("browser_vault_unlock", "浏览器 · 解锁凭据", "🔓"),
            ("kanban_complete", "看板 · 完成任务", "✅"),
            ("kanban_attach_url", "看板 · 添加链接", "🌐"),
            ("spotify_playlists", "音乐 · 播放列表", "🎵"),
            ("discord_admin", "Discord · 服务器管理", "🛡️"),
            ("yb_send_sticker", "元宝 · 发送表情", "🎴"),
            ("desktop_project", "桌面项目", "📁"),
            ("xai_video_edit", "视频编辑", "✂️"),
            ("session_title", "会话标题", "🏷️"),
        ],
    )
    def test_label_and_mark(self, name: str, zh: str, emoji: str) -> None:
        assert _tool_display_names(name)[1] == zh
        assert _tool_emoji(name) == emoji


class TestProcessManageIsProcessesNotFlows:
    """``process_manage`` polls/kills background terminal processes.

    Its Hermes schema (``tools/process_registry.py``) is
    ``terminal(background=true)`` management — poll/wait/kill/log/write/
    submit/close/handoff. 「流程管理」 would read as workflow/approval flows,
    which it is not.
    """

    def test_named_after_processes(self) -> None:
        assert _tool_display_names("process_manage")[1] == "进程管理"

    def test_not_named_after_flows(self) -> None:
        assert "流程" not in _tool_display_names("process_manage")[1]


class TestHindsightMemoryTools:
    """Hindsight's three operations, with the memory provider in each label."""

    def test_all_three_named_after_memory(self) -> None:
        assert _tool_display_names("hindsight_retain")[1] == "Hindsight · 记忆写入"
        assert _tool_display_names("hindsight_recall")[1] == "Hindsight · 记忆回溯"
        assert _tool_display_names("hindsight_reflect")[1] == "Hindsight · 记忆推演"

    def test_all_three_present(self) -> None:
        for name in ("hindsight_retain", "hindsight_recall", "hindsight_reflect"):
            assert name in _TOOL_SPECS


class TestMcpToolNames:
    def test_mcp_name_is_not_double_spaced(self) -> None:
        got = _humanize_tool_name("mcp__grok_search_rs__web_search")
        assert got == "grok search rs · web search"
        assert "  " not in got

    def test_mcp_falls_back_to_same_text_in_both_locales(self) -> None:
        en, zh = _tool_display_names("mcp__grok_search_rs__web_search")
        assert en == zh


class TestLegacyAliasFallbackStillWorks:
    """The old alias table must keep resolving names not in the exact specs."""

    def test_bare_read_still_resolves(self) -> None:
        assert _resolve_tool_descriptor("read")["title"] == "Read"

    def test_dashed_web_search_normalizes(self) -> None:
        assert _resolve_tool_descriptor("web-search")["title"] == "Search"

    def test_unknown_returns_none(self) -> None:
        assert _resolve_tool_descriptor("nonexistent_tool_xyz") is None

    def test_unknown_name_humanized_in_both_locales(self) -> None:
        en, zh = _tool_display_names("some_future_tool")
        assert en == zh == "Some future tool"


class TestToolEmoji:
    """Every tool must resolve to an emoji — the row's shape depends on it."""

    def test_every_icon_token_is_mapped(self) -> None:
        """Guard: a new tool bringing a new icon token must not slip through.

        Emoji resolution keys off the icon token so one entry covers a whole
        family. That only holds while every token in use has a mapping — this
        test is what says so out loud when someone adds the next one.
        """
        from hermes_lark_streaming.state.tooluse import (
            _TOOL_DESCRIPTORS,
            _TOOL_EMOJI_BY_ICON,
        )

        tokens = {spec[2] for spec in _TOOL_SPECS.values()}
        tokens |= {d["icon"] for d in _TOOL_DESCRIPTORS if d.get("icon")}
        missing = tokens - set(_TOOL_EMOJI_BY_ICON)
        assert not missing, f"这些 icon token 还没配 emoji: {sorted(missing)}"

    def test_every_spec_resolves(self) -> None:
        for name in _TOOL_SPECS:
            assert _tool_emoji(name), name

    @pytest.mark.parametrize(
        ("name", "emoji"),
        [
            ("terminal", "🖥️"),
            ("read_file", "📄"),
            ("write_file", "✏️"),
            ("web_search", "🔍"),
            ("delegate_task", "🤖"),
            # by-name overrides where the shared icon token is too coarse
            ("hindsight_recall", "👁️"),   # time_outlined, shared with cron
            ("cronjob", "⏰"),
            ("image_generate", "🎨"),     # report_outlined, shared with video
            ("video_generate", "🎬"),
        ],
    )
    def test_specific_mappings(self, name: str, emoji: str) -> None:
        assert _tool_emoji(name) == emoji

    def test_unmapped_tools_fall_back(self) -> None:
        """MCP and future tools have no descriptor — they still get a mark."""
        assert _tool_emoji("mcp__grok_search_rs__web_search") == _DEFAULT_TOOL_EMOJI
        assert _tool_emoji("nonexistent_tool_xyz") == _DEFAULT_TOOL_EMOJI
        assert _tool_emoji(None) == _DEFAULT_TOOL_EMOJI

    def test_tracker_reports_emoji_alongside_names(self) -> None:
        t = ToolUseTracker()
        assert t.last_tool_emoji is None
        t.record_start("terminal")
        assert t.last_tool_emoji == "🖥️"
        t.record_end("terminal", output="ok")
        assert t.last_tool_emoji == "🖥️", "sticky, same as last_tool_names"
        t.record_start("read_file")
        assert t.last_tool_emoji == "📄"


class TestLastToolNames:
    """Sticky by design — a fast tool must not blank the label on completion."""

    def test_none_when_no_tools(self) -> None:
        assert ToolUseTracker().last_tool_names is None

    def test_reports_running_tool(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal", "ls -la")
        assert t.last_tool_names == ("Terminal", "终端命令")

    def test_persists_after_completion(self) -> None:
        """A 1s terminal call used to blink and vanish; now it stays put."""
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_end("terminal", output="ok")
        assert t.last_tool_names == ("Terminal", "终端命令")

    def test_switches_to_next_tool(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_end("terminal", output="ok")
        t.record_start("read_file")
        assert t.last_tool_names == ("Read", "读取文件")

    def test_reports_latest_of_several_running(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_start("read_file")
        assert t.last_tool_names == ("Read", "读取文件")


class TestLoadingStatusText:
    def test_blank_when_no_label(self) -> None:
        text = _loading_status_text(None)
        assert text == {"tag": "plain_text", "content": " "}

    def test_tag_never_changes(self) -> None:
        """Feishu rejects a changed tag on partial update — both forms must match."""
        assert _loading_status_text(None)["tag"] == _loading_status_text(("Terminal", "终端命令"))["tag"]

    def test_chinese_rendering(self) -> None:
        """Locks the exact row format: pad + emoji + name, and no verb prefix.

        The tool titles are already verb phrases (读取文件, 写入文件), so a
        prefix would be a third thing saying "in progress" after the spinner
        and the emoji. The two leading spaces are EN SPACE (U+2002), not
        ASCII — a leading ASCII run is the sort of thing a renderer collapses.
        """
        text = _loading_status_text(("Terminal", "终端命令"), emoji="🖥️")
        assert text["i18n_content"]["zh_cn"] == "  🖥️ 终端命令"
        assert text["i18n_content"]["en_us"] == "  🖥️ Terminal"
        assert "正在调用" not in text["i18n_content"]["zh_cn"]
        assert "Calling" not in text["i18n_content"]["en_us"]

    def test_renders_without_emoji(self) -> None:
        """An unmapped tool still renders — emoji is decoration, not structure."""
        text = _loading_status_text(("Mystery", "神秘工具"))
        assert text["i18n_content"]["zh_cn"] == "  神秘工具"

    def test_label_lands_in_the_spinner_row(self) -> None:
        el = _loading_element(("Read", "读取文件"), emoji="📄")
        assert el["element_id"] == _LOADING_ELEMENT_ID
        assert el["icon"]["tag"] == "custom_icon"  # the three dots
        assert el["text"]["i18n_content"]["zh_cn"] == "  📄 读取文件"

    def test_spinner_row_adds_no_elements(self) -> None:
        from hermes_lark_streaming.cardkit.elements import _count_tag_objects

        assert _count_tag_objects(_loading_element()) == _count_tag_objects(
            _loading_element(("Read", "读取文件")),
        )


class TestLabelReachedFromRealFlush:
    """Regression: the label call must survive the flush's early returns.

    The first round of unit tests invoked _sync_loading_label directly, so they
    passed while the real flush never reached it — on Feishu the spinner stayed
    blank. These drive _do_unified_flush instead.
    """

    def _session_and_ctrl(self):
        from hermes_lark_streaming.controller.linear_mixin import UnifiedControllerMixin
        from hermes_lark_streaming.state.linear import UnifiedLinearState
        from hermes_lark_streaming.state.session import CardSession
        from hermes_lark_streaming.state.phase import CardPhase

        loop = asyncio.new_event_loop()
        session = CardSession("om_test123456", "oc_test123456", loop)
        session.card_id = "card_777"
        session.state = CardPhase.STREAMING
        session.linear = True
        session.interactive_mode = False
        session.unified_state = UnifiedLinearState()
        session.existing_elements = {_LOADING_ELEMENT_ID}
        session.flush.set_card_message_ready(True)

        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        client.cardkit_stream_element = AsyncMock()

        cfg = MagicMock()
        cfg.show_reasoning = False
        cfg.streaming_panel_expanded = False
        cfg.max_tool_steps = 20
        cfg.max_reasoning_rounds = 20

        ctrl = UnifiedControllerMixin()
        ctrl._client = client
        ctrl._cfg = cfg
        return session, ctrl, client, loop

    def _label_updates(self, client) -> list[dict]:
        found = []
        for call in client.cardkit_batch_update.await_args_list:
            for action in call.args[1]:
                if (
                    action.get("action") == "partial_update_element"
                    and action["params"].get("element_id") == _LOADING_ELEMENT_ID
                ):
                    found.append(action["params"]["partial_element"])
        return found

    def test_label_pushed_on_first_tool_event(self) -> None:
        """This is the case that shipped broken: panel gets built, label didn't."""
        session, ctrl, client, loop = self._session_and_ctrl()
        try:
            session.tool_use.record_start("terminal", "curl wttr.in")
            session.unified_state.on_tool_event(is_new_tool=True)
            loop.run_until_complete(ctrl._do_unified_flush(session))
        finally:
            loop.close()

        updates = self._label_updates(client)
        assert updates, "spinner label was never sent during a real flush"
        assert "终端命令" in updates[-1]["text"]["i18n_content"]["zh_cn"]

    def test_label_stays_after_a_fast_tool_finishes(self) -> None:
        """The 1s terminal call that blinked and vanished on Feishu."""
        session, ctrl, client, loop = self._session_and_ctrl()
        try:
            session.tool_use.record_start("terminal")
            session.unified_state.on_tool_event(is_new_tool=True)
            loop.run_until_complete(ctrl._do_unified_flush(session))
            session.tool_use.record_end("terminal", output="ok")
            session.unified_state.on_tool_event(is_new_tool=False)
            loop.run_until_complete(ctrl._do_unified_flush(session))
        finally:
            loop.close()

        updates = self._label_updates(client)
        assert updates, "label was never sent"
        assert "终端命令" in updates[-1]["text"]["i18n_content"]["zh_cn"]
        blanks = [u for u in updates if u["text"].get("content") == " "]
        assert not blanks, "label must not blank out when a tool completes"

    def test_label_rotates_to_the_next_tool(self) -> None:
        session, ctrl, client, loop = self._session_and_ctrl()
        try:
            session.tool_use.record_start("terminal")
            session.unified_state.on_tool_event(is_new_tool=True)
            loop.run_until_complete(ctrl._do_unified_flush(session))
            session.tool_use.record_end("terminal", output="ok")
            session.tool_use.record_start("read_file")
            session.unified_state.on_tool_event(is_new_tool=True)
            loop.run_until_complete(ctrl._do_unified_flush(session))
        finally:
            loop.close()

        zh = [u["text"]["i18n_content"]["zh_cn"] for u in self._label_updates(client)]
        assert "终端命令" in zh[0]
        assert "读取文件" in zh[-1]

    def test_label_survives_answer_only_flush(self) -> None:
        session, ctrl, client, loop = self._session_and_ctrl()
        try:
            session.tool_use.record_start("read_file", "SOUL.md")
            session.unified_state.on_tool_event(is_new_tool=True)
            session.unified_state.on_answer_delta("正在查看")
            loop.run_until_complete(ctrl._do_unified_flush(session))
        finally:
            loop.close()

        updates = self._label_updates(client)
        assert updates, "label lost when an answer delta shares the flush"
        assert "读取文件" in updates[-1]["text"]["i18n_content"]["zh_cn"]


class TestFooterStatusIcons:
    """Leading dot on the footer status: 🟢 done / 🛑 /stop / 🔴 error."""

    def _footer(self, **kwargs) -> tuple[str, str]:
        from hermes_lark_streaming.cardkit.elements import _build_footer_elements

        el = _build_footer_elements({"duration": 12.5}, **kwargs)[1]
        return el["content"], el["i18n_content"]["zh_cn"]

    def test_completed_is_green(self) -> None:
        en, zh = self._footer()
        assert zh.startswith("🟢 已完成")
        assert en.startswith("🟢 Completed")

    def test_stopped_is_stop_sign(self) -> None:
        en, zh = self._footer(is_aborted=True)
        assert zh.startswith("🛑 已停止")
        assert en.startswith("🛑 Stopped")

    def test_error_is_red(self) -> None:
        en, zh = self._footer(is_error=True)
        assert "🔴 出错" in zh
        assert "🔴 Error" in en
        assert "color='red'" in en  # red font kept on top of the dot

    def test_icons_are_distinct(self) -> None:
        icons = {self._footer()[1][0], self._footer(is_aborted=True)[1][0], self._footer(is_error=True)[1][:2]}
        assert len(icons) == 3


class TestPanelHeaderFallback:
    def test_no_active_tool_leaves_title_unchanged(self) -> None:
        header = build_panel_header(reasoning_rounds=[], tool_steps=[{"name": "terminal"}])
        assert "正在调用" not in header["title"]["i18n_content"]["zh_cn"]

    def test_active_tool_appended_to_title(self) -> None:
        header = build_panel_header(
            reasoning_rounds=[],
            tool_steps=[{"name": "terminal"}],
            active_tool=("Terminal", "终端命令"),
        )
        assert header["title"]["i18n_content"]["zh_cn"].endswith("正在调用 终端命令")


def _make_session(**kwargs):
    session = MagicMock()
    session.interactive_mode = False
    session.card_id = "card_abc123456789"
    session.existing_elements = {_LOADING_ELEMENT_ID}
    session._streaming_closed = False
    session._loading_label = None
    session._loading_label_supported = True
    session._loading_status_key = None
    session.sequence = 1
    session.text_sizes = {}
    session.tool_use = ToolUseTracker()
    session.unified_state = MagicMock()
    session.unified_state.panel_visible = True
    for k, v in kwargs.items():
        setattr(session, k, v)
    return session


class TestSyncLoadingLabel:
    """The label update is sent alone so a rejection can't poison the panel batch."""

    def _ctrl(self, client):
        from hermes_lark_streaming.controller.linear_mixin import UnifiedControllerMixin

        ctrl = UnifiedControllerMixin()
        ctrl._client = client
        return ctrl

    def test_pushes_running_tool(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session()
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))

        client.cardkit_batch_update.assert_awaited_once()
        actions = client.cardkit_batch_update.await_args.args[1]
        assert len(actions) == 1, "label must travel alone, not batched with panel actions"
        assert actions[0]["action"] == "partial_update_element"
        assert actions[0]["params"]["element_id"] == _LOADING_ELEMENT_ID
        partial = actions[0]["params"]["partial_element"]
        assert "tag" not in partial, "partial_update_element 不应带 tag"
        assert "终端命令" in partial["text"]["i18n_content"]["zh_cn"]
        assert session._loading_label == ("Terminal", "终端命令")

    def test_skips_when_unchanged(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(_loading_label=("Terminal", "终端命令"))
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))
        client.cardkit_batch_update.assert_not_awaited()

    def test_no_update_when_tool_finishes(self) -> None:
        """Completion is not a change — the label stays until the next tool."""
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(_loading_label=("Terminal", "终端命令"))
        session.tool_use.record_start("terminal")
        session.tool_use.record_end("terminal", output="ok")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))

        client.cardkit_batch_update.assert_not_awaited()
        assert session._loading_label == ("Terminal", "终端命令")

    def test_updates_when_next_tool_starts(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(_loading_label=("Terminal", "终端命令"))
        session.tool_use.record_start("terminal")
        session.tool_use.record_end("terminal", output="ok")
        session.tool_use.record_start("read_file")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))

        partial = client.cardkit_batch_update.await_args.args[1][0]["params"]["partial_element"]
        assert "读取文件" in partial["text"]["i18n_content"]["zh_cn"]

    def test_skips_after_loading_element_deleted(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(existing_elements=set())
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))
        client.cardkit_batch_update.assert_not_awaited()

    def test_skips_in_interactive_mode(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(interactive_mode=True)
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))
        client.cardkit_batch_update.assert_not_awaited()

    def test_schema_error_disables_and_switches_to_header(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock(
            side_effect=FeishuAPIError("unknown property text", CARDKIT_SCHEMA_ERROR),
        )
        session = _make_session()
        session.tool_use.record_start("terminal")
        ctrl = self._ctrl(client)

        asyncio.run(ctrl._sync_loading_label(session))

        assert session._loading_label_supported is False
        assert session.unified_state.panel_dirty is True
        assert ctrl._active_tool_for_header(session) == ("Terminal", "终端命令")

        # And it stops retrying on later flushes.
        client.cardkit_batch_update.reset_mock()
        asyncio.run(ctrl._sync_loading_label(session))
        client.cardkit_batch_update.assert_not_awaited()

    def test_header_stays_empty_while_label_works(self) -> None:
        session = _make_session()
        session.tool_use.record_start("terminal")
        assert self._ctrl(MagicMock())._active_tool_for_header(session) is None

    def test_transient_error_keeps_feature_enabled(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock(
            side_effect=FeishuAPIError("rate limited", 99991400),
        )
        session = _make_session()
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))

        assert session._loading_label_supported is True
        assert session._loading_label is None  # not recorded, so it retries


class TestToolStepTitleRendersEmoji:
    """The panel step row must show a coloured emoji, not a grey icon token.

    Feishu's ``standard_icon`` tokens are monochrome line art, so a div built
    from one renders grey whatever token is picked — that is why five
    ``browser_vault_*`` rows all looked like the same grey padlock. The emoji
    now rides in the text instead, and sits *outside* the bold run because
    Feishu drops a bold span that mixes emoji into it.
    """

    def _title(self, **overrides):
        step = {
            "name": "process_manage",
            "title": "Manage processes",
            "title_zh": "进程管理",
            "status": "running",
            "emoji": "⚙️",
        }
        step.update(overrides)
        return _build_tool_step_title(step, text_sizes=None)

    def test_no_standard_icon_slot(self) -> None:
        el = self._title()
        assert "icon" not in el, "灰白线稿方块应已移除"

    def test_emoji_leads_the_text(self) -> None:
        el = self._title()
        assert el["text"]["content"].startswith("⚙️ ")

    def test_emoji_sits_outside_the_bold_run(self) -> None:
        el = self._title()
        content = el["text"]["content"]
        assert "**⚙️" not in content, "emoji 不能在粗体内，飞书会丢样式"
        # The mark leads, then the colour span opens, and only inside it does
        # the bold run start.
        assert content.startswith("⚙️ <font")
        assert ">**" in content
    def test_both_locales_carry_the_emoji(self) -> None:
        el = self._title()
        i18n = el["text"]["i18n_content"]
        assert i18n["zh_cn"].startswith("⚙️ ")
        assert i18n["en_us"].startswith("⚙️ ")
        assert "**进程管理**" in i18n["zh_cn"]
        assert "**Manage processes**" in i18n["en_us"]

    def test_missing_emoji_still_renders_a_title(self) -> None:
        """Older callers pass no emoji — the row must not grow a stray space."""
        el = self._title(emoji="")
        assert el["text"]["content"].startswith("<font")

    def test_status_colour_is_preserved(self) -> None:
        running = self._title(status="running")
        success = self._title(status="success")
        assert running["text"]["content"] != success["text"]["content"]
        assert "orange-300" in running["text"]["content"]


class TestTerminalProgramAliases:
    """老大 2026-09-21：自装的 CLI 经 terminal 跑，按程序本身显示（🔍 smart-search）而不是 🖥️ 终端命令."""

    @pytest.mark.parametrize("command", [
        "smart-search search 'deepseek v4.1'",
        "cd /tmp && /usr/local/bin/smart-search search x",
        "FOO=1 sudo smart-search search q",
        "Smart-Search search q",
    ])
    def test_listed_program_gets_own_name_and_emoji(self, command: str) -> None:
        assert _tool_display_names("terminal", command) == ("smart-search", "smart-search")
        assert _tool_emoji("terminal", command) == "🔍"

    @pytest.mark.parametrize("command", [
        "ls -la",
        "grep smart-search notes.txt",       # 只是参数，不是程序
        "echo hi | smart-search-helper x",   # 名字不完全匹配
        "",
    ])
    def test_other_commands_stay_terminal(self, command: str) -> None:
        assert _tool_display_names("terminal", command) == ("Terminal", "终端命令")
        assert _tool_emoji("terminal", command) == "🖥️"

    def test_alias_only_applies_to_terminal(self) -> None:
        assert _tool_display_names("read_file", "smart-search search x") == ("Read", "读取文件")

    def test_display_step_drops_program_from_detail(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "smart-search search 'deepseek v4.1'")
        tracker.record_end("terminal", output="ok")
        step = tracker.build_display_steps()[0]
        assert step["title"].startswith("smart-search")
        assert step["title_zh"].startswith("smart-search")
        assert step["emoji"] == "🔍"
        assert step["detail"] == "search 'deepseek v4.1'"

    def test_spinner_label_follows_alias(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "smart-search search q")
        assert tracker.last_tool_names == ("smart-search", "smart-search")
        assert tracker.last_tool_emoji == "🔍"
        tracker.record_start("terminal", "ls -la")
        assert tracker.last_tool_names == ("Terminal", "终端命令")
        assert tracker.last_tool_emoji == "🖥️"

    def test_row_renders_search_emoji_outside_bold(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "smart-search search q")
        step = tracker.build_display_steps()[0]
        content = _build_tool_step_title(step)["text"]["content"]
        assert content.startswith("🔍 ")
        assert "**smart-search" in content

    @pytest.mark.parametrize("command", [
        "gh api repos/konbakuyomu/smartsearch/contents/README.md",
        "gh pr list --state open",
        "cd repo && gh release view v1.0",
    ])
    def test_gh_renders_as_github(self, command: str) -> None:
        assert _tool_display_names("terminal", command) == ("GitHub", "GitHub")
        assert _tool_emoji("terminal", command) == "🐙"

    def test_gh_detail_keeps_subcommand(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh api repos/konbakuyomu/smartsearch/contents/README.md")
        step = tracker.build_display_steps()[0]
        assert step["title"].startswith("GitHub")
        assert step["emoji"] == "🐙"
        assert step["detail"].startswith("api repos/konbakuyomu/smartsearch")

    def test_ghq_or_ghost_are_not_gh(self) -> None:
        assert _tool_display_names("terminal", "ghq get foo/bar") == ("Terminal", "终端命令")
        assert _tool_display_names("terminal", "ghost run") == ("Terminal", "终端命令")

    @pytest.mark.parametrize("command", [
        'python3 /opt/data/.hermes/profiles/image/workspace/scripts/zimage_gen.py "Eye-level shot"',
        'python3 /opt/data/.hermes/profiles/image/workspace/scripts/gpt_image_gen.py --size 1024 "a cat"',
        './gpt_image_gen.py "x"',
        "cd /tmp && python run_gpt_image.py --n 2",
        "bash make_image.sh",
    ])
    def test_any_image_script_renders_as_image_generation(self, command: str) -> None:
        """老大 2026-09-21：脚本名里含 image 的一律按生成图片显示，不逐个列脚本名。"""
        assert _tool_display_names("terminal", command) == ("Generate image", "生成图片")
        assert _tool_emoji("terminal", command) == "🎨"

    @pytest.mark.parametrize("command", [
        "ls -l /opt/x/cache/images/a.png",          # image 只在参数路径里
        "python3 -m pip install imageio",           # 解释器后面是选项，不是脚本
        '/opt/hermes/.venv/bin/python -c "print(1)"',
        "docker logs octopus | grep -i images/generations",
        "cat << EOF > /tmp/x.py\nimport image\nEOF",
    ])
    def test_image_only_in_arguments_stays_terminal(self, command: str) -> None:
        assert _tool_display_names("terminal", command) == ("Terminal", "终端命令")

    def test_image_script_detail_drops_interpreter_and_script(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", 'python3 /opt/data/x/scripts/gpt_image_gen.py --size 1024 "a cat"')
        step = tracker.build_display_steps()[0]
        assert step["title_zh"].startswith("生成图片")
        assert step["emoji"] == "🎨"
        assert step["detail"] == '--size 1024 "a cat"'
