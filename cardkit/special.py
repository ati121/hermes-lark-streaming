"""CardKit v2.0 — Specialized card types: cron, gateway, clarify."""

from __future__ import annotations

import ast
from typing import Any

from .i18n import _LOCALES, _T, _i18n
from .elements import _escape_md
from .md import (
    _MAX_CRON_TABLES,
    _downgrade_tables,
    _split_long_text,
    optimize_markdown_style,
)

__all__ = [
    'build_cron_card',
    'build_gateway_card',
    'build_clarify_card',
    'build_clarify_submitted_card',
    'build_clarify_confirmed_card',
    'normalize_clarify_choices',
]

_CLARIFY_DICT_FIELD_PRIORITY = (
    "label", "description", "text", "title",
    "name", "path", "value", "id",
)

_CLARIFY_MAX_CHOICE_LEN = 80

def _normalize_choice(choice: Any) -> str:
    """Normalize clarify choice to readable string. Handles: plain string,
    dict-repr string (parsed via ast.literal_eval), real dict. Never raises."""
    if choice is None:
        return ""
    if not isinstance(choice, str):
        if isinstance(choice, dict):
            return _extract_readable_from_dict(choice)
        if isinstance(choice, (list, tuple)):
            parts = [_normalize_choice(x) for x in choice]
            return " ".join(p for p in parts if p)[:_CLARIFY_MAX_CHOICE_LEN]
        choice = str(choice)

    text = choice.strip()
    if not text:
        return ""

    # Parse dict-repr strings: starts with { ends with }.
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            extracted = _extract_readable_from_dict(parsed)
            if extracted:
                text = extracted

    if len(text) > _CLARIFY_MAX_CHOICE_LEN:
        text = text[: _CLARIFY_MAX_CHOICE_LEN - 1] + "…"

    return text

def _extract_readable_from_dict(d: dict) -> str:
    """Extract readable string field from dict (priority order, strings only)."""
    for field in _CLARIFY_DICT_FIELD_PRIORITY:
        val = d.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def normalize_clarify_choices(choices: list[str] | None) -> list[str]:
    """Normalize choices for display + AI resolution. Filters empty."""
    if not choices:
        return []
    normalized = []
    for c in choices:
        n = _normalize_choice(c)
        if n:
            normalized.append(n)
    return normalized

def _static_summary(content: str) -> str:
    """One-line card summary — strips fences so the preview can't break."""
    return content[:120].replace("\n", " ").replace("```", "").strip()


def _static_card_shell(elements: list[dict], *, streaming_mode: bool | None = None) -> dict[str, Any]:
    """schema-2.0 static card shell. streaming_mode=None keeps the key absent
    (cron/gateway cards omit it; clarify cards pin it to False)."""
    config: dict[str, Any] = {"locales": _LOCALES}
    if streaming_mode is not None:
        config["streaming_mode"] = streaming_mode
    return {
        "schema": "2.0",
        "config": config,
        "body": {"elements": elements},
    }


def _icon_div(token: str, color: str, text: dict[str, Any], *, size: str = "20px 20px") -> dict:
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": token,
            "size": size,
            "color": color,
        },
        "text": text,
    }


def build_cron_card(content: str) -> dict[str, Any]:
    """Cron 推送用的极简静态卡片 — schema 2.0，仅 markdown 内容."""
    if not content.strip():
        return _static_card_shell([])
    elements = [
        {"tag": "markdown", "content": chunk}
        for chunk in _split_long_text(_downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES))
        if chunk.strip()
    ]
    card = _static_card_shell(elements)
    summary = _static_summary(content)
    if summary:
        card["config"]["summary"] = {"content": summary}
    return card

def build_gateway_card(content: str, *, category: str = "", status_label: str = "", status_emoji: str = "") -> dict[str, Any]:
    """Gateway-internal message card — lightweight, static, no streaming. For slash
    command replies, auth, session, errors. category retained for reaction routing."""
    elements: list[dict] = []

    if status_label and status_emoji:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"{status_emoji} {status_label}",
                "text_color": "turquoise",
                "text_size": "notation",
            },
        })

    if content.strip():
        for chunk in _split_long_text(_downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES)):
            if chunk.strip():
                elements.append({"tag": "markdown", "content": chunk})

    card = _static_card_shell(elements)

    summary = _static_summary(content) if content.strip() else ""
    if summary:
        card["config"]["summary"] = {"content": summary}

    return card

