"""Card-wide table limits apply before streaming, sealing and static fallback."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.cardkit import (
    build_answer_elements,
    build_cron_card,
    build_cron_cards,
    build_gateway_card,
    build_gateway_cards,
    build_interactive_card_v2,
    card_visible_text,
)
from hermes_lark_streaming.cardkit.budget import (
    CARD_ELEMENT_LIMIT,
    CARD_HARD_LIMIT_BYTES,
    card_fits,
    count_elements,
    json_size,
)
from hermes_lark_streaming.cardkit.md import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    _open_fence_at_end,
    render_answer_markdown,
)
from hermes_lark_streaming.controller import (
    CardSession,
    StreamCardController,
    linear_mixin,
)
from hermes_lark_streaming.controller.mixin import (
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    STREAMING,
)
from hermes_lark_streaming.feishu import (
    IM_FREQUENCY_LIMIT,
    FeishuAPIError,
    FeishuClient,
    MessageDeliveryUncertain,
)
from hermes_lark_streaming.feishu.client import CardKitAmbiguousWrite
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import TerminalReason


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


@pytest.mark.parametrize("count", [0, 1, 4])
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
    assert table_count(card) == 4
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


@pytest.mark.parametrize("builder", [build_cron_cards, build_gateway_cards])
def test_static_pagination_preserves_source_rows_and_valid_fences(builder):
    code = "```python\n" + "打印(记录)\n" * 950 + "```\n\n"
    rows = [f"{i} | value-{i:03d} " + "中" * 75 for i in range(180)]
    text = code + "Index | Content\n---|---\n" + "\n".join(rows) + "\n"
    pages = builder(text)

    assert len(pages) > 2
    assert "".join(source for source, _ in pages) == text
    visible = "\n".join(card_visible_text(card) for _, card in pages)
    assert visible.count("打印(记录)") == 950
    assert all(row in visible for row in rows)
    for _, card in pages:
        assert card_fits(card)
        assert table_count(card) <= 5
        assert all(not _open_fence_at_end(node["content"]) for node in markdown_nodes(card))


def test_static_pagination_checks_component_count_under_the_byte_limit():
    text = "- a\n\nb\n\n" * 950
    card = build_cron_card(text)
    assert json_size(card) < CARD_HARD_LIMIT_BYTES
    assert count_elements(card) > CARD_ELEMENT_LIMIT
    pages = build_cron_cards(text)

    assert len(pages) > 1
    assert "".join(source for source, _ in pages) == text
    assert all(card_fits(card) for _, card in pages)


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


@pytest.mark.asyncio
async def test_native_streaming_component_keeps_four_tables(pipeline):
    ctrl, session = pipeline
    session.interactive_mode = False
    session.card_id = "native-card"

    await ctrl._do_unified_flush(session)

    content = ctrl._client.cardkit_stream_element.await_args.args[2]
    assert len(_find_tables_outside_code_blocks(content)) == 4
    assert all(f"change-{index}" in content for index in range(6))


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_long_multibyte_completion_keeps_the_full_answer(pipeline, native):
    ctrl, session = pipeline
    session.interactive_mode = not native
    session.card_id = "native-card" if native else session.card_id
    session.state = COMPLETING
    text = "龘" * 5_000 + "麤" * 5_000 + "靐" * 5_000
    session.unified_state = UnifiedLinearState()
    session.unified_state.on_answer_delta(text)

    await ctrl._do_linear_complete_with_fallback(session)

    if native:
        ctrl._client.cardkit_close_streaming.assert_awaited_once()
        ctrl._client.cardkit_batch_update.assert_not_awaited()
        first = ctrl._client.cardkit_update.await_args.args[1]
        assert first["config"]["streaming_mode"] is False
    else:
        first = ctrl._client.update_card.await_args.args[1]
    pages = [first, *(call.args[1] for call in ctrl._client.reply_card.await_args_list)]
    assert len(pages) > 1
    assert all(card_fits(card) for card in pages)
    assert "".join(char for card in pages for char in card_visible_text(card) if char in "龘麤靐") == text
    assert session.state == COMPLETED
    ctrl._client.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text_succeeds", [True, False])
async def test_failed_continuation_falls_back_for_that_page_only(pipeline, text_succeeds):
    ctrl, session = pipeline
    session.state = COMPLETING
    session.unified_state.on_answer_delta("龘" * 28_000)
    ctrl._client.reply_card.side_effect = [FeishuAPIError("rejected"), "second-page", "third-page", "fourth-page"]
    if not text_succeeds:
        ctrl._client.reply_text.side_effect = FeishuAPIError("text rejected")

    await ctrl._do_linear_complete_with_fallback(session)

    assert ctrl._client.reply_card.await_count >= 3
    assert ctrl._client.update_card.await_count == 1
    failed_page = ctrl._client.reply_card.await_args_list[0].args[1]
    attempted_text = "".join(call.args[1] for call in ctrl._client.reply_text.await_args_list)
    assert attempted_text and card_visible_text(failed_page).startswith(attempted_text)
    if text_succeeds:
        assert attempted_text == card_visible_text(failed_page)
        assert session.state == COMPLETED
    else:
        assert session.state == CREATION_FAILED
        assert session.terminal_reason == TerminalReason.DELIVERY_UNCONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_confirmed", [True, False])
async def test_ambiguous_native_seal_never_replays_an_insert(pipeline, replacement_confirmed):
    ctrl, session = pipeline
    session.interactive_mode = False
    session.card_id = "native-card"
    session.state = COMPLETING
    ctrl._client.cardkit_batch_update.side_effect = CardKitAmbiguousWrite("batch may have landed")
    ctrl._client.cardkit_update.side_effect = [CardKitAmbiguousWrite("lost response"), None] if replacement_confirmed else [
        CardKitAmbiguousWrite("lost response"), FeishuAPIError("latest attempt rejected"),
    ]

    await ctrl._do_linear_complete_with_fallback(session)

    ctrl._client.cardkit_batch_update.assert_awaited_once()
    assert ctrl._client.cardkit_update.await_count == 2
    ctrl._client.reply_card.assert_not_awaited()
    ctrl._client.reply_text.assert_not_awaited()
    if replacement_confirmed:
        assert session.state == COMPLETED
    else:
        assert session.state == CREATION_FAILED
        assert session.terminal_reason == TerminalReason.DELIVERY_UNCONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["first_patch_uncertain", "repair_uncertain", "repair_confirmed"])
async def test_uncertain_im_seal_delivers_continuations_without_duplicate_first_page(pipeline, monkeypatch, outcome):
    ctrl, session = pipeline
    monkeypatch.setattr(linear_mixin, "_INTERACTIVE_RECOVERY_DELAYS", (0, 0))
    session.state = COMPLETING
    text = "首张卡片标记\n\n" + "龘" * 19_000
    session.unified_state = UnifiedLinearState()
    session.unified_state.on_answer_delta(text)
    uncertain = MessageDeliveryUncertain("PATCH might have been applied")
    rejected = FeishuAPIError("last PATCH rejected")
    if outcome == "repair_uncertain":
        limited = FeishuAPIError("frequency limit", code=IM_FREQUENCY_LIMIT)
        attempts = [limited, uncertain, rejected]
    elif outcome == "repair_confirmed":
        attempts = [uncertain, None]
    else:
        attempts = [uncertain, rejected]
    ctrl._client.update_card.side_effect = attempts

    await ctrl._do_linear_complete_with_fallback(session)
    ctrl._client.reply_card.assert_not_awaited()
    task = session.seal_task
    assert task is not None
    await task

    assert ctrl._client.update_card.await_count == len(attempts)
    first_page = card_visible_text(ctrl._client.update_card.await_args_list[0].args[1])
    continuations = [card_visible_text(call.args[1]) for call in ctrl._client.reply_card.await_args_list]
    assert len(continuations) >= 2
    assert "首张卡片标记" in first_page
    assert all("首张卡片标记" not in page for page in continuations)
    assert sum(page.count("龘") for page in [first_page, *continuations]) == 19_000
    ctrl._client.reply_text.assert_not_awaited()
    if outcome == "repair_confirmed":
        assert session.state == COMPLETED
    else:
        assert session.state == CREATION_FAILED
        assert session.terminal_reason == TerminalReason.DELIVERY_UNCONFIRMED


@pytest.mark.asyncio
async def test_reasoning_toggle_uses_remaining_space_without_changing_answer(pipeline):
    ctrl, _ = pipeline
    content = [{"tag": "markdown", "content": "龘" * 7_200}]
    card = build_interactive_card_v2(elements=content, summary="Answer", text_sizes={})
    snapshot = {"full": "麤" * 5_000, "card": card, "card_msg_id": "original-card", "text_sizes": {}}

    assert await ctrl._apply_reasoning_snapshot(snapshot, True)

    updated = ctrl._client.update_card.await_args.args[1]
    assert card_fits(updated)
    assert updated["body"]["elements"][-1:] == content
    reasoning = updated["body"]["elements"][1]["text"]["content"]
    assert reasoning.startswith("麤") and reasoning.endswith("…")
    assert len(reasoning) < 5_000
