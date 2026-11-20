"""Markdown 文本处理 — 标题降级、表格降级、图片 key 剥离、长文本分块."""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")

_MAX_CARD_TABLES = 20  # Preprocessing cap before final CardKit answer layout.
MAX_MARKDOWN_COMPONENT_TABLES = 4
_MAX_CRON_TABLES = 5   # 静态卡片：5表降级阈值（飞书 Card 2.0 单卡硬限）
_MAX_INTERACTIVE_TABLES = _MAX_CRON_TABLES  # Whole-card writes share the static-card limit.
_MAX_CHUNK_CHARS = 2400

# ── Pre-compiled regex patterns (P2-01: avoid recompilation on every call) ──
# Closed ``` / ~~~ fences anywhere, plus a fence opened at line start that is
# still unclosed: mid-stream (or after a cut-off reply) everything after the
# opener is code, so its ``*`` must not be escaped.
_RE_FENCED_CODE = re.compile(r'(`{3,}|~{3,})[\s\S]*?\1|(?:^|(?<=\n)) {0,3}(?:`{3,}|~{3,})[\s\S]*\Z')
_RE_INLINE_CODE = re.compile(r'`[^`]+`')
_RE_BOLD = re.compile(r'\*{2,3}(?!\s)((?:(?!\*{2,3}).)+?)(?<!\s)\*{2,3}', re.DOTALL)
_RE_VALID_ITALIC = re.compile(r'(?<![a-zA-Z0-9_])\*(?!\s)((?:(?!\*).)+?)(?<!\s)\*', re.DOTALL)
_RE_UNPAIRED_ASTERISK = re.compile(r'(?<!\\)\*(?=[^\s*])')
_RE_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_CODE_BLOCK_EXTRACT = re.compile(r"(^|\n)(`{3,}|~{3,})([^\n`]*)\n[\s\S]*?(?:\n\2(?=\n|$)|\Z)")
_RE_H1_TO_H3 = re.compile(r"^#{1,3} ", re.MULTILINE)
_RE_HEADING_DEMOTE = re.compile(r"^#{2,6} (.+)$", re.MULTILINE)
_RE_H1_DEMOTE = re.compile(r"^# (.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"(?:\r\n|\r|\n){3,}")
_RE_SHORT_MD_CHECK = re.compile(r'^#{1,6} |\n#{1,6} |```|!\[|(?:\r\n|\r|\n){3,}')
# v1.3.0: placeholder pattern for restoring protected code/bold/italic blocks
_RE_PROTECTED_PLACEHOLDER = re.compile(r'\x00P(\d+)P\x00')
_RE_BACKTICK_RUN = re.compile(r"`+")
_RE_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_RE_TABLE_DELIMITER = re.compile(r"^ {0,3}\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_RE_LIST_MARKER = re.compile(r"^([ \t]*)([-+*•●▪◦]|[0-9]{1,9}[.)])([ \t]+)(\S.*)$")
_RE_LIST_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_RE_THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_RE_BLOCK_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+|$)")
_MAX_ANSWER_SPACING_BLOCKS = 64

__all__ = [
    "_MAX_CRON_TABLES",
    "_downgrade_tables",
    "_find_tables_outside_code_blocks",
    "_split_long_text",
    "_strip_invalid_image_keys",
    "escape_markdown_asterisks",
    "optimize_markdown_style",
    "render_answer_markdown",
    "split_answer_spacing_blocks",
    "split_list_spacing_blocks",
    "split_table_spacing_blocks",
]

def _is_table_delimiter(line: str, *, min_dashes: int = 1) -> bool:
    line = line.strip()
    return "|" in line and all(
        re.fullmatch(rf":?-{{{min_dashes},}}:?", cell.strip())
        for cell in line.strip("|").split("|")
    )


