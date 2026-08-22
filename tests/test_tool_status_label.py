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
            ("memory", "记忆"),
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


class TestHindsightMemoryTools:
    """Hindsight's three operations, all named around 记忆."""

    def test_all_three_named_after_memory(self) -> None:
        assert _tool_display_names("hindsight_retain")[1] == "记忆写入"
        assert _tool_display_names("hindsight_recall")[1] == "记忆回溯"
        assert _tool_display_names("hindsight_reflect")[1] == "记忆推演"

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
            ("hindsight_recall", "🧠"),   # time_outlined, shared with cron
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
