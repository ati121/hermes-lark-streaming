"""Literal bullet glyphs must become real lists without rewriting code examples."""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit import (
    build_answer_elements,
    build_cron_card,
    build_gateway_card,
)
from hermes_lark_streaming.cardkit.md import render_answer_markdown


@pytest.mark.parametrize("final", [False, True])
def test_long_items_use_native_lists_on_every_frame(final: bool) -> None:
    source = (
        "• 🌿 **方案 A**：" + "窗口变窄时正文应当自然换行，" * 20 + "\n"
        "• 🎯 **方案 B**：保留 [链接](https://example.com) 和 `a*b`，计算 2*3"
    )
    rendered = render_answer_markdown(source, final=final)
    assert rendered == source.replace("• ", "- ").replace("2*3", "2\\*3")
    elements = build_answer_elements(rendered)
    assert len(elements) == 1
    assert elements[0]["tag"] == "markdown"
    assert elements[0]["content"] == rendered


@pytest.mark.parametrize("marker", ["•", "●", "▪", "◦"])
def test_manual_bullet_variants_preserve_continuations_and_crlf(marker: str) -> None:
    source = f"{marker} 第一行\r\n  第二行\r\n{marker} 下一项\r\n"
    assert render_answer_markdown(source) == source.replace(marker, "-")


def test_native_lists_keep_their_markers_numbers_and_nesting() -> None:
    source = "- 第一项\n    • 子项\n        ◦ 孙项\n+ 第二项\n* 第三项\n3. 第四项\n4) 第五项"
    assert render_answer_markdown(source) == source.replace("•", "-").replace("◦", "-")


@pytest.mark.parametrize(
    "code",
    [
        "```markdown\n• 示例\n```",
        "~~~markdown\n• 示例\n~~~",
        "````markdown\n```\n• 示例\n```\n````",
        "```markdown\n• 未闭合围栏",
        "~~~markdown\n• 未闭合围栏",
        "    • 缩进代码",
        "\t• 缩进代码",
        "`行内代码\n• 示例\n结束`",
        "``行内 ` 代码\n• 示例\n结束``",
    ],
)
def test_code_examples_do_not_turn_into_lists(code: str) -> None:
    source = f"• 正文\n\n示例：\n\n{code}"
    assert render_answer_markdown(source) == source.replace("• 正文", "- 正文")


def test_nested_fence_and_indented_code_inside_item_stay_literal() -> None:
    source = (
        "- 父项\n\n"
        "    ```markdown\n    • 示例\n    ```\n\n"
        "      • 缩进代码\n\n"
        "    • 真正的子项\n"
        "• 下一项"
    )
    assert render_answer_markdown(source) == source.replace("• 真正", "- 真正").replace("• 下一", "- 下一")


def test_top_level_fence_ends_list_context() -> None:
    source = "- 项目\n\n```text\nexample\n```\n\n    • 缩进代码\n\n• 正文"
    assert render_answer_markdown(source) == source.replace("• 正文", "- 正文")


@pytest.mark.parametrize(
    "source, expected",
    [
        ("未闭合 ` 标记\n• 正文", "未闭合 ` 标记\n- 正文"),
        ("`单反引号\n• 示例\n`` 长度不同\n• 正文", "`单反引号\n- 示例\n`` 长度不同\n- 正文"),
        ("``较长定界符 `\n• 示例\n结束``\n• 正文", "``较长定界符 `\n• 示例\n结束``\n- 正文"),
        ("`第一段`\n• 正文\n`第二段\n• 示例\n结束`", "`第一段`\n- 正文\n`第二段\n• 示例\n结束`"),
        ("``未闭合\n`独立代码段\n• 示例\n结束`\n• 正文", "``未闭合\n`独立代码段\n• 示例\n结束`\n- 正文"),
    ],
)
def test_inline_code_requires_matching_delimiter_lengths(source: str, expected: str) -> None:
    assert render_answer_markdown(source) == expected


def test_bullets_in_prose_tables_escaped_text_and_rules_stay_literal() -> None:
    source = (
        "行内 • 圆点和 `• 代码`\n"
        "\\• 转义的圆点\n\n"
        "* * *\n\n"
        "| 标记 | 内容 |\n| --- | --- |\n| • 示例 | 不改 |\n\n"
        "• 真正的列表"
    )
    assert render_answer_markdown(source) == source.replace("• 真正", "- 真正")


@pytest.mark.parametrize("builder", [build_cron_card, build_gateway_card])
def test_static_cards_use_native_lists_too(builder) -> None:
    source = "• **第一项**\n• 第二项\n\n```text\n• 示例\n```"
    card = builder(source)
    assert card["body"]["elements"][0]["content"] == (
        "- **第一项**\n- 第二项\n\n```text\n• 示例\n```"
    )