def _find_tables_outside_code_blocks(text: str) -> list[tuple[int, int, str]]:
    """Find pipe tables outside fenced code, including CRLF and optional edges."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    results: list[tuple[int, int, str]] = []
    fence_char, fence_length = "", 0
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        fence = _RE_FENCE_LINE.match(line)
        if fence:
            run, suffix = fence.groups()
            if not fence_char:
                fence_char, fence_length = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_length and not suffix.strip():
                fence_char, fence_length = "", 0
            index += 1
            continue
        if fence_char or index + 1 >= len(lines) or len(line) - len(line.lstrip(" ")) > 3:
            index += 1
            continue
        if "|" not in line or not _is_table_delimiter(lines[index + 1]):
            index += 1
            continue
        end_index = index + 2
        while end_index < len(lines):
            row = lines[end_index].rstrip("\r\n")
            if not row.strip() or "|" not in row or _RE_FENCE_LINE.match(row):
                break
            end_index += 1
        start = offsets[index]
        end = offsets[end_index - 1] + len(lines[end_index - 1].rstrip("\r\n"))
        results.append((start, end, text[start:end]))
        index = end_index
    return results

def _downgrade_tables(text: str, limit: int = _MAX_CARD_TABLES) -> str:
    """超限表格降级为代码块（保留内容可见但飞书不渲染为表格元素）."""
    # Early return: no tables possible without pipe characters
    if '|' not in text:
        return text
    matches = _find_tables_outside_code_blocks(text)
    if len(matches) <= limit:
        return text
    result = text
    for start, end, raw in reversed(matches[limit:]):
        fence = "`" * max(3, _longest_backtick_run(raw) + 1)
        newline = "\r\n" if "\r\n" in raw else "\n"
        replacement = f"{fence}{newline}{raw}{newline}{fence}"
        result = result[:start] + replacement + result[end:]
    return result


def _enforce_markdown_table_budget(elements: list[dict], *, limit: int = _MAX_INTERACTIVE_TABLES) -> list[dict]:
    """Apply one table budget across a card, independently for each locale.

    Work on copied containers: a rendered card must not mutate a session's
    saved Markdown, reasoning snapshot or caller-owned elements. Run after
    chunking, which can repeat a large table's header in multiple elements.
    """
    locales = {""}

    def collect(value):
        if isinstance(value, dict):
            variants = value.get("i18n_content")
            if isinstance(variants, dict):
                locales.update(variants)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(elements)
    remaining = dict.fromkeys(locales, max(0, limit))

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(child) for key, child in value.items()}
        content = value.get("content")
        if value.get("tag") not in {"markdown", "lark_md"} or not isinstance(content, str):
            return result
        variants = value.get("i18n_content", {})
        if not isinstance(variants, dict):
            variants = {}
        rendered = {}
        for locale in locales:
            source = variants.get(locale, content) if locale else content
            if not isinstance(source, str):
                source = content
            count = len(_find_tables_outside_code_blocks(source)) if "|" in source else 0
            rendered[locale] = _downgrade_tables(
                source, limit=min(remaining[locale], MAX_MARKDOWN_COMPONENT_TABLES),
            )
            remaining[locale] = max(0, remaining[locale] - count)
        result["content"] = rendered[""]
        localized = {locale: rendered[locale] for locale in locales if locale and (
            locale in variants or rendered[locale] != rendered[""]
        )}
        if localized:
            result["i18n_content"] = localized
        return result

    return visit(elements)

def _strip_invalid_image_keys(text: str) -> str:
    """移除非 img_ 前缀的图片引用."""
    if "![" not in text:
        return text

    def _replace(m: re.Match) -> str:
        return m.group(0) if m.group(2).startswith("img_") else ""

    return _RE_IMAGE_REF.sub(_replace, text)

def escape_markdown_asterisks(text: str) -> str:
    """飞书 Markdown 解析器比 CommonMark 更激进——会把 2*4000+4*3000"""
    if '\x00' in text:
        text = text.replace('\x00', '')

    if '*' not in text:
        return text

    _protected: list[str] = []

    def _save(m: re.Match) -> str:
        _protected.append(m.group(0))
        return f'\x00P{len(_protected) - 1}P\x00'

    # Step 1: 保护代码区域
    text = _RE_FENCED_CODE.sub(_save, text)
    text = _RE_INLINE_CODE.sub(_save, text)

    # Step 2: 保护粗体 **...** 和 ***...***
    text = _RE_BOLD.sub(_save, text)

    text = _RE_VALID_ITALIC.sub(_save, text)

    # Step 4: 转义剩余 *（飞书可能误配对的）
    text = _RE_UNPAIRED_ASTERISK.sub(r'\\*', text)

    # Single pass instead of one full-text replace per placeholder (that was
    # O(P×N) and ran under the stream lock on every flush). A protected span
    # can itself hold earlier placeholders (code inside bold), so restore
    # recursively.
    if _protected:
        def _restore(m: re.Match) -> str:
            return _RE_PROTECTED_PLACEHOLDER.sub(_restore, _protected[int(m.group(1))])

        text = _RE_PROTECTED_PLACEHOLDER.sub(_restore, text)

    # Null bytes render as boxes (□) in Feishu and must never reach the API.
    if '\x00' in text:
        text = text.replace('\x00', '')

    return text

def _code_block_ranges(text: str) -> list[tuple[int, int]]:
    """Locate fenced/indented literal blocks without changing their line endings.

    Blank lines between indented code lines belong to that code block; trailing
    blank lines remain outside so ordinary paragraph spacing can be compressed.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        fence = _RE_FENCE_LINE.match(body)
        if fence:
            run, _ = fence.groups()
            end = index + 1
            while end < len(lines):
                closing = _RE_FENCE_LINE.match(lines[end].rstrip("\r\n"))
                if closing:
                    close_run, suffix = closing.groups()
                    if close_run[0] == run[0] and len(close_run) >= len(run) and not suffix.strip():
                        end += 1
                        break
                end += 1
            ranges.append((offsets[index], offsets[end - 1] + len(lines[end - 1].rstrip("\r\n"))))
            index = end
            continue
        if body.strip() and (len(body) - len(body.lstrip(" ")) >= 4 or body.startswith("\t")):
            end = index + 1
            last_code = index
            while end < len(lines):
                following = lines[end].rstrip("\r\n")
                if following.strip():
                    if len(following) - len(following.lstrip(" ")) < 4 and not following.startswith("\t"):
                        break
                    last_code = end
                end += 1
            ranges.append((offsets[index], offsets[last_code] + len(lines[last_code].rstrip("\r\n"))))
            index = end
            continue
        index += 1
    return ranges


