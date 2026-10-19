"""Official artwork resolves per client, including partial card updates."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.cardkit.elements import (
    _LOADING_IMG_KEY,
    _build_tool_step_title,
    _loading_element,
    _loading_hint_element,
)
from hermes_lark_streaming.feishu.branding import GITHUB_ASSET, OPENVIKING_ASSET, BrandAssets
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
    # The spinner row's icon slot is the animation and nothing else: Feishu
    # allows one icon per div, so brand artwork there would hide the spinner.
    spinner = _loading_element(("OpenViking · Memory", "OpenViking · 记忆"), emoji="📖")
    assert spinner["icon"]["img_key"] == _LOADING_IMG_KEY
    assert "📖" not in spinner["text"]["content"]


def test_non_openviking_tools_keep_their_icons():
    title = _build_tool_step_title({"name": "terminal", "title": "Terminal", "emoji": "🖥️"})
    assert "icon" not in title
    assert "🖥️" in title["text"]["content"]


class TestGitHubBrand:
    """gh 行用真实 GitHub mark，不再用 🐙 代指 Octocat."""

    def _step(self, command: str) -> dict:
        tracker = ToolUseTracker()
        tracker.record_start("terminal", command)
        return tracker.build_display_steps()[0]

    def test_gh_step_carries_the_github_brand(self):
        step = self._step("gh api repos/x/y")
        assert step["brand"] == "github"
        title = _build_tool_step_title(step)
        assert title["icon"]["img_key"] == GITHUB_ASSET
        assert "🐙" not in title["text"]["content"]

    def test_plain_terminal_step_has_no_brand(self):
        assert self._step("ls -la")["brand"] is None

    def test_local_gh_keeps_terminal_icon(self):
        step = self._step("gh --version")
        assert step["brand"] is None
        title = _build_tool_step_title(step)
        assert "icon" not in title
        assert "🖥️" in title["text"]["content"]

    def test_spinner_keeps_the_animation(self):
        """The spinner slot is the animation; the mark lives on the tool row.

        Feishu's ``div.icon`` is a single object (no list form) and ``lark_md``
        cannot render images, so putting the GitHub mark on the spinner would
        remove the loading animation. The panel row one line below already
        carries the mark, aligned with every other tool icon.
        """
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh pr list")
        assert tracker.status_tool_brand == "github"
        spinner = _loading_element(
            tracker.status_tool_names, emoji=tracker.status_tool_emoji, brand="github",
        )
        assert spinner["icon"]["img_key"] == _LOADING_IMG_KEY
        assert "🐙" not in spinner["text"]["content"]
        # And the tool row still shows the real mark.
        row = _build_tool_step_title(tracker.build_display_steps()[0])
        assert row["icon"]["img_key"] == GITHUB_ASSET

    def test_local_gh_spinner_keeps_terminal_emoji(self):
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh --version")
        assert tracker.status_tool_brand is None
        spinner = _loading_element(
            tracker.status_tool_names, emoji=tracker.status_tool_emoji,
            brand=tracker.status_tool_brand,
        )
        assert "🖥️" in spinner["text"]["content"]

    @pytest.mark.asyncio
    async def test_github_asset_uploads_and_replaces_the_marker(self):
        assets = BrandAssets()
        upload = AsyncMock(return_value="img_github")
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh pr list")
        payload = _build_tool_step_title(tracker.build_display_steps()[0])
        result = await assets.resolve(payload, upload)
        assert result["icon"]["img_key"] == "img_github"
        upload.assert_awaited_once()
        assert upload.await_args.args[0].replace("\\", "/").endswith("assets/github.png")

    @pytest.mark.asyncio
    async def test_github_upload_failure_falls_back_to_a_standard_icon(self):
        assets = BrandAssets()
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh pr list")
        payload = _build_tool_step_title(tracker.build_display_steps()[0])
        result = await assets.resolve(payload, AsyncMock(return_value=None))
        assert result["icon"]["tag"] == "standard_icon"
        assert GITHUB_ASSET not in str(result)

    @pytest.mark.asyncio
    async def test_openviking_and_github_assets_are_cached_separately(self):
        assets = BrandAssets()
        upload = AsyncMock(side_effect=lambda path: "img_gh" if "github" in path else "img_ov")
        openviking = await assets.resolve(_loading_hint_element("openviking_recall"), upload)
        tracker = ToolUseTracker()
        tracker.record_start("terminal", "gh pr list")
        github = await assets.resolve(_build_tool_step_title(tracker.build_display_steps()[0]), upload)
        assert openviking["icon"]["img_key"] == "img_ov"
        assert github["icon"]["img_key"] == "img_gh"
        assert upload.await_count == 2
        await assets.resolve(_build_tool_step_title(tracker.build_display_steps()[0]), upload)
        assert upload.await_count == 2


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
