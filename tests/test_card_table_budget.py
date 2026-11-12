"""Card-wide table limits apply before streaming, sealing and static fallback."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.cardkit import (
    build_answer_elements,
    build_cron_card,
    build_gateway_card,
    build_interactive_card_v2,
)
from hermes_lark_streaming.cardkit.md import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    render_answer_markdown,
)
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import COMPLETED, COMPLETING, STREAMING
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.state.linear import UnifiedLinearState


def table(index):
    return f"| Version | Change |\n|---|---|\n| {index} | change-{index} |"


def answer(count=6):
    return "\n\n".join(f"## Section {i}\n\n{table(i)}" for i in range(count))


def markdown_nodes(value):
    if isinstance(value, dict):
        if value.get("tag") in {"markdown", "lark_md"}:
            yield value
        for child in value.values():
            yield from markdown_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from markdown_nodes(child)


def table_count(card, locale=None):
    return sum(len(_find_tables_outside_code_blocks(
        node.get("i18n_content", {}).get(locale, node.get("content", "")),
    )) for node in markdown_nodes(card))


@pytest.mark.parametrize("final", [False, True])
def test_interactive_answer_six_tables_preserves_content_with_five_rendered(final):
    source = build_answer_elements(render_answer_markdown(answer(), final=final))
    original = deepcopy(source)
    card = build_interactive_card_v2(elements=source, summary="Answer", text_sizes={"body": {"mobile": "large"}})
    assert table_count(card) == 5
    serialized = json.dumps(card)
    for index in range(6):
        assert f"change-{index}" in serialized
    assert "```" in serialized
    assert "hls_body" in serialized
    assert source == original


def test_budget_shared_across_answer_and_nested_review_markdown():
    card = build_interactive_card_v2(
        elements=[{"tag": "markdown", "content": table(i)} for i in range(4)] + [{
            "tag": "collapsible_panel", "elements": [
                {"tag": "markdown", "content": table(4)},
                {"tag": "div", "text": {"tag": "lark_md", "content": table(5)}},
            ],
        }], summary="Answer", text_sizes={},
    )
    assert table_count(card) == 5
    assert "change-5" in str(card)


def test_locale_variants_have_independent_budgets_and_inherit_default_content():
    card = build_interactive_card_v2(elements=[
        {"tag": "markdown", "content": answer(4), "i18n_content": {"zh_cn": table(9)}},
        {"tag": "markdown", "content": answer(3)},
        {"tag": "markdown", "content": table(20), "i18n_content": {"en_us": answer(7)}},
    ], summary="Answer", text_sizes={})
    assert table_count(card) == 5
    assert table_count(card, "en_us") == 5
    assert table_count(card, "zh_cn") == 5


@pytest.mark.parametrize("count", [0, 1, 5])
def test_cards_within_budget_are_unchanged(count):
    elements = [{"tag": "markdown", "content": answer(count)}]
    card = build_interactive_card_v2(elements=elements, summary="Answer", text_sizes={})
    assert card["body"]["elements"] == elements


@pytest.mark.parametrize("fence", ["```", "~~~~", "````"])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_table_detection_respects_fences_and_crlf(fence, ending):
    text = f"{fence}text\n{table(0)}\n{fence}\n\n{table(1)}".replace("\n", ending)
    assert len(_find_tables_outside_code_blocks(text)) == 1
    result = _downgrade_tables(text, limit=0)
    assert f"{fence}text{ending}{table(0).replace(chr(10), ending)}{ending}{fence}" in result
    assert not _find_tables_outside_code_blocks(result)


def test_short_and_missing_outer_pipe_tables_count_toward_budget():
    text = "\n\n".join(f"A | B\n--|--\n{i} | value-{i}" for i in range(6))
    assert len(_find_tables_outside_code_blocks(text)) == 6
    card = build_interactive_card_v2(elements=[{"tag": "markdown", "content": text}], summary="A", text_sizes={})
    assert table_count(card) == 5
    assert "value-5" in str(card)


def test_overflow_table_with_backtick_cell_gets_a_safe_fence():
    text = "| A | B |\n|---|---|\n| literal ``` | value |"
    result = _downgrade_tables(text, limit=0)
    assert result.startswith("````\n")
    assert result.endswith("\n````")
    assert not _find_tables_outside_code_blocks(result)


def test_unchanged_localized_plain_text_does_not_add_fallback_variants():
    source = [{"tag": "markdown", "content": "Completed", "i18n_content": {"zh_cn": "完成"}}]
    card = build_interactive_card_v2(elements=source, summary="A", text_sizes={})
    assert card["body"]["elements"] == source


@pytest.mark.parametrize("builder", [build_cron_card, build_gateway_card])
def test_chunking_one_long_table_cannot_exceed_card_table_budget(builder):
    text = "| Index | Content |\n|---|---|\n" + "\n".join(f"| {i} | {'x' * 80} |" for i in range(180))
    card = builder(text)
    assert len(card["body"]["elements"]) > 5
    assert table_count(card) == 5
    for i in range(180):
        assert f"| {i} |" in str(card)


@pytest.fixture
async def pipeline(request):
    ctrl = StreamCardController()
    ctrl._cfg._raw = {"hermes_lark_streaming": {"enabled": True},
                      "feishu": {"app_id": "app", "app_secret": "secret"}}
    ctrl._initialized = True
    ctrl._client = AsyncMock(spec=FeishuClient)
    session = CardSession(request.node.name, "test-chat", asyncio.get_running_loop())
    session.linear = True
    session.interactive_mode = True
    session.unified_state = UnifiedLinearState()
    session.unified_state.on_answer_delta(answer())
    session.text_sizes = {"body": {"mobile": "large"}}
    session.card_msg_id = "original-card"
    session.card_id = "im:original-card"
    session.state = STREAMING
    session._card_ready.set()
    session.flush.set_card_message_ready(True)
    ctrl._sess_put(session.message_id, session)
    yield ctrl, session
    ctrl._sess_clear()


@pytest.mark.asyncio
async def test_streaming_and_seal_keep_original_card_with_six_tables(pipeline):
    ctrl, session = pipeline

    async def reject_overflow(message_id, card, **kwargs):
        if table_count(card) > 5:
            raise FeishuAPIError("ErrCode: 11310; card table number over limit", code=230099)

    ctrl._client.update_card.side_effect = reject_overflow
    await ctrl._do_unified_flush(session)
    assert table_count(ctrl._client.update_card.await_args.args[1]) == 5
    assert session.unified_state.answer_dirty is False
    session.state = COMPLETING
    await ctrl._do_linear_complete_with_fallback(session)
    assert session.state == COMPLETED
    assert ctrl._client.update_card.await_count == 2
    assert all(call.args[0] == "original-card" for call in ctrl._client.update_card.await_args_list)
    ctrl._client.reply_card.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_fallback_also_obeys_shared_table_budget(pipeline):
    ctrl, session = pipeline
    ctrl._client.update_card.side_effect = FeishuAPIError("other failure", code=999999)
    session.state = COMPLETING
    await ctrl._do_linear_complete_with_fallback(session)
    card = ctrl._client.reply_card.await_args.args[1]
    assert table_count(card) == 5
    for index in range(6):
        assert f"change-{index}" in str(card)
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_reasoning_toggle_with_table_content_keeps_whole_card_in_budget(pipeline):
    ctrl, session = pipeline
    card = ctrl._build_interactive_linear_card(session, final=True)
    snapshot = {"full": table(99), "card": card, "card_msg_id": "original-card", "text_sizes": session.text_sizes}
    assert await ctrl._apply_reasoning_snapshot(snapshot, True)
    updated = ctrl._client.update_card.await_args.args[1]
    assert table_count(updated) == 5
    assert "change-99" in str(updated)
    for index in range(6):
        assert f"change-{index}" in str(updated)
