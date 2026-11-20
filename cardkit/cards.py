"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .budget import CARD_BUDGET_BYTES, CARD_ELEMENT_LIMIT, card_fits, split_plain_text
from .elements import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _build_unified_panel_placeholder,
    _loading_element,
    _loading_hint_element,
    _streaming_element,
    build_card_text_size_style,
    enforce_element_limit,
)
from .i18n import _LOCALES, _T, _i18n, _t
from .md import (
    MAX_MARKDOWN_COMPONENT_TABLES,
    _downgrade_tables,
    _enforce_markdown_table_budget,
    _split_long_text,
)

__all__ = [
    '_enforce_card_element_limit',
    'build_interactive_card_v2',
    'build_streaming_card_v2',
    'card_visible_text',
    'paginate_card',
]


def _enforce_card_element_limit(
    card: dict[str, Any],
    *,
    panel_element_id: str = UNIFIED_PANEL_ELEMENT_ID,
    text_sizes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Backwards-compatible alias for :func:`enforce_element_limit`."""
    return enforce_element_limit(
        card, panel_element_id=panel_element_id, text_sizes=text_sizes,
    )


def _build_summary(text: str) -> dict[str, Any]:
    """Feishu CardKit 2.0 displays ``i18n_content.<locale>`` for users"""
    truncated = text[:120].replace("\n", " ").replace("```", "").strip()
    return {
        "content": truncated,
        "i18n_content": _i18n(truncated, truncated),
    }

def build_streaming_card_v2(
    *,
    tool_steps: list[dict] | None = None,
    elapsed_ms: float = 0,
    show_streaming_element: bool = True,
    streaming_panel_expanded: bool = True,
    print_strategy: str = "delay",
    print_step: int = 4,
    include_unified_panel: bool = True,
    include_loading_hint: bool = True,
    loading_hint_status: str = "loading_context",
    loading_status_key: str | None = None,
    loading_status_emoji: str | None = None,
    loading_status_brand: str | None = None,
    include_answer_element: bool = True,
    text_sizes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Card lifecycle (v1.0.2+):"""
    elements: list[dict] = []

    # ── Unified panel placeholder (linear mode — single panel for reasoning+tools) ──
    if include_unified_panel:
        elements.append(_build_unified_panel_placeholder(expanded=streaming_panel_expanded))

    # ── Streaming answer element ──
    if show_streaming_element and include_answer_element:
        elements.append(_streaming_element(
            element_id=ANSWER_ELEMENT_ID,
            text_sizes=text_sizes,
        ))

    # ── Loading hint (context loading placeholder, removed on first LLM token) ──
    if include_loading_hint:
        elements.append(_loading_hint_element(loading_hint_status))

    # ── Loading spinner (carries the live model/tool status once streaming starts) ──
    elements.append(_loading_element(
        status_key=loading_status_key,
        emoji=loading_status_emoji,
        brand=loading_status_brand,
        text_sizes=text_sizes,
    ))

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 70},
                "print_step": {"default": print_step},
                "print_strategy": print_strategy,
            },
            "locales": _LOCALES,
            "summary": {
                "content": _T["processing"][0],
                "i18n_content": _t("processing"),
            },
        },
        "body": {"elements": elements},
    }
    mapped_styles = build_card_text_size_style(text_sizes)
    if mapped_styles:
        card["config"]["style"] = {"text_size": mapped_styles}
    return card