def _inline_ranges_outside_blocks(text: str) -> list[tuple[int, int]]:
    # Mask literal blocks before pairing backticks, otherwise a fence or a code
    # example can accidentally pair with a later real inline-code delimiter.
    masked: list[str] = []
    cursor = 0
    for start, end in _code_block_ranges(text):
        masked.append(text[cursor:start])
        masked.append(re.sub(r"[^\r\n]", " ", text[start:end]))
        cursor = end
    masked.append(text[cursor:])
    return _inline_code_ranges("".join(masked))


def optimize_markdown_style(text: str) -> str:
    """Normalise prose while retaining literal code and its original blank lines."""
    if len(text) < 100 and not _RE_SHORT_MD_CHECK.search(text):
        return text
    try:
        def prose(value: str) -> str:
            if _RE_H1_TO_H3.search(value):
                value = _RE_HEADING_DEMOTE.sub(r'##### \1', value)
                value = _RE_H1_DEMOTE.sub(r'#### \1', value)
            return _RE_MULTI_NEWLINE.sub(
                lambda match: ("\r\n" if "\r\n" in match[0] else "\r" if "\r" in match[0] else "\n") * 2,
                value,
            )

        parts: list[str] = []
        cursor = 0
        for start, end in _code_block_ranges(text):
            parts.extend((prose(text[cursor:start]), text[start:end]))
            cursor = end
        parts.append(prose(text[cursor:]))
        r = "".join(parts)

        # 5. 剥离无效图片 key
        r = _strip_invalid_image_keys(r)

        return r
    except Exception:
        _logger.debug("optimize_markdown_style failed", exc_info=True)
        return text

