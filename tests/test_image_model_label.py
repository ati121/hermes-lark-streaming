"""GPT Image labels use the selected model without inspecting prompt text."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_lark_streaming.patching import _maybe_wrap_callbacks, _msg_ctx
from hermes_lark_streaming.state.tooluse import _tool_display_names, _tool_emoji


@pytest.mark.parametrize("model", ["gpt-image-1", "gpt-image-1.5", "GPT Image 2", "gpt-image-2-mini", "openai/gpt-image-1"])
def test_gpt_image_models_share_label(model):
    args = {"model": model, "prompt": "a cat"}
    assert _tool_display_names("image_generate", args=args) == ("GPT · Generate image", "GPT · 生成图片")
    assert _tool_emoji("image_generate", args=args) == "🎨"


@pytest.mark.parametrize("args", [{}, {"model": "flux"}, {"prompt": "use gpt-image-1"}, {"model": "gpt-5"}])
def test_other_or_unknown_models_keep_generic_label(args):
    assert _tool_display_names("image_generate", args=args) == ("Generate image", "生成图片")


def test_image_tool_snapshots_profile_model_without_mutating_arguments(monkeypatch):
    module = ModuleType("tools.image_generation_tool")
    module._read_configured_image_model = lambda: "gpt-image-1.5"
    monkeypatch.setitem(sys.modules, "tools.image_generation_tool", module)
    notify = Mock(return_value=True)
    monkeypatch.setattr("hermes_lark_streaming.patching.hooks.on_tool_updated", notify)
    original = Mock()
    agent = SimpleNamespace(tool_progress_callback=original)
    token = _msg_ctx.set({"message_id": "image-generation"})
    try:
        _maybe_wrap_callbacks(agent)
        args = {"prompt": "a cat"}
        agent.tool_progress_callback("tool.started", "image_generate", "a cat", args)
        snapshot = notify.call_args.kwargs["tool_args"]
        assert snapshot == {"prompt": "a cat", "model": "gpt-image-1.5"}
        assert args == {"prompt": "a cat"}
        assert _tool_display_names("image_generate", args=snapshot)[1] == "GPT · 生成图片"
        original.assert_not_called()
    finally:
        _msg_ctx.reset(token)