def build_clarify_card(*, question: str, choices: list[str] | None = None, clarify_id: str = "") -> dict[str, Any]:
    """构建 Clarify 待选择态卡片 (State 1: Pending). 三态: 标题/选项列表/快速选择下拉/
    自定义输入. choices 经 normalize + escape for lark_md. select_static 用 plain_text."""
    elements: list[dict] = [
        _icon_div("info_outlined", "blue", {
            "tag": "lark_md",
            "content": f"**{_escape_md(question)}**",
        }),
    ]

    # Defense in depth: adapter also normalizes, but card builders must be safe.
    normalized_choices = normalize_clarify_choices(choices)

    if normalized_choices:
        labels = [chr(ord("A") + i) if i < 26 else str(i + 1) for i in range(len(normalized_choices))]
        option_lines = []
        for label, choice in zip(labels, normalized_choices):
            option_lines.append(f"{label}. {_escape_md(choice)}")
        options_md = "\n".join(option_lines)
        elements.append({
            "tag": "markdown",
            "content": options_md,
        })

        # select_static dropdown (plain_text, no markdown).
        options: list[dict] = [
            {
                "text": {"tag": "plain_text", "content": f"{label}. {choice}"},
                "value": str(i),
            }
            for i, (label, choice) in enumerate(zip(labels, normalized_choices))
        ]

        en_placeholder, zh_placeholder = _T["clarify_select_placeholder"]
        select_el: dict[str, Any] = {
            "tag": "select_static",
            "element_id": "clarify_select",
            "placeholder": {
                "tag": "plain_text",
                "content": en_placeholder,
                "i18n_content": _i18n(en_placeholder, zh_placeholder),
            },
            "options": options,
            "behaviors": [{
                "type": "callback",
                "value": {
                    "hermes_clarify_action": "select",
                    "clarify_id": clarify_id,
                },
            }],
        }
        elements.append(select_el)

    en_input_ph, zh_input_ph = _T["clarify_input_placeholder"]
    input_el: dict[str, Any] = {
        "tag": "input",
        "element_id": "clarify_input",
        "placeholder": {
            "tag": "plain_text",
            "content": en_input_ph,
            "i18n_content": _i18n(en_input_ph, zh_input_ph),
        },
        "max_length": 500,
        "name": "clarify_input",
        "behaviors": [{
            "type": "callback",
            "value": {
                "hermes_clarify_action": "input_submit",
                "clarify_id": clarify_id,
            },
        }],
    }
    elements.append(input_el)

    return _static_card_shell(elements, streaming_mode=False)

def _clarify_header_elements(question: str, selected: str, *, token: str, color: str) -> list[dict]:
    """Shared two-row header of the submitted/confirmed clarify cards."""
    safe_selected = _escape_md(selected)
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(safe_selected)
    zh_sel_label = zh_selected.format(safe_selected)
    return [
        _icon_div(token, color, {
            "tag": "lark_md",
            "content": f"**{_escape_md(question)}**",
        }),
        _icon_div(token, color, {
            "tag": "lark_md",
            "content": en_sel_label,
            "i18n_content": _i18n(en_sel_label, zh_sel_label),
        }, size="16px 16px"),
    ]

def build_clarify_submitted_card(*, question: str, selected: str, clarify_id: str = "") -> dict[str, Any]:
    """构建 Clarify 已提交态卡片 (State 2: Submitted/Soft Lock). 标题 + 用户选择 +
    "已提交" 提示 + 重试按钮."""
    en_submitted, zh_submitted = _T["clarify_submitted"]
    en_retry, zh_retry = _T["clarify_retry"]

    elements: list[dict] = [
        *_clarify_header_elements(question, selected, token="lock_outlined", color="orange"),
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"*{en_submitted}*",
                "i18n_content": _i18n(f"*{en_submitted}*", f"*{zh_submitted}*"),
            },
        },
        {
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": en_retry,
                    "i18n_content": _i18n(en_retry, zh_retry),
                },
                "type": "primary",
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "hermes_clarify_action": "retry_submit",
                        "clarify_id": clarify_id,
                    },
                }],
            }],
        },
    ]

    return _static_card_shell(elements, streaming_mode=False)

def build_clarify_confirmed_card(*, question: str, selected: str) -> dict[str, Any]:
    """构建 Clarify 已确认态卡片 (State 3: Confirmed/Hard Lock). 标题 + 选择 + "已确认"."""
    en_confirmed, zh_confirmed = _T["clarify_confirmed"]

    elements: list[dict] = [
        *_clarify_header_elements(question, selected, token="resolve_filled", color="green"),
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": en_confirmed,
                "i18n_content": _i18n(en_confirmed, zh_confirmed),
            },
        },
    ]

    return _static_card_shell(elements, streaming_mode=False)