def render_answer_markdown(text: str, *, final: bool = False, table_limit: int = _MAX_CARD_TABLES) -> str:
    """Normalise answer text for one card write — the single markdown exit.

    Four call sites used to assemble this pipeline by hand and disagreed:
    streaming writes skipped the table limit, the static fallback card skipped
    both the limit and the asterisk escape, and the interactive card applied
    ``optimize_markdown_style`` only to the final frame. Routing every answer
    write through here keeps the streamed prefix byte-identical to the sealed
    one, so the last partial update is not a visible re-layout.

    ``final`` adds the whole-text passes (heading/table normalisation). During
    streaming they are skipped because each delta would re-run them over an
    ever-growing string; the seal always runs them once.
    """
    if not text:
        return " "
    rendered = optimize_markdown_style(text) if final else text
    if final or table_limit != _MAX_CARD_TABLES:
        rendered = _downgrade_tables(rendered, limit=table_limit)
    return escape_markdown_asterisks(_normalize_list_markers(rendered)) or " "


def _inline_code_ranges(text: str) -> list[tuple[int, int]]:
    """Pair equal-length backtick runs in linear time, leaving unmatched runs literal.

    Index the next run of each length so unmatched openers do not repeatedly
    scan the remaining text. Reuse the existing token regex rather than a
    lookaround/backreference expression that resembles an inline shell DSL to
    Hermes' install scanner.
    """
    runs = list(_RE_BACKTICK_RUN.finditer(text))
    next_run: dict[int, int] = {}
    closing: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index].end() - runs[index].start()
        if length in next_run:
            closing[index] = next_run[length]
        next_run[length] = index
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        end = closing.get(index)
        if end is None:
            index += 1
        else:
            ranges.append((runs[index].start(), runs[end].end()))
            index = end + 1
    return ranges


def _normalize_list_markers(text: str) -> str:
    """Use Feishu's native list syntax so wrapped lines get a hanging indent.

    Literal bullets are plain text, so their wrapped lines start at the card's
    left edge. Replace them with ``- `` lists; preserve actual Markdown list
    markers, item text, spacing and nesting, and leave code/table examples alone.
    This runs on every frame, without adding card elements or hard line wraps.
    """
    if not any(marker in text for marker in "•●▪◦"):
        return text

    result: list[str] = []
    # (marker indentation, content indentation), in display columns.
    parents: list[tuple[int, int]] = []
    fence_char = ""
    fence_length = 0
    code_spans = iter(_inline_code_ranges(text))
    code_span = next(code_spans, None)
    offset = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        body = line.rstrip("\r\n")
        indent = len(body[:len(body) - len(body.lstrip(" \t"))].expandtabs(4))
        fence = _RE_LIST_FENCE.match(body)
        code_indent = (not parents and indent >= 4) or (
            parents and indent >= parents[-1][1] + 4
        )
        if fence and (fence_char or not code_indent):
            run, suffix = fence.groups()
            if not fence_char:
                if parents and indent < parents[-1][1]:
                    parents.clear()
                fence_char, fence_length = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_length and not suffix.strip():
                fence_char, fence_length = "", 0
            result.append(line)
            continue
        if fence_char or not body.strip():
            result.append(line)
            continue

        match = _RE_LIST_MARKER.match(body)
        if not match or _RE_THEMATIC_BREAK.fullmatch(body):
            if parents and indent < parents[-1][1]:
                parents.clear()
            result.append(line)
            continue

        marker_start = line_start + match.start(2)
        while code_span is not None and code_span[1] <= marker_start:
            code_span = next(code_spans, None)
        if code_span is not None and code_span[0] <= marker_start < code_span[1]:
            result.append(line)
            continue

        while parents and indent < parents[-1][0]:
            parents.pop()
        # Four-space indentation outside a list is code. Inside a list,
        # another four spaces beyond the item's content is also code.
        if (not parents and indent >= 4) or (
            parents and indent >= parents[-1][1] + 4
        ):
            result.append(line)
            continue
        if parents and indent == parents[-1][0]:
            parents.pop()
        prefix, marker, gap, _ = match.groups()
        parents.append((indent, len((prefix + marker + gap).expandtabs(4))))
        native = "-" if marker in "•●▪◦" else marker
        result.append(line[:match.start(2)] + native + line[match.end(2):])
    return "".join(result)


