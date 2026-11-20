"""Markdown pipeline fixes: single-pass escape restore, unclosed / tilde fences,
code-block blank lines, fence- and table-aware chunking for static cards."""

from __future__ import annotations

import re
import time

import pytest
from hermes_lark_streaming.cardkit import build_cron_card, build_gateway_card, md
from hermes_lark_streaming.cardkit.elements import _build_error_panel
from hermes_lark_streaming.cardkit.md import (
    _open_fence_at_end,
    _split_long_text,
    escape_markdown_asterisks,
    optimize_markdown_style,
    split_markdown_source,
)


def _reference_escape(text: str) -> str:
    """The pre-v1.47 restore loop (one full-text replace per placeholder)."""
    if "\x00" in text:
        text = text.replace("\x00", "")
    if "*" not in text:
        return text
    protected: list[str] = []

    def _save(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00P{len(protected) - 1}P\x00"

    text = md._RE_FENCED_CODE.sub(_save, text)
    text = md._RE_INLINE_CODE.sub(_save, text)
    text = md._RE_BOLD.sub(_save, text)
    text = md._RE_VALID_ITALIC.sub(_save, text)
    text = md._RE_UNPAIRED_ASTERISK.sub(r"\\*", text)
    for i in range(len(protected) - 1, -1, -1):
        text = text.replace(f"\x00P{i}P\x00", protected[i])
    return text.replace("\x00", "")


class TestEscapeRestore:
    CASES = (
        "2*3*4",
        "**粗体** 与 *斜体* 以及 2*3",
        "inline `a*b` then 4*5",
        "```py\ndef f(*a, **k):\n    return a*2\n```\n然后 3*4",
        "**粗体里有 `c*d` 代码** 和 *斜体里 `e*f`* 结尾 7*8",
        "***三星*** **x** *y* `z*` ~~~\na*b\n~~~ 9*9",
        "嵌套 **外 *内* 外** 1*2*3",
        "\x00P0P\x00 伪造占位符 *a* 2*3",
        "".join(f"**b{i}** `c{i}*` {i}*{i} " for i in range(200)),
    )

    def test_matches_reference_implementation(self) -> None:
        for text in self.CASES:
            assert escape_markdown_asterisks(text) == _reference_escape(text), text

    def test_large_bold_dense_text_is_fast(self) -> None:
        text = "".join(f"**粗体{i}** 普通 2*{i} " for i in range(6000))
        assert len(text) > 100_000
        start = time.perf_counter()
        escape_markdown_asterisks(text)
        assert time.perf_counter() - start < 0.1


class TestFencedCodeEscape:
    def test_unclosed_backtick_fence_not_escaped(self) -> None:
        text = "示例：\n```py\ndef f(*args, **kw):"
        assert escape_markdown_asterisks(text) == text

    def test_tilde_fence_not_escaped(self) -> None:
        assert escape_markdown_asterisks("~~~\na*b*c\n~~~\n2*3") == "~~~\na*b*c\n~~~\n2\\*3"

    def test_inline_triple_backticks_do_not_swallow_rest(self) -> None:
        # Not at line start → not an opening fence; text after stays escaped.
        assert escape_markdown_asterisks("用 ``` 开头 2*3") == "用 ``` 开头 2\\*3"


class TestOptimizeKeepsCodeBlankLines:
    def test_backtick_block_blank_lines_kept(self) -> None:
        text = "# T\n\n```py\nimport os\n\n\ndef f():\n    pass\n```\n\n\n\n正文"
        result = optimize_markdown_style(text)
        assert "import os\n\n\ndef f()" in result
        assert "```\n\n正文" in result

    def test_tilde_block_blank_lines_kept(self) -> None:
        text = "# T\n\n~~~\na\n\n\n\nb\n~~~\n"
        assert "a\n\n\n\nb" in optimize_markdown_style(text)

    @pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
    def test_blank_line_compression_preserves_line_ending(self, ending: str) -> None:
        assert optimize_markdown_style(f"first{ending * 4}last") == f"first{ending * 2}last"

    @pytest.mark.parametrize("ending", ["\n", "\r\n"])
    @pytest.mark.parametrize("indent", ["    ", "\t"])
    def test_indented_code_blank_lines_kept(self, ending: str, indent: str) -> None:
        code = f"{indent}first{ending * 4}{indent}second"
        text = f"# T{ending * 3}{code}{ending * 4}正文"
        result = optimize_markdown_style(text)
        assert code in result
        assert result.startswith(f"#### T{ending * 2}")
        assert result.endswith(f"second{ending * 2}正文")

    def test_indented_crlf_fence_is_preserved(self) -> None:
        code = "  ```py\r\n# code\r\n\r\n\r\na = 1\r\n  ```"
        result = optimize_markdown_style(f"# T\r\n\r\n{code}\r\n\r\n\r\n正文")
        assert code in result
        assert result.endswith("```\r\n\r\n正文")


def _code_text(lines: int = 400, lang: str = "python") -> str:
    body = "\n".join(f"x_{i} = {i} * 2" for i in range(lines))
    return f"前言\n\n```{lang}\n{body}\n```\n\n结尾 *a*"


class TestSplitLongTextFences:
    def test_every_chunk_has_balanced_fences(self) -> None:
        parts = _split_long_text(_code_text(), limit=1000)
        assert len(parts) > 2
        for part in parts:
            assert len(part) <= 1000
            assert _open_fence_at_end(part) == ""

    def test_reopened_fence_keeps_language(self) -> None:
        parts = _split_long_text(_code_text(), limit=1000)
        for part in parts[1:-1]:
            assert part.startswith("```python\n")
            assert part.endswith("\n```")

    def test_code_lines_survive_split(self) -> None:
        parts = _split_long_text(_code_text(lines=300), limit=1000)
        joined = "\n".join(parts)
        for i in range(300):
            assert f"x_{i} = {i} * 2" in joined

    def test_tilde_fence_reopened(self) -> None:
        parts = _split_long_text(_code_text(lang="").replace("```", "~~~"), limit=1000)
        assert all(_open_fence_at_end(p) == "" for p in parts)
        assert parts[1].startswith("~~~\n")


def _table(rows: int) -> str:
    return "| A | B |\n|---|---|\n" + "\n".join(f"| {i} | {'y' * 20} |" for i in range(rows))


class TestSplitLongTextTables:
    def test_table_moved_whole_to_next_chunk(self) -> None:
        text = "段落 " * 150 + "\n\n" + _table(10)
        parts = _split_long_text(text, limit=700)
        table_parts = [p for p in parts if "|---|" in p]
        assert len(table_parts) == 1
        assert table_parts[0].startswith("| A | B |")

    def test_long_table_repeats_header(self) -> None:
        parts = _split_long_text(_table(150), limit=1000)
        assert len(parts) > 2
        for part in parts:
            assert part.startswith("| A | B |\n|---|---|\n")
            assert all(line.endswith("|") for line in part.split("\n"))

    def test_rows_not_lost(self) -> None:
        joined = "\n".join(_split_long_text(_table(150), limit=1000))
        for i in range(150):
            assert f"| {i} |" in joined

    @pytest.mark.parametrize("ending", ["\n", "\r\n"])
    def test_oversized_row_is_literal_and_keeps_exact_source(self, ending: str) -> None:
        table = ending.join(("| A | B |", "|---|---|", f"| {'x' * 3000} | z |", "| q | r |"))
        text = f"前言{ending * 2}{table}{ending * 2}结尾"
        parts = split_markdown_source(text, limit=500)
        assert "".join(source for source, _ in parts) == text
        assert all(len(rendered) <= 500 for _, rendered in parts)
        literal = [(source, rendered) for source, rendered in parts if source and source in table]
        assert literal
        for source, rendered in literal:
            assert rendered.startswith("```\n")
            assert rendered.endswith("\n```")
            assert source in rendered
            assert not _open_fence_at_end(rendered)
        assert "".join(source for source, _ in literal) == table

    def test_wide_header_is_repeated_when_one_row_still_fits(self) -> None:
        header = f"| {'a' * 130} | B |\n|---|---|\n"
        rows = [f"| {index} | {'x' * 25} |" for index in range(30)]
        text = header + "\n".join(rows)
        parts = split_markdown_source(text, limit=500)
        assert "".join(source for source, _ in parts) == text
        assert len(parts) > 1
        assert all(rendered.startswith(header) and len(rendered) <= 500 for _, rendered in parts)


class TestSplitLongTextInlineCode:
    @pytest.mark.parametrize("delimiter", ["`", "``"])
    def test_inline_code_moves_intact_across_chunk_boundary(self, delimiter: str) -> None:
        code = f"{delimiter}command " + "x" * 90 + delimiter
        text = "前" * 170 + " " + code + " 后" * 150
        parts = split_markdown_source(text, limit=240)
        assert "".join(source for source, _ in parts) == text
        assert sum(code in rendered for _, rendered in parts) == 1
        assert all(len(rendered) <= 240 for _, rendered in parts)
        assert all(rendered.count(delimiter) in (0, 2) for _, rendered in parts)

    def test_multiline_inline_code_is_not_cut_at_its_newline(self) -> None:
        code = "`" + "x" * 70 + "\n" + "y" * 70 + "`"
        text = "a" * 120 + code + "z" * 300
        parts = split_markdown_source(text, limit=200)
        assert "".join(source for source, _ in parts) == text
        assert sum(code in rendered for _, rendered in parts) == 1
        assert all(len(rendered) <= 200 for _, rendered in parts)

    def test_oversized_inline_code_is_literal_and_keeps_source(self) -> None:
        code = "``" + "x" * 1000 + "`inside`" + "y" * 1000 + "``"
        text = "前言 " + code + " 结尾"
        parts = split_markdown_source(text, limit=300)
        assert "".join(source for source, _ in parts) == text
        assert all(len(rendered) <= 300 for _, rendered in parts)
        literal = [(source, rendered) for source, rendered in parts if rendered.startswith(("```\n", "~~~\n"))]
        assert "".join(source for source, _ in literal) == code
        assert all(source in rendered and not _open_fence_at_end(rendered) for source, rendered in literal)


class TestStaticCards:
    def test_cron_card_escapes_asterisks(self) -> None:
        card = build_cron_card("结果 2*3*4 与 **粗体**")
        assert card["body"]["elements"][0]["content"] == "结果 2\\*3\\*4 与 **粗体**"

    def test_gateway_card_escapes_asterisks(self) -> None:
        card = build_gateway_card("a*b*c")
        assert card["body"]["elements"][0]["content"] == "a\\*b\\*c"

    def test_cron_summary_has_i18n(self) -> None:
        summary = build_cron_card("Hello\n```x```")["config"]["summary"]
        assert summary["content"] == "Hello x"
        assert summary["i18n_content"]["zh_cn"] == "Hello x"

    def test_long_code_cron_card_elements_balanced(self) -> None:
        card = build_cron_card(_code_text(lines=600))
        elements = card["body"]["elements"]
        assert len(elements) > 1
        assert all(_open_fence_at_end(e["content"]) == "" for e in elements)


class TestErrorPanelFence:
    def test_backticks_in_detail_use_longer_fence(self) -> None:
        panel = _build_error_panel("Traceback\n```\nboom\n```")
        content = str(panel)
        assert "````\\nTraceback\\n```\\nboom\\n```\\n````" in content
