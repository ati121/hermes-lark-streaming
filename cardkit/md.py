"""Markdown 文本处理 — 标题降级、表格降级、图片 key 剥离、长文本分块."""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("hermes_lark_streaming")

_MAX_CARD_TABLES = 20  # 流式卡片：20表降级阈值（流式增量内容，飞书宽松执行）
_MAX_CRON_TABLES = 5   # 静态卡片：5表降级阈值（飞书 Card 2.0 单卡硬限）
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
_RE_TABLE_ROW = re.compile(r"\|.+\|\n\|[-:| ]+\|[\s\S]*?(?=\n\n|\n(?!\|)|$)")
_RE_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_CODE_BLOCK_EXTRACT = re.compile(r"(^|\n)(`{3,}|~{3,})([^\n`]*)\n[\s\S]*?(?:\n\2(?=\n|$)|\Z)")
_RE_H1_TO_H3 = re.compile(r"^#{1,3} ", re.MULTILINE)
_RE_HEADING_DEMOTE = re.compile(r"^#{2,6} (.+)$", re.MULTILINE)
_RE_H1_DEMOTE = re.compile(r"^# (.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_SHORT_MD_CHECK = re.compile(r'^#{1,6} |\n#{1,6} |```|!\[|\n{3,}')
# v1.3.0: placeholder pattern for restoring protected code/bold/italic blocks
_RE_PROTECTED_PLACEHOLDER = re.compile(r'\x00P(\d+)P\x00')
_RE_BACKTICK_RUN = re.compile(r"`+")
_RE_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_RE_TABLE_DELIMITER = re.compile(r"^ {0,3}\|(?:\s*:?-{3,}:?\s*\|)+\s*$")

__all__ = [
    "_MAX_CRON_TABLES",
    "_downgrade_tables",
    "_find_tables_outside_code_blocks",
    "_split_long_text",
    "_strip_invalid_image_keys",
    "escape_markdown_asterisks",
    "optimize_markdown_style",
    "render_answer_markdown",
    "split_table_spacing_blocks",
]

def _find_tables_outside_code_blocks(text: str) -> list[tuple[int, int, str]]:
    """查找代码块外的 markdown 表格，返回 [(start, end, raw), ...]."""
    code_ranges: list[tuple[int, int]] = []
    for m in _RE_FENCED_CODE.finditer(text):
        code_ranges.append((m.start(), m.end()))

    def _in_code(idx: int) -> bool:
        return any(s <= idx < e for s, e in code_ranges)

    results: list[tuple[int, int, str]] = []
    for m in _RE_TABLE_ROW.finditer(text):
        if not _in_code(m.start()):
            results.append((m.start(), m.end(), m.group(0)))
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
        replacement = f"```\n{raw}\n```"
        result = result[:start] + replacement + result[end:]
    return result

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

def optimize_markdown_style(text: str) -> str:
    """1. 提取代码块用占位符保护"""
    if len(text) < 100 and not _RE_SHORT_MD_CHECK.search(text):
        return text
    try:
        # 1. 提取代码块
        mark = "___CB_"
        code_blocks: list[str] = []

        def _extract(m: re.Match) -> str:
            prefix = m.group(1) or ""
            block = m.group(0)[len(prefix) :]
            idx = len(code_blocks)
            code_blocks.append(block)
            return f"{prefix}{mark}{idx}___"

        r = _RE_CODE_BLOCK_EXTRACT.sub(_extract, text)

        # 2. 标题降级（仅当存在 H1-H3 时）
        if _RE_H1_TO_H3.search(text):
            r = _RE_HEADING_DEMOTE.sub(r'##### \1', r)
            r = _RE_H1_DEMOTE.sub(r'#### \1', r)

        # 3. 压缩多余空行（在还原前进行，代码块内的空行保持原样）
        r = _RE_MULTI_NEWLINE.sub("\n\n", r)

        # 4. 还原代码块
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{mark}{i}___", block)

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
    rendered = _downgrade_tables(optimize_markdown_style(text), limit=table_limit) if final else text
    return escape_markdown_asterisks(rendered) or " "


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
        header = line.strip()
        delimiter = lines[index + 1].strip()
        cells = delimiter.strip("|").split("|")
        if (
            len(line) - len(line.lstrip(" ")) > 3
            or not header.startswith("|")
            or not delimiter.startswith("|")
            or not all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)
        ):
            index += 1
            continue
        start = offsets[index]
        end_index = index + 2
        while end_index < len(lines) and lines[end_index].lstrip(" ").startswith("|"):
            end_index += 1
        end = offsets[end_index] - (len(lines[end_index - 1]) - len(lines[end_index - 1].rstrip("\r\n")))
        before = re.search(r"(?:\r?\n[ \t]*){2,}$", text[:start])
        if before and text[:before.start()].strip():
            boundaries.add((before.start(), start))
        after = re.match(r"(?:[ \t]*\r?\n){2,}", text[end:])
        if after and text[end + after.end():].strip():
            boundaries.add((end, end + after.end()))
        index = end_index
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


def _is_table_line(line: str) -> bool:
    return line.lstrip(" ").startswith("|")


def _pick_cut(text: str, limit: int) -> int:
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return cut


def _split_long_text(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    """将超长文本按段落/换行拆分为多个不超过 limit 字符的块.

    Each chunk becomes its own markdown element, so a cut must not leave a
    code fence or table half-open: a fence open at the cut is closed in this
    chunk and reopened (same opener line, language kept) in the next; a cut
    inside a table moves to before the table, or — for a table longer than one
    chunk — falls between rows and repeats the header rows in the next chunk.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    carry = ""  # fence opener / table header re-emitted at the next chunk head
    while text:
        head = 0
        if carry:
            text = f"{carry}\n{text}"
            head = len(carry) + 1
            carry = ""
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = _pick_cut(text, limit)
        opener = _open_fence_at_end(text[:cut])
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
            chunks.append(f"{body}\n{close}")
            carry = opener
        else:
            line_start = text.rfind("\n", 0, cut) + 1
            at_boundary = cut == len(text) or text[cut] == "\n"
            current = text[line_start:cut]
            following = text[cut + 1:].split("\n", 1)[0] if at_boundary else ""
            in_table = _is_table_line(current) and (not at_boundary or _is_table_line(following))
            if in_table:
                table_start = line_start
                while table_start > 0:
                    prev_start = text.rfind("\n", 0, table_start - 1) + 1
                    if not _is_table_line(text[prev_start:table_start - 1]):
                        break
                    table_start = prev_start
                if table_start > head and text[:table_start].strip():
                    cut = table_start
                else:
                    if not at_boundary and line_start - 1 > max(head, table_start):
                        cut = line_start - 1  # never split a row mid-line
                    rows = text[table_start:].split("\n", 2)
                    if (
                        len(rows) > 2
                        and _RE_TABLE_DELIMITER.match(rows[1])
                        and cut > table_start + len(rows[0]) + len(rows[1]) + 1
                    ):
                        carry = f"{rows[0]}\n{rows[1]}"
            chunks.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    return chunks
