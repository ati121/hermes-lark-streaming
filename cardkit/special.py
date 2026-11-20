"""CardKit v2.0 — Specialized card types: cron, gateway, clarify."""

from __future__ import annotations

import ast
from typing import Any

from .budget import card_fits, split_plain_text, text_prefix
from .cards import _build_summary
from .elements import _escape_md
from .i18n import _LOCALES, _T, _i18n
from .md import (
    _MAX_CRON_TABLES,
    _downgrade_tables,
    _enforce_markdown_table_budget,
    _normalize_list_markers,
    _split_long_text,
    escape_markdown_asterisks,
    optimize_markdown_style,
    split_answer_spacing_blocks,
    split_markdown_source,
)

__all__ = [
    'build_clarify_card',
    'build_clarify_confirmed_card',
    'build_clarify_expired_card',
    'build_clarify_submitted_card',
    'build_cron_card',
    'build_cron_cards',
    'build_gateway_card',
    'build_gateway_cards',
    'clarify_choice_pairs',
    'normalize_clarify_choices',
]

_CLARIFY_DICT_FIELD_PRIORITY = (
    "label", "description", "text", "title",
    "name", "path", "value", "id",
)

_CLARIFY_MAX_CHOICE_LEN = 80
# A small list is easier to answer with one tap; cap it at three so buttons fit
# comfortably on narrow screens. Keep the dropdown for longer lists.
_CLARIFY_DIRECT_CHOICE_LIMIT = 3
_GATEWAY_STATUS_ELEMENT_ID = "hls_gateway_status"

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

def clarify_choice_pairs(choices: list[str] | None) -> list[tuple[str, str]]:
    """Keep the displayed option and exact answer aligned when dropping blank options."""
    return [(label, str(choice)) for choice in choices or [] if (label := _normalize_choice(choice))]


def normalize_clarify_choices(choices: list[str] | None) -> list[str]:
    """Normalize choice labels for display."""
    return [label for label, _ in clarify_choice_pairs(choices)]

def _static_markdown_chunks(content: str) -> list[str]:
    """Normalised, fence/table-safe markdown chunks for a static card.

    The asterisk escape runs per chunk, after splitting, so every chunk it
    sees has balanced fences.
    """
    normalized = _normalize_list_markers(
        _downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES),
    )
    return [escape_markdown_asterisks(chunk) for chunk in _split_long_text(normalized) if chunk.strip()]


def _static_markdown_elements(content: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for chunk in _static_markdown_chunks(content):
        for index, block in enumerate(split_answer_spacing_blocks(chunk)):
            element: dict[str, Any] = {"tag": "markdown", "content": block}
            if index:
                element["margin"] = "16px 0px 0px 0px"
            elements.append(element)
    return elements


def _static_card_shell(elements: list[dict], *, streaming_mode: bool | None = None) -> dict[str, Any]:
    """schema-2.0 static card shell. streaming_mode=None keeps the key absent
    (cron/gateway cards omit it; clarify cards pin it to False)."""
    config: dict[str, Any] = {"locales": _LOCALES, "update_multi": True}
    if streaming_mode is not None:
        config["streaming_mode"] = streaming_mode
    return {
        "schema": "2.0",
        "config": config,
        "body": {"elements": _enforce_markdown_table_budget(elements)},
    }


def _icon_div(token: str, color: str, text: dict[str, Any]) -> dict:
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": token,
            "color": color,
        },
        "text": text,
    }


def build_cron_card(content: str) -> dict[str, Any]:
    """Cron 推送用的极简静态卡片 — schema 2.0，仅 markdown 内容."""
    if not content.strip():
        return _static_card_shell([])
    elements = _static_markdown_elements(content)
    card = _static_card_shell(elements)
    summary = _build_summary(content)
    if summary["content"]:
        card["config"]["summary"] = summary
    return card


