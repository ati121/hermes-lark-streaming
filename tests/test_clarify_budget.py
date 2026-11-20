"""Clarify cards stay sendable when prompts or choice lists are very large."""

import pytest
from hermes_lark_streaming.cardkit.budget import card_fits, count_elements, json_size
from hermes_lark_streaming.cardkit.special import (
    build_clarify_card,
    build_clarify_confirmed_card,
    build_clarify_expired_card,
    build_clarify_submitted_card,
)


@pytest.mark.parametrize("value", ["a", "汉", "🚀", "\\`*[]<>\n\""])
def test_pending_large_choices_fit_without_changing_option_indices(value):
    choices = [f"item-{index}: {value * 80}" for index in range(1_000)]
    card = build_clarify_card(question=value * 12_000, choices=choices, clarify_id="prompt")

    assert card_fits(card)
    assert json_size(card) <= 24_000
    assert count_elements(card) <= 200
    elements = card["body"]["elements"]
    select = next(element for element in elements if element["tag"] == "select_static")
    assert 3 < len(select["options"]) < len(choices)
    assert [option["value"] for option in select["options"]] == [
        str(index) for index in range(len(select["options"]))
    ]
    assert "item-0" in select["options"][0]["text"]["content"]
    assert any("choices" in element.get("text", {}).get("content", "") for element in elements)
    assert elements[-1]["tag"] == "input"
    assert "…" in elements[0]["text"]["content"]


@pytest.mark.parametrize("value", ["a", "汉", "🚀", "\\`*[]<>\n\""])
@pytest.mark.parametrize("builder", [build_clarify_submitted_card, build_clarify_confirmed_card])
def test_large_result_cards_fit_and_mark_truncation(builder, value):
    card = builder(question=value * 12_000, selected=value * 12_000)

    assert card_fits(card)
    assert "…" in card["body"]["elements"][0]["text"]["content"]
    assert "…" in card["body"]["elements"][1]["text"]["content"]


def test_expired_large_prompt_fits():
    assert card_fits(build_clarify_expired_card(question="🚀[]\\\n" * 12_000))


def test_small_choices_and_question_are_unchanged():
    card = build_clarify_card(question="Pick one", choices=["First", "Second"])
    elements = card["body"]["elements"]
    assert elements[0]["text"]["content"] == "**Pick one**"
    buttons = next(element for element in elements if element["tag"] == "column_set")["columns"]
    assert [column["elements"][0]["behaviors"][0]["value"]["choice_index"] for column in buttons] == ["0", "1"]
    assert all("size" not in element.get("icon", {}) for element in elements)


@pytest.mark.parametrize("field", ["clarify_id", "callback_token"])
def test_oversized_callback_metadata_raises_for_host_fallback(field):
    with pytest.raises(ValueError, match="delivery budget"):
        build_clarify_card(question="Q", choices=["A"], **{field: "x" * 30_000})


def test_submitted_oversized_retry_identifier_raises():
    with pytest.raises(ValueError, match="delivery budget"):
        build_clarify_submitted_card(question="Q", selected="A", clarify_id="x" * 30_000)
