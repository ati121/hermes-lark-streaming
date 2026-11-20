"""The answer, process panel and status share one live card budget."""

import pytest
from hermes_lark_streaming.cardkit.budget import card_fits, json_size
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.cardkit.elements import (
    _loading_element,
    _loading_status_text,
    _streaming_element,
    build_panel_header,
    build_unified_panel,
    limit_streaming_panel,
)
from hermes_lark_streaming.controller.linear_mixin import _streaming_preview
from hermes_lark_streaming.state.linear import ReasoningRound


@pytest.mark.parametrize("unit", ["a", "龘", "🧑‍💻", '\\"\t\x00'])
def test_live_card_combines_answer_panel_and_unbounded_tool_name(unit):
    name = "custom_" + unit * 6_000
    label = (name, name)
    panel = build_unified_panel(
        reasoning_rounds=[ReasoningRound(index=i, text=unit * 3_000) for i in range(20)],
        tool_steps=[], active_tool=label,
    )
    panel = limit_streaming_panel(panel, None)
    card = build_streaming_card_v2()
    card["body"]["elements"] = [
        panel,
        _streaming_element(_streaming_preview(unit * 40_000)),
        _loading_element(label),
    ]

    assert card_fits(panel, byte_limit=6_000, element_limit=120)
    assert card_fits(card)
    assert json_size(_loading_element(label)) < 1_500
    assert json_size(build_panel_header(
        reasoning_rounds=[], tool_steps=[], active_tool=label,
    )) < 2_000
    assert _loading_status_text(label)["content"].endswith("…")
    assert panel["header"]["title"]["content"].endswith("…")


def test_short_tool_display_names_keep_their_content():
    text = _loading_status_text(("Read files", "读取文件"))
    assert text["content"] == "  Read files"
    assert text["i18n_content"]["zh_cn"] == "  读取文件"
    header = build_panel_header(
        reasoning_rounds=[], tool_steps=[], active_tool=("Read files", "读取文件"),
    )
    assert "Read files" in header["title"]["content"]
    assert "…" not in str(header)
