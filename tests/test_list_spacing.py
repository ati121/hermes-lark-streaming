"""Feishu needs an element margin to preserve a blank line after a list."""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit import (
    build_answer_elements,
    build_cron_card,
    build_gateway_card,
)
from hermes_lark_streaming.cardkit.md import (
    render_answer_markdown,
    split_answer_spacing_blocks,
    split_list_spacing_blocks,
)

_LIST = "- 🌿 **第一项**：长内容自然换行\n- 🎯 第二项"
_SECTION = "📌 **一个说明**\n\n说明正文。"
_TABLE = "| 项目 | 结果 |\n| --- | --- |\n| A | B |"


@pytest.mark.parametrize("marker", ["-", "+", "*", "1.", "2)", "•", "●"])
@pytest.mark.parametrize("following", [_SECTION, "## 下一节\n\n正文", "普通段落", "> 引用"])
def test_blank_line_after_native_or_literal_list_becomes_boundary(marker: str, following: str) -> None:
    items = f"{marker} 第一项\n{marker} 第二项"
    assert split_list_spacing_blocks(f"{items}\n\n{following}") == [items, following]


@pytest.mark.parametrize(
    "source",
    [
        f"{_LIST}\n普通续行",  # no authored blank line
        "- 项目\n## 标题\n\n普通段落",  # a heading already ended the list
        "普通段落\n\n另一个段落",
        "- 项目\n\n- 同级项目\n\n    - 嵌套项目",
        "1. 项目\n\n2. 同级项目\n\n    1. 嵌套项目",
        "- 项目\n  延续正文\n\n  同一个条目的新段落",
        "- 项目\n\n    **条目内的标题**\n\n    同一个条目的正文",
        "    - 缩进代码\n\n普通段落",
        "\t- 缩进代码\n\n普通段落",
        "* * *\n\n普通段落",
        "\\- 转义的标记\n\n普通段落",
        "> - 引用内的项目\n>\n> 引用内的正文",
        "`跨行代码\n- 示例\n\n普通段落\n结束`",
        "- `条目中的跨行代码\n\n仍在代码内\n结束`",
        "```markdown\n- 示例\n\n📌 标题\n```",
        "~~~markdown\n- 示例\n\n📌 标题\n~~~",
        "````markdown\n```\n- 示例\n\n📌 标题\n```\n````",
        "```markdown\n- 未闭合围栏\n\n📌 标题",
        "~~~markdown\n- 未闭合围栏\n\n📌 标题",
        "- 列表\n\n  ```markdown\n  - 示例\n\n  标题\n  ```",
        "- 列表\n\n  ~~~markdown\n  - 示例\n\n  标题\n  ~~~",
        "  - 缩进列表\n\n    列表正文",
        f"{_LIST}\n\n",  # no empty element while a stream waits for the next token
    ],
)
def test_internal_spacing_and_code_examples_are_unchanged(source: str) -> None:
    assert split_list_spacing_blocks(source) == [source]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_item_fence_does_not_hide_the_real_next_section(fence: str) -> None:
    items = f"- 项目\n\n  {fence}markdown\n  - 示例\n\n  标题\n  {fence}"
    assert split_list_spacing_blocks(f"{items}\n\n{_SECTION}") == [items, _SECTION]


def test_list_followed_by_top_level_code_gets_margin_before_the_code() -> None:
    code = "```markdown\n- 示例\n\n标题\n```"
    assert split_list_spacing_blocks(f"{_LIST}\n\n{code}\n\n正文") == [
        _LIST, f"{code}\n\n正文",
    ]


def test_multiple_sections_keep_heading_and_its_paragraph_together() -> None:
    first = f"📌 **第一节**\n\n{_LIST}"
    second = f"📌 **第二节**\n\n{_LIST}"
    assert split_list_spacing_blocks(f"{first}\n\n{second}\n\n{_SECTION}") == [
        first, second, _SECTION,
    ]


def test_crlf_and_whitespace_only_blank_lines_keep_content_line_endings() -> None:
    items = _LIST.replace("\n", "\r\n")
    section = _SECTION.replace("\n", "\r\n")
    assert split_list_spacing_blocks(f"{items}\r\n \t\r\n{section}") == [items, section]


def test_list_and_table_boundaries_share_one_margin() -> None:
    source = f"{_LIST}\n\n{_TABLE}\n\n{_LIST}\n\n{_SECTION}"
    elements = build_answer_elements(source)
    assert [el["content"] for el in elements] == [_LIST, _TABLE, _LIST, _SECTION]
    assert [el["margin"] for el in elements] == ["0px 0px 0px 0px"] + ["16px 0px 0px 0px"] * 3


@pytest.mark.parametrize("final", [False, True])
def test_real_margin_and_device_aliases_after_normalizing_manual_bullets(final: bool) -> None:
    source = f"{_LIST.replace('- ', '• ')}\n\n{_SECTION}"
    sizes = {"body": {"default": "normal", "mobile": "x-large"}}
    elements = build_answer_elements(render_answer_markdown(source, final=final), text_sizes=sizes)
    assert [el["content"] for el in elements] == [_LIST, _SECTION]
    assert elements[1]["margin"] == "16px 0px 0px 0px"
    assert all(el["text_size"] == "hls_body" for el in elements)
    assert len({el["element_id"] for el in elements}) == len(elements)
    assert all(len(el["element_id"]) <= 20 for el in elements)


def test_many_sections_stay_within_element_budget_without_losing_text() -> None:
    sections = [f"**Section {i}**\n\n- item {i}" for i in range(250)]
    source = "\n\n".join(sections)
    blocks = split_answer_spacing_blocks(source)
    assert len(blocks) == 64
    assert "\n\n".join(blocks) == source


@pytest.mark.parametrize("builder", [build_cron_card, build_gateway_card])
def test_static_cards_use_explicit_list_spacing(builder) -> None:
    elements = builder(f"{_LIST}\n\n{_SECTION}")["body"]["elements"]
    assert [el["content"] for el in elements] == [_LIST, _SECTION]
    assert elements[1]["margin"] == "16px 0px 0px 0px"