def _split_spacing_blocks(text: str, boundaries: set[tuple[int, int]]) -> list[str]:
    if not boundaries:
        return [text]
    blocks: list[str] = []
    cursor = 0
    for start, end in sorted(boundaries):
        if start < cursor:
            continue
        if text[cursor:start].strip():
            blocks.append(text[cursor:start])
        cursor = end
    if text[cursor:].strip():
        blocks.append(text[cursor:])
    return blocks or [text]


def split_list_spacing_blocks(text: str) -> list[str]:
    """Give blank lines after a top-level list a real card layout boundary.

    Blank lines between items, nested lists, continuation paragraphs and code
    inside an item stay in the same Markdown element. A single newline never
    introduces an extra margin. Fenced/indented code is not a list.
    """
    boundaries: set[tuple[int, int]] = set()
    # The top-level item's marker and content columns (nested items do not
    # change these). They distinguish an item continuation from a new section.
    list_indent: tuple[int, int] | None = None
    fence_char = ""
    fence_length = 0
    spans = iter(_inline_code_ranges(text))
    span = next(spans, None)
    blank_start: int | None = None
    last_end = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        body = line.rstrip("\r\n")
        prefix = body[:len(body) - len(body.lstrip(" \t"))]
        indent = len(prefix.expandtabs(4))
        position = start + len(prefix)
        while span is not None and span[1] <= position:
            span = next(spans, None)
        in_span = span is not None and span[0] <= position < span[1]
        fence = _RE_LIST_FENCE.match(body)
        if fence and in_span and span[0] == position:
            # A line-start fence takes precedence over the inline tokenizer's
            # pairing of its opening/closing backtick runs.
            in_span = False
        if fence_char:
            if fence:
                run, suffix = fence.groups()
                if run[0] == fence_char and len(run) >= fence_length and not suffix.strip():
                    fence_char, fence_length = "", 0
            last_end, blank_start = start + len(body), None
            continue
        if not body.strip():
            if blank_start is None:
                blank_start = last_end
            continue

        item = _RE_LIST_MARKER.match(body)
        if _RE_THEMATIC_BREAK.fullmatch(body) or in_span:
            item = None
        # At a blank-line boundary, a dedented non-item starts a new section.
        # Blank lines before another item or its indented body are internal.
        if list_indent is not None and not in_span and indent < list_indent[1]:
            if item is None:
                if blank_start is not None:
                    boundaries.add((blank_start, start))
                    list_indent = None
                elif fence or _RE_BLOCK_HEADING.match(body) or _RE_THEMATIC_BREAK.fullmatch(body):
                    list_indent = None
            elif indent <= list_indent[0]:
                list_indent = None
        if item is not None and not in_span and list_indent is None and indent <= 3:
            marker, gap = item.group(2, 3)
            list_indent = (indent, len((prefix + marker + gap).expandtabs(4)))
        if fence and not in_span and (indent <= 3 or (
            list_indent is not None and indent < list_indent[1] + 4
        )):
            run, _ = fence.groups()
            fence_char, fence_length = run[0], len(run)
        last_end, blank_start = start + len(body), None
    return _split_spacing_blocks(text, boundaries)


def split_answer_spacing_blocks(text: str) -> list[str]:
    """Combine table and list spacing, sharing one element budget."""
    blocks = [block for table in split_table_spacing_blocks(text)
              for block in split_list_spacing_blocks(table)]
    if len(blocks) > _MAX_ANSWER_SPACING_BLOCKS:
        # Preserve all remaining text in one element rather than overflowing
        # Feishu's 200-tag limit on answers containing many short sections.
        blocks = blocks[:_MAX_ANSWER_SPACING_BLOCKS - 1] + [
            "\n\n".join(blocks[_MAX_ANSWER_SPACING_BLOCKS - 1:]),
        ]
    return blocks