def _static_card_pages(content: str, whole: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if card_fits(whole):
        return [(content, whole)]

    # Each fragment carries its exact source slice so a failed continuation
    # can fall back without repeating any page already sent.
    def page(elements, *, first: bool):
        card = _static_card_shell(elements)
        if first and "summary" in whole["config"]:
            card["config"]["summary"] = whole["config"]["summary"]
        return card

    def fragments():
        for source, fragment in split_markdown_source(content):
            elements = _static_markdown_elements(fragment)
            if card_fits(page(elements, first=False)):
                yield source, elements
                continue
            # An unusually dense fragment can still exceed the card budget
            # after Markdown spacing. Keep its source visible in plain text.
            for part in split_plain_text(source):
                yield part, [{"tag": "div", "text": {"tag": "plain_text", "content": part}}]

    pages: list[tuple[str, dict[str, Any]]] = []
    current = [element for element in whole["body"]["elements"] if element["tag"] != "markdown"]
    source_parts: list[str] = []
    for source, elements in fragments():
        candidate = page([*current, *elements], first=not pages)
        if current and not card_fits(candidate):
            pages.append(("".join(source_parts), page(current, first=not pages)))
            source_parts = []
            current = []
        current.extend(elements)
        source_parts.append(source)
    if source_parts or not pages:
        pages.append(("".join(source_parts), page(current, first=not pages)))
    return pages


def build_cron_cards(content: str) -> list[tuple[str, dict[str, Any]]]:
    return _static_card_pages(content, build_cron_card(content))


def build_gateway_card(content: str, *, category: str = "", status_label: str = "", status_emoji: str = "") -> dict[str, Any]:
    """Gateway-internal message card — lightweight, static, no streaming. For slash
    command replies, auth, session, errors. category retained for reaction routing."""
    elements: list[dict] = []

    if status_label and status_emoji:
        elements.append({
            "tag": "div",
            "element_id": _GATEWAY_STATUS_ELEMENT_ID,
            "text": {
                "tag": "plain_text",
                "content": text_prefix(f"{status_emoji} {status_label}", byte_limit=1_000),
                "text_color": "turquoise",
                "text_size": "notation",
            },
        })

    if content.strip():
        elements.extend(_static_markdown_elements(content))

    card = _static_card_shell(elements)

    summary = _build_summary(content)
    if summary["content"]:
        card["config"]["summary"] = summary

    return card


def build_gateway_cards(
    content: str, *, category: str = "", status_label: str = "", status_emoji: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    return _static_card_pages(content, build_gateway_card(
        content, category=category, status_label=status_label, status_emoji=status_emoji,
    ))

def _clarify_display_text(text: str) -> str:
    """Leave space for Markdown escaping and repeated locale variants."""
    prefix = text_prefix(text, byte_limit=2_000)
    return prefix.rstrip() + "…" if prefix != text else text


def _checked_clarify_card(card: dict[str, Any]) -> dict[str, Any]:
    if not card_fits(card):
        # The adapter can use the host prompt if callback metadata itself is
        # too large. Never truncate IDs or tokens and change their meaning.
        raise ValueError("Clarify card exceeds the card delivery budget")
    return card


def _build_clarify_card(
    *, question: str, choices: list[str] | None = None, clarify_id: str = "", callback_token: str = "",
) -> dict[str, Any]:
    """构建 Clarify 待选择态卡片 (State 1: Pending): 标题、选项、快捷选择或下拉、自定义输入."""
    callback_value = {"clarify_id": clarify_id}
    if callback_token:
        callback_value["clarify_token"] = callback_token
    elements: list[dict] = [
        _icon_div("info_outlined", "blue", {
            "tag": "lark_md",
            "content": f"**{_escape_md(question)}**",
        }),
    ]

    normalized_choices = choices or []

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

        if len(normalized_choices) <= _CLARIFY_DIRECT_CHOICE_LIMIT:
            # Keep the full choice text above and use compact buttons to avoid
            # repeating long labels or crowding the card on mobile.
            en_choose, zh_choose = _T["clarify_choose"]
            columns = []
            for i, label in enumerate(labels):
                en_label, zh_label = en_choose.format(label), zh_choose.format(label)
                columns.append({
                    "tag": "column",
                    "width": "auto",
                    "elements": [{
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": en_label,
                            "i18n_content": _i18n(en_label, zh_label),
                        },
                        "type": "default",
                        "size": "small",
                        "behaviors": [{
                            "type": "callback",
                            "value": {
                                **callback_value,
                                "hermes_clarify_action": "choice",
                                "choice_index": str(i),
                            },
                        }],
                    }],
                })
            elements.append({
                "tag": "column_set",
                "element_id": "clarify_choices",
                "flex_mode": "flow",
                "horizontal_spacing": "8px",
                "columns": columns,
            })
        else:
            # Long lists stay in a compact dropdown. Its callback is retained
            # for cards created before this direct-button layout was introduced.
            options: list[dict] = [
                {
                    "text": {"tag": "plain_text", "content": f"{label}. {choice}"},
                    "value": str(i),
                }
                for i, (label, choice) in enumerate(zip(labels, normalized_choices))
            ]

            en_placeholder, zh_placeholder = _T["clarify_select_placeholder"]
            elements.append({
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
                    "value": {**callback_value, "hermes_clarify_action": "select"},
                }],
            })

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
            "value": {**callback_value, "hermes_clarify_action": "input_submit"},
        }],
    }
    elements.append(input_el)

    return _static_card_shell(elements, streaming_mode=False)


