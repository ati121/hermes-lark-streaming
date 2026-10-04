"""Official artwork resolves per client, including partial card updates."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.cardkit.elements import (
    _build_tool_step_title,
    _loading_element,
    _loading_hint_element,
)
from hermes_lark_streaming.feishu.branding import OPENVIKING_ASSET, BrandAssets
from hermes_lark_streaming.state.tooluse import ToolUseTracker


@pytest.mark.asyncio
async def test_artwork_cached_and_payload_not_mutated():
    assets = BrandAssets()
    upload = AsyncMock(return_value="img_test_official")
    payload = {"elements": [_loading_hint_element("openviking_recall")]}
    outputs = await asyncio.gather(*(assets.resolve(payload, upload) for _ in range(3)))
    upload.assert_awaited_once()
    assert payload["elements"][0]["icon"]["img_key"] == OPENVIKING_ASSET
    assert all(o["elements"][0]["icon"]["img_key"] == "img_test_official" for o in outputs)


@pytest.mark.asyncio
async def test_upload_failure_falls_back_without_repeated_uploads():
    upload = AsyncMock(return_value=None)
    assets = BrandAssets()
    payload = [{"params": {"partial_element": _loading_hint_element("openviking_recall")}}]
    result = await assets.resolve(payload, upload)
    assert result[0]["params"]["partial_element"]["icon"]["tag"] == "standard_icon"
    assert OPENVIKING_ASSET not in str(result)
    await assets.resolve(payload, upload)
    upload.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_cards_do_not_upload_and_bot_keys_are_isolated():
    upload = AsyncMock(return_value="img_first_bot")
    card = _loading_hint_element()
    assert await BrandAssets().resolve(card, upload) is card
    upload.assert_not_called()
    icon = _loading_hint_element("openviking_recall")
    first = await BrandAssets().resolve(icon, upload)
    second = await BrandAssets().resolve(icon, AsyncMock(return_value="img_second_bot"))
    assert first["icon"]["img_key"] != second["icon"]["img_key"]


@pytest.mark.parametrize("name", ["viking_prefetch", "viking_search", "viking_read", "viking_browse", "viking_remember", "viking_forget", "viking_add_resource"])
def test_official_icon_on_every_openviking_tool(name):
    title = _build_tool_step_title({"name": name, "title": "OpenViking · Memory", "emoji": "📖"})
    assert title["icon"]["img_key"] == OPENVIKING_ASSET
    assert "📖" not in title["text"]["content"]
    spinner = _loading_element(("OpenViking · Memory", "OpenViking · 记忆"), emoji="📖")
    assert spinner["icon"]["img_key"] == OPENVIKING_ASSET
    assert "📖" not in spinner["text"]["content"]


def test_non_openviking_tools_keep_their_icons():
    title = _build_tool_step_title({"name": "terminal", "title": "Terminal", "emoji": "🖥️"})
    assert "icon" not in title
    assert "🖥️" in title["text"]["content"]


def test_preparation_duration_does_not_include_later_model_wait(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("hermes_lark_streaming.state.tooluse.time.time", lambda: clock[0])
    tracker = ToolUseTracker()
    tracker.record_memory_retrieval(108)
    clock[0] += 20
    assert tracker.elapsed_ms == 108
    assert tracker.last_tool_names is None
    tracker.record_start("terminal")
    clock[0] += 1
    assert tracker.elapsed_ms == 1108
    assert tracker.last_tool_names == ("Terminal", "终端命令")