def split_table_spacing_blocks(text: str) -> list[str]:
    """Convert blank lines beside pipe tables into separate layout blocks.

    Feishu collapses these blank lines inside one Markdown element. Keep the
    original text when no table boundary needs spacing, including fenced code.
    """
    if "|" not in text:
        return [text]
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    boundaries: set[tuple[int, int]] = set()
    fence_char = ""
    fence_length = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            run, suffix = fence.groups()
            if not fence_char:
                fence_char, fence_length = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_length and not suffix.strip():
                fence_char, fence_length = "", 0
            index += 1
            continue
        if fence_char or index + 1 >= len(lines):
            index += 1
            continue
        header = line.rstrip("\r\n")
        delimiter = lines[index + 1].rstrip("\r\n")
        if (
            len(header) - len(header.lstrip(" ")) > 3
            or "|" not in header
            or not _is_table_delimiter(delimiter, min_dashes=3)
        ):
            index += 1
            continue
        start = offsets[index]
        end_index = index + 2
        while end_index < len(lines):
            row = lines[end_index].rstrip("\r\n")
            if (
                not row.strip()
                or "|" not in row
                or len(row) - len(row.lstrip(" ")) > 3
            ):
                break
            end_index += 1
        end = offsets[end_index] - (len(lines[end_index - 1]) - len(lines[end_index - 1].rstrip("\r\n")))
        before = re.search(r"(?:\r?\n[ \t]*){2,}$", text[:start])
        if before and text[:before.start()].strip():
            boundaries.add((before.start(), start))
        after = re.match(r"(?:[ \t]*\r?\n){2,}", text[end:])
        if after and text[end + after.end():].strip():
            boundaries.add((end, end + after.end()))
        index = end_index
    return _split_spacing_blocks(text, boundaries)


def _longest_backtick_run(value: str) -> int:
    return max((len(m) for m in _RE_BACKTICK_RUN.findall(value)), default=0)


def _open_fence_at_end(text: str) -> str:
    """Return the opener line of a code fence left open at the end of ``text``."""
    opener = ""
    fence_char, fence_length = "", 0
    for line in text.split("\n"):
        fence = _RE_FENCE_LINE.match(line.rstrip("\r"))
        if not fence:
            continue
        run, suffix = fence.groups()
        if not fence_char:
            fence_char, fence_length, opener = run[0], len(run), line.strip()
        elif run[0] == fence_char and len(run) >= fence_length and not suffix.strip():
            fence_char, fence_length, opener = "", 0, ""
    return opener


def _pick_cut(text: str, limit: int) -> int:
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return cut