def build_clarify_card(
    *, question: str, choices: list[str] | None = None, clarify_id: str = "", callback_token: str = "",
) -> dict[str, Any]:
    """Render the largest safe prefix of choices, preserving callback indices."""
    question = _clarify_display_text(question)
    normalized = normalize_clarify_choices(choices)

    def candidate(count: int) -> dict[str, Any]:
        card = _build_clarify_card(
            question=question, choices=normalized[:count],
            clarify_id=clarify_id, callback_token=callback_token,
        )
        if count < len(normalized):
            en = f"Showing {count} of {len(normalized)} choices. You can type another answer below."
            zh = f"已显示 {count}/{len(normalized)} 个选项，也可在下方输入其他答案。"
            card["body"]["elements"].insert(-1, {
                "tag": "div",
                "text": {"tag": "plain_text", "content": en, "i18n_content": _i18n(en, zh)},
            })
        return card

    # A dropdown option contributes a tagged text object. Capping the search
    # also avoids constructing an arbitrarily large payload just to reject it.
    lower, upper = 0, min(len(normalized), 200)
    best = _checked_clarify_card(candidate(0))
    while lower <= upper:
        count = (lower + upper) // 2
        card = candidate(count)
        if card_fits(card):
            best, lower = card, count + 1
        else:
            upper = count - 1
    return best


def _clarify_header_elements(question: str, selected: str, *, token: str, color: str) -> list[dict]:
    """Shared two-row header of the submitted/confirmed clarify cards."""
    safe_selected = _escape_md(_clarify_display_text(selected))
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(safe_selected)
    zh_sel_label = zh_selected.format(safe_selected)
    return [
        _icon_div(token, color, {
            "tag": "lark_md",
            "content": f"**{_escape_md(_clarify_display_text(question))}**",
        }),
        _icon_div(token, color, {
            "tag": "lark_md",
            "content": en_sel_label,
            "i18n_content": _i18n(en_sel_label, zh_sel_label),
        }),
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
        },
    ]

    return _checked_clarify_card(_static_card_shell(elements, streaming_mode=False))

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

    return _checked_clarify_card(_static_card_shell(elements, streaming_mode=False))


def build_clarify_expired_card(*, question: str) -> dict[str, Any]:
    """Retire controls when Hermes no longer has a waiter for this prompt."""
    en = "This question is no longer accepting answers."
    zh = "这条追问已停止接收回答。"
    return _checked_clarify_card(_static_card_shell([
        _icon_div("info_outlined", "grey", {
            "tag": "lark_md", "content": f"**{_escape_md(_clarify_display_text(question))}**",
        }),
        {"tag": "div", "text": {"tag": "plain_text", "content": en, "i18n_content": _i18n(en, zh)}},
    ], streaming_mode=False))
