"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

__all__ = [
    'build_streaming_card_v2',
    'build_interactive_card_v2',
    '_enforce_card_element_limit',
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
            "update_multi": True,
            "locales": _LOCALES,
            "summary": _build_summary(summary),
        },
        "body": {"elements": elements},
    }
    mapped_styles = build_card_text_size_style(text_sizes)
    if mapped_styles:
        card["config"]["style"] = {"text_size": mapped_styles}
    return _enforce_card_element_limit(card, text_sizes=text_sizes)
