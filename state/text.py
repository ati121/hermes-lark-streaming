"""文本累积器 — 增量式流式文本追踪."""

from __future__ import annotations

import re

REASONING_PREFIX = "Reasoning:\n"

_REASONING_TAG = r"(?:think(?:ing)?|thought|antthinking)"
_REASONING_TAG_RE = re.compile(r"<\s*(/?)\s*" + _REASONING_TAG + r"\s*>", re.IGNORECASE)
_REASONING_OPEN_RE = re.compile(r"<\s*" + _REASONING_TAG + r"\s*>", re.IGNORECASE)
_REASONING_CLOSE_RE = re.compile(r"<\s*/\s*" + _REASONING_TAG + r"\s*>", re.IGNORECASE)
_REASONING_PAIR_RE = re.compile(
    r"<\s*" + _REASONING_TAG + r"\s*>[\s\S]*?<\s*/\s*" + _REASONING_TAG + r"\s*>",
    re.IGNORECASE,
)
_REASONING_UNCLOSED_TAIL_RE = re.compile(
    r"<\s*" + _REASONING_TAG + r"\s*>[\s\S]*$", re.IGNORECASE,
)

def split_reasoning_text(text: str | None) -> dict[str, str | None]:
    if not isinstance(text, str) or not text.strip():
        return {}
    trimmed = text.strip()
    if trimmed.startswith(REASONING_PREFIX) and len(trimmed) > len(REASONING_PREFIX):
        return {"reasoning_text": _clean_reasoning_prefix(trimmed)}
    tagged = extract_thinking_content(text)
    stripped = strip_reasoning_tags(text)
    if not tagged and stripped == text:
        return {"answer_text": text}
    return {
        "reasoning_text": tagged or None,
        "answer_text": stripped or None,
    }

def extract_thinking_content(text: str) -> str:
    if not text:
        return ""
    result = ""
    last_index = 0
    in_thinking = False
    for match in _REASONING_TAG_RE.finditer(text):
        idx = match.start()
        if in_thinking:
            result += text[last_index:idx]
        in_thinking = match.group(1) != "/"
        last_index = match.end()
    if in_thinking:
        result += text[last_index:]
    return result.strip()

def strip_reasoning_tags(text: str) -> str:
    """去掉思考段，只留可见答案。

    顺序很重要：先删完整 <tag>…</tag> 对（原文先删标签会导致配对失败、
    思考正文残留在答案里），再把未闭合的 <tag> 连同它后面的一切一起删掉
    （流被截断时模型正在思考，尾巴全是思考），最后才清掉游离的闭合标签。
    未闭合尾巴必须在删游离开标签之前处理，否则开标签一被删掉就再也匹配不上了。
    """
    if not text:
        return text
    result = _REASONING_PAIR_RE.sub("", text)
    result = _REASONING_UNCLOSED_TAIL_RE.sub("", result)
    result = _REASONING_OPEN_RE.sub(
        lambda _: "",
        _REASONING_CLOSE_RE.sub("", result),
    )
    if result.strip().startswith(REASONING_PREFIX):
        result = ""
    return result

def _clean_reasoning_prefix(text: str) -> str:
    cleaned = re.sub(r"^Reasoning:\s*", "", text, flags=re.IGNORECASE)
    cleaned = "\n".join(
        line.replace("_", "") if line.startswith("_") and line.endswith("_") else line for line in cleaned.split("\n")
    )
    return cleaned.strip()

# ── 跨 chunk 思考标签拆分（stream_delta 按块到达，标签对可能劈在两块之间）──

_REASONING_TAG_NAMES = ("think", "thinking", "thought", "antthinking")
_MAX_TAG_LEN = max(len(n) for n in _REASONING_TAG_NAMES) + 3  # "<" "/" ">"


def _is_tag_prefix(cand: str) -> bool:
    """cand 是否可能补全成某个思考标签（是某个完整标签的严格前缀）。"""
    if not cand or not cand.startswith("<"):
        return False
    body = cand[1:].lstrip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    if not body:
        return True
    lowered = body.lower()
    return any(name.startswith(lowered) for name in _REASONING_TAG_NAMES)


def _partial_tag_tail(text: str) -> str:
    """text 末尾疑似半个标签的部分（窗口内从最左的 "<" 试起）。"""
    start = max(0, len(text) - _MAX_TAG_LEN)
    while True:
        cut = text.find("<", start)
        if cut == -1:
            return ""
        cand = text[cut:]
        if _is_tag_prefix(cand):
            return cand
        start = cut + 1


class ReasoningStreamSplitter:
    """把流式答案增量拆成 (思考增量, 正文增量)，标签开合状态跨 chunk 保留。

    逐块跑 ``split_reasoning_text`` 时，``<think>`` / ``</think>`` 分落在
    两个 chunk 会让中段漏进答案正文、或让闭合标签永远配不上对。这里
    持有每会话的开合状态，并把块尾疑似半个标签的部分压到下一个 chunk
    一起解析；流结束时用 :meth:`flush` 清空缓冲。

    "Reasoning:\\n" 前缀沿用逐块语义（与旧 on_answer 行为一致）：只认出现在
    块首的标记，标记块剩余部分全部算思考；前半截标记先压住等下一块拼满。
    """

    __slots__ = ("_in_think", "_pending")

    _MARKER = "Reasoning:\n"

    def __init__(self) -> None:
        self._in_think = False
        self._pending = ""

    def split(self, text: str | None) -> tuple[str, str]:
        """处理一个增量，返回 (reasoning_delta, answer_delta)。"""
        if self._pending:
            text = self._pending + (text or "")
            self._pending = ""
        if not text:
            return ("", "")
        stripped = text.lstrip()
        if stripped and len(stripped) <= len(self._MARKER) and self._MARKER.startswith(stripped):
            # 可能是标记的前半截（"Reas…" / "Reasoning:" / "Reasoning:\n"），
            # 等下一个 chunk 拼满再判
            self._pending = text
            return ("", "")
        if stripped.startswith(self._MARKER):
            # 标记成立：本块剩余全是思考（与 split_reasoning_text 全文语义一致）
            return (stripped[len(self._MARKER):].lstrip(), "")
        reasoning: list[str] = []
        answer: list[str] = []
        pos = 0
        for m in _REASONING_TAG_RE.finditer(text):
            seg = text[pos:m.start()]
            (reasoning if self._in_think else answer).append(seg)
            self._in_think = m.group(1) != "/"
            pos = m.end()
        rest = text[pos:]
        tail = _partial_tag_tail(rest)
        if tail:
            rest = rest[: len(rest) - len(tail)]
            self._pending = tail
        (reasoning if self._in_think else answer).append(rest)
        return ("".join(reasoning), "".join(answer))

    def flush(self) -> tuple[str, str]:
        """流结束时清空缓冲 — 压着的部分按当前开合状态归位。"""
        pending, self._pending = self._pending, ""
        return (pending, "") if self._in_think else ("", pending)

class TextState:
    """— dead code. The dirty-tracking mechanism was replaced by UnifiedLinearState's"""

    def __init__(self) -> None:
        self.completed_text = ""
        self.accumulated = ""

    @property
    def display_text(self) -> str:
        if self.accumulated:
            return self.accumulated
        return self.completed_text or ""

    def on_partial(self, text: str) -> None:
        if not text:
            return
        self.accumulated += text

    def on_deliver(self, text: str) -> None:
        text = strip_reasoning_tags(text)
        if self.completed_text:
            self.completed_text += "\n\n" + text
        else:
            self.completed_text = text
        if not self.accumulated:
            self.accumulated = text
