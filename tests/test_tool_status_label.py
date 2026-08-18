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
    _TOOL_SPECS,
    _humanize_tool_name,
    _resolve_tool_descriptor,
    _tool_display_names,
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


class TestActiveToolNames:
    def test_none_when_no_tools(self) -> None:
        assert ToolUseTracker().active_tool_names is None

    def test_reports_running_tool(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal", "ls -la")
        assert t.active_tool_names == ("Terminal", "终端命令")

    def test_none_after_completion(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_end("terminal", output="ok")
        assert t.active_tool_names is None

    def test_reports_latest_of_several_running(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_start("read_file")
        assert t.active_tool_names == ("Read", "读取文件")

    def test_falls_back_to_earlier_running_tool(self) -> None:
        t = ToolUseTracker()
        t.record_start("terminal")
        t.record_start("read_file")
        t.record_end("read_file", output="done")
        assert t.active_tool_names == ("Terminal", "终端命令")


class TestLoadingStatusText:
    def test_blank_when_no_label(self) -> None:
        text = _loading_status_text(None)
        assert text == {"tag": "plain_text", "content": " "}

    def test_tag_never_changes(self) -> None:
        """Feishu rejects a changed tag on partial update — both forms must match."""
        assert _loading_status_text(None)["tag"] == _loading_status_text(("Terminal", "终端命令"))["tag"]

    def test_chinese_rendering(self) -> None:
        text = _loading_status_text(("Terminal", "终端命令"))
        assert text["i18n_content"]["zh_cn"] == "正在调用 终端命令"
        assert text["i18n_content"]["en_us"] == "Calling Terminal..."

    def test_label_lands_in_the_spinner_row(self) -> None:
        el = _loading_element(("Read", "读取文件"))
        assert el["element_id"] == _LOADING_ELEMENT_ID
        assert el["icon"]["tag"] == "custom_icon"  # the three dots
        assert el["text"]["i18n_content"]["zh_cn"] == "正在调用 读取文件"

    def test_spinner_row_adds_no_elements(self) -> None:
        from hermes_lark_streaming.cardkit.elements import _count_tag_objects

        assert _count_tag_objects(_loading_element()) == _count_tag_objects(
            _loading_element(("Read", "读取文件")),
        )


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
        assert partial["text"]["i18n_content"]["zh_cn"] == "正在调用 终端命令"
        assert session._loading_label == ("Terminal", "终端命令")

    def test_skips_when_unchanged(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(_loading_label=("Terminal", "终端命令"))
        session.tool_use.record_start("terminal")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))
        client.cardkit_batch_update.assert_not_awaited()

    def test_clears_label_when_tool_finishes(self) -> None:
        client = MagicMock()
        client.cardkit_batch_update = AsyncMock()
        session = _make_session(_loading_label=("Terminal", "终端命令"))
        session.tool_use.record_start("terminal")
        session.tool_use.record_end("terminal", output="ok")

        asyncio.run(self._ctrl(client)._sync_loading_label(session))

        partial = client.cardkit_batch_update.await_args.args[1][0]["params"]["partial_element"]
        assert partial["text"] == {"tag": "plain_text", "content": " "}
        assert session._loading_label is None

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