def _split_long_text(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    return [rendered for _, rendered in split_markdown_source(text, limit=limit)]


def _literal_markdown_source(text: str, limit: int) -> list[tuple[str, str]]:
    """Render an oversized syntax unit literally, preserving exact source slices."""
    # Either delimiter is valid for a fenced block. Pick the shorter safe one
    # so embedded backticks (including inline-code delimiters) stay visible.
    backticks = "`" * max(3, _longest_backtick_run(text) + 1)
    tildes = "~" * max(3, max((len(run) for run in re.findall(r"~+", text)), default=0) + 1)
    fence = min((backticks, tildes), key=len)
    budget = limit - 2 * len(fence) - 2
    if budget <= 0:
        # A pathological delimiter run can itself exceed a chunk. Slice first
        # so each piece can choose a different, shorter safe fence.
        if len(text) <= 1 or limit < 10:
            raise ValueError("limit is too small for literal Markdown")
        middle = len(text) // 2
        return _literal_markdown_source(text[:middle], limit) + _literal_markdown_source(text[middle:], limit)
    chunks: list[tuple[str, str]] = []
    while text:
        cut = len(text) if len(text) <= budget else max(1, _pick_cut(text, budget))
        source, text = text[:cut], text[cut:]
        chunks.append((source, f"{fence}\n{source}\n{fence}"))
    return chunks


def split_markdown_source(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Split Markdown safely, retaining the exact source slice for each result."""
    if len(text) <= limit:
        return [(text, text)]
    literal_ranges: list[tuple[int, int]] = []
    for start, end, table in _find_tables_outside_code_blocks(text):
        rows = table.splitlines()
        # Repeating a header cannot repair a row wider than a complete chunk.
        # Keep the entire table readable as literal source instead of emitting
        # a dangling cell or a continuation with no valid table header.
        header_size = len(rows[0]) + len(rows[1]) + 1
        if any(len(row) + header_size + 2 > limit for row in rows[2:]) or header_size >= limit:
            literal_ranges.append((start, end))
    literal_ranges.extend(
        (start, end) for start, end in _inline_ranges_outside_blocks(text) if end - start > limit
    )
    if not literal_ranges:
        return _split_markdown_source_regular(text, limit)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(literal_ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    chunks: list[tuple[str, str]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            chunks.extend(_split_markdown_source_regular(text[cursor:start], limit))
        chunks.extend(_literal_markdown_source(text[start:end], limit))
        cursor = end
    if cursor < len(text):
        chunks.extend(_split_markdown_source_regular(text[cursor:], limit))
    return chunks


def _split_markdown_source_regular(text: str, limit: int) -> list[tuple[str, str]]:
    """将超长文本按段落/换行拆分为多个不超过 limit 字符的块.

    Each chunk becomes its own markdown element, so a cut must not leave a
    code fence or table half-open: a fence open at the cut is closed in this
    chunk and reopened (same opener line, language kept) in the next; a cut
    inside a table moves to before the table, or — for a table longer than one
    chunk — falls between rows and repeats the header rows in the next chunk.
    """
    if len(text) <= limit:
        return [(text, text)]
    source = text
    source_index = 0
    chunks: list[tuple[str, str]] = []
    carry = ""  # fence opener / table header re-emitted at the next chunk head
    while text:
        head = 0
        if carry:
            text = f"{carry}\n{text}"
            head = len(carry) + 1
            carry = ""
        if len(text) <= limit:
            chunks.append((source[source_index:], text))
            break
        cut = _pick_cut(text, limit)
        opener = _open_fence_at_end(text[:cut])
        if opener and len(opener) >= limit // 4:
            # An abnormally long fence cannot be repeated within the budget.
            # The source chunk is still available for literal-text fallback.
            opener = ""
        if opener:
            close = re.match(r"`+|~+", opener).group(0)
            # Reserve room for the closing fence so the chunk stays in budget.
            cut = _pick_cut(text, limit - len(close) - 1)
            opener = _open_fence_at_end(text[:cut])
        if cut <= head:
            cut = limit  # always make progress past the carried prefix
        if opener:
            close = re.match(r"`+|~+", opener).group(0)
            body = text[:cut].rstrip("\n")
            rendered = f"{body}\n{close}"
            carry = opener if len(opener) < limit // 4 else ""
        else:
            for start, end in _inline_ranges_outside_blocks(text):
                if start < cut < end:
                    # A span that fits by itself moves intact to the next
                    # component. At the start, include it in this component.
                    cut = start if start > head else end
                    break
            line_start = text.rfind("\n", 0, cut) + 1
            at_boundary = cut == len(text) or text[cut] == "\n"
            current = text[line_start:cut]
            next_break = text.find("\n", cut + 1)
            following = text[cut + 1:next_break if next_break != -1 else len(text)] if at_boundary else ""
            in_table = "|" in current and (not at_boundary or "|" in following)
            if in_table:
                table_start = line_start
                while table_start > 0:
                    prev_start = text.rfind("\n", 0, table_start - 1) + 1
                    if "|" not in text[prev_start:table_start - 1]:
                        break
                    table_start = prev_start
                rows = text[table_start:].split("\n", 2)
                if len(rows) > 2 and _is_table_delimiter(rows[1]):
                    if table_start > head and text[:table_start].strip():
                        cut = table_start
                    else:
                        if not at_boundary and line_start - 1 > max(head, table_start):
                            cut = line_start - 1
                        header = f"{rows[0]}\n{rows[1]}"
                        if len(header) < limit and cut > table_start + len(header):
                            carry = header
            rendered = text[:cut].rstrip("\n")
        rest = text[cut:]
        text = rest.lstrip("\n")
        source_next = source_index + cut - head + len(rest) - len(text)
        chunks.append((source[source_index:source_next], rendered))
        source_index = source_next
    return chunks