def _split_large_markdown(element: dict[str, Any]) -> list[dict[str, Any]]:
    """Cut a large text payload at fenced Markdown or line boundaries."""
    content = element.get("content", "")
    variants = element.get("i18n_content", {})
    # Serialized control characters can cost six bytes per character. Leave
    # space for every localized version as well as the card/element wrapper.
    char_limit = max(100, min(3200, 3000 // (len(variants) + 1)))
    chunks = _split_long_text(content, limit=char_limit)
    local_chunks = {locale: _split_long_text(text, limit=char_limit) for locale, text in variants.items()}
    count = max(len(chunks), *(len(parts) for parts in local_chunks.values()), 1)
    result = []
    for index in range(count):
        piece = {**element, "content": _downgrade_tables(
            chunks[index] if index < len(chunks) else " ",
            limit=MAX_MARKDOWN_COMPONENT_TABLES,
        )}
        if local_chunks:
            piece["i18n_content"] = {
                locale: _downgrade_tables(
                    parts[index] if index < len(parts) else " ", limit=MAX_MARKDOWN_COMPONENT_TABLES,
                )
                for locale, parts in local_chunks.items()
            }
        if index:
            piece.pop("element_id", None)
        result.append(piece)
    return result


def card_visible_text(card: dict[str, Any]) -> str:
    """Recover the visible text of one page if that page must be sent as text."""
    def texts(elements):
        for element in elements:
            if element.get("tag") == "markdown":
                yield element.get("content", "")
            elif element.get("tag") == "div":
                yield element.get("text", {}).get("content", "")
            if element.get("tag") == "collapsible_panel":
                yield from texts(element.get("elements", []))

    return "\n\n".join(part for part in texts(card["body"]["elements"]) if part.strip())


def paginate_card(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Pack a rendered card into ordered Feishu-sized pages without dropping body text."""
    elements = card["body"]["elements"]
    ids = {element.get("element_id") for element in elements}
    reserve_reasoning = "rsn_toggle" in ids and "rsn_body" not in ids

    def fits(candidate, *, first=False):
        if first and reserve_reasoning:
            # A reasoning button adds a div and its text when opened.
            return card_fits(candidate, byte_limit=CARD_BUDGET_BYTES - 600, element_limit=CARD_ELEMENT_LIMIT - 2)
        return card_fits(candidate)

    if fits(card, first=True):
        return [card]

    def page(elements, *, first=False):
        shell = {**card, "body": {**card["body"], "elements": _enforce_markdown_table_budget(elements)}}
        if not first:
            shell.pop("header", None)
            shell["config"] = {**shell.get("config", {})}
            shell["config"].pop("summary", None)
        return shell

    def components(element, *, first=False):
        if fits(page([element], first=first), first=first):
            yield element
        elif element.get("tag") == "markdown":
            for piece in _split_large_markdown(element):
                if fits(page([piece], first=first), first=first):
                    yield piece
                else:
                    # A pathological code fence or formatting token can dwarf
                    # a page by itself. Keep the characters as literal text.
                    variants = piece.get("i18n_content", {})
                    budget = 12_000 // (len(variants) + 1)
                    default = split_plain_text(piece["content"], byte_limit=budget)
                    localized = {
                        locale: split_plain_text(text, byte_limit=budget) for locale, text in variants.items()
                    }
                    for index in range(max(len(default), *(len(parts) for parts in localized.values()), 0)):
                        text = {"tag": "plain_text", "content": default[index] if index < len(default) else " "}
                        if localized:
                            text["i18n_content"] = {
                                locale: parts[index] if index < len(parts) else " " for locale, parts in localized.items()
                            }
                        yield {"tag": "div", "text": text}
        elif element.get("tag") == "collapsible_panel":
            title = element.get("header", {}).get("title")
            if title and title.get("content"):
                yield from components({"tag": "markdown", "content": title["content"]}, first=first)
            for child in element.get("elements", []):
                yield from components(child, first=first)
        elif element.get("tag") == "div" and element.get("text", {}).get("content"):
            text = element["text"]
            yield from components({"tag": "markdown", **{
                key: text[key] for key in ("content", "i18n_content", "text_size") if key in text
            }}, first=first)
        else:
            raise ValueError("A card component cannot fit on an otherwise empty page")

    pages: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for source in elements:
        for element in components(source, first=not pages and not current):
            if current and not fits(page([*current, element], first=not pages), first=not pages):
                pages.append(page(current, first=not pages))
                current = []
            current.append(element)
    if current or not pages:
        pages.append(page(current, first=not pages))
    return pages


def build_interactive_card_v2(
    *,
    elements: list[dict[str, Any]],
    summary: str,
    text_sizes: Mapping[str, Any],
) -> dict[str, Any]:
    """为普通 interactive 消息接口构建完整 Card JSON。

    飞书目前只在此传输方式中应用设备差异字号别名。CardKit 实体创建/更新接口
    虽然接受相同 JSON，但渲染 Markdown 元素时会忽略这些别名。
    """
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "update_multi": True,
            "locales": _LOCALES,
            "summary": _build_summary(summary),
        },
        "body": {"elements": elements},
    }
    mapped_styles = build_card_text_size_style(text_sizes)
    if mapped_styles:
        card["config"]["style"] = {"text_size": mapped_styles}
    card = _enforce_card_element_limit(card, text_sizes=text_sizes)
    card["body"]["elements"] = _enforce_markdown_table_budget(card["body"]["elements"])
    return card
