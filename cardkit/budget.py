"""Size limits shared by card construction and Feishu delivery."""

from __future__ import annotations

import json
from typing import Any

CARD_HARD_LIMIT_BYTES = 30_000
# Leave room for the API envelope and the renderer's expansion of styled text.
CARD_BUDGET_BYTES = 24_000
CARD_ELEMENT_LIMIT = 200
TEXT_CHUNK_BYTES = 12_000


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8", errors="surrogatepass"))


def count_elements(value: Any) -> int:
    if isinstance(value, dict):
        return ("tag" in value) + sum(count_elements(child) for child in value.values())
    if isinstance(value, list):
        return sum(count_elements(child) for child in value)
    return 0


def card_fits(
    card: dict[str, Any], *, byte_limit: int = CARD_BUDGET_BYTES,
    element_limit: int = CARD_ELEMENT_LIMIT,
) -> bool:
    return json_size(card) <= byte_limit and count_elements(card) <= element_limit


def split_plain_text(text: str, *, byte_limit: int = TEXT_CHUNK_BYTES) -> list[str]:
    """Split at a nearby paragraph, line or character boundary; preserve every character."""
    if not text:
        return []
    if byte_limit < 8:
        raise ValueError("byte_limit must allow an escaped character and quotes")
    if json_size(text) <= byte_limit:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        cut = _prefix_end(text, start, byte_limit)
        parts.append(text[start:cut])
        start = cut
    return parts


def _prefix_end(text: str, start: int, byte_limit: int) -> int:
    low = start + 1
    high = min(len(text), start + byte_limit)
    while low <= high:
        middle = (low + high) // 2
        if json_size(text[start:middle]) <= byte_limit:
            low = middle + 1
        else:
            high = middle - 1
    if high == len(text):
        return high
    midway = (start + high) // 2
    paragraph = text.rfind("\n\n", midway, high)
    if paragraph >= 0:
        return paragraph + 2
    newline = text.rfind("\n", midway, high)
    if newline >= 0:
        return newline + 1
    if high > start + 1 and text[high - 1:high + 1] == "\r\n":
        return high - 1
    return high


def text_prefix(text: str, *, byte_limit: int) -> str:
    """Return only the first byte-budgeted fragment without scanning the remainder."""
    if byte_limit < 8:
        raise ValueError("byte_limit must allow an escaped character and quotes")
    return text[:_prefix_end(text, 0, byte_limit)]
