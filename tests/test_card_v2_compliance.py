"""Feishu Card JSON 2.0 & CardKit/IM v1 OpenAPI compliance regression tests."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hermes_lark_streaming.aowen import (
    _build_unknown_command_card,
    _handle_config_reload,
    build_help_card,
    build_interrupt_hint_card,
    build_monitor_card,
    build_reset_card,
    build_status_card,
)
from hermes_lark_streaming.cardkit import (
    build_answer_elements,
    build_clarify_card,
    build_clarify_confirmed_card,
    build_clarify_expired_card,
    build_clarify_submitted_card,
    build_cron_card,
    build_gateway_card,
    build_interactive_card_v2,
    build_streaming_card_v2,
    build_unified_panel,
)
from hermes_lark_streaming.cardkit.elements import (
    _build_footer_elements,
    _build_tool_step_output,
    build_pinned_reasoning_elements_from_text,
)
from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.linear_mixin import _is_card_limit_error
from hermes_lark_streaming.controller.mixin import STREAMING
from hermes_lark_streaming.feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_INTERACTION_LOCKED,
    CARDKIT_STREAMING_ADD_TIMEOUT,
    CARDKIT_STREAMING_CLOSED,
    CARDKIT_STREAMING_TIMEOUT,
    CARDKIT_TAG_IMMUTABLE,
    CARDKIT_TRANSIENT_CODES,
    FeishuAPIError,
    FeishuClient,
    FeishuClientConfig,
    is_schema_error,
    is_streaming_closed_error,
    is_terminal_api_code,
)
from hermes_lark_streaming.feishu.client import _is_transient_error
from hermes_lark_streaming.state.linear import UnifiedLinearState

_ELEMENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")


def _audit_card_v2(obj, path: str = "root", seen_ids: set[str] | None = None) -> list[str]:
    """Validate strict Card JSON 2.0 constraints across an entire card tree."""
    findings: list[str] = []
    if seen_ids is None:
        seen_ids = set()

    if isinstance(obj, dict):
        eid = obj.get("element_id")
        if eid is not None:
            if not isinstance(eid, str) or not _ELEMENT_ID_RE.match(eid):
                findings.append(f"invalid element_id at {path}: {eid!r}")
            if eid in seen_ids:
                findings.append(f"duplicate element_id at {path}: {eid!r}")
            seen_ids.add(eid)

        tag = obj.get("tag")
        if tag == "div":
            txt = obj.get("text")
            if isinstance(txt, dict) and txt.get("tag") == "lark_md":
                if "text_color" in txt:
                    findings.append(f"lark_md carries text_color at {path}.text")
                if "```" in (txt.get("content") or ""):
                    findings.append(f"lark_md carries fenced code block at {path}.text")
        elif tag == "column_set":
            flex_mode = obj.get("flex_mode", "none")
            if flex_mode != "none":
                for idx, col in enumerate(obj.get("columns", [])):
                    if "width" in col or "weight" in col:
                        findings.append(
                            f"column width/weight set under flex_mode={flex_mode!r} at {path}.columns[{idx}]"
                        )

        for key, value in obj.items():
            findings.extend(_audit_card_v2(value, f"{path}.{key}", seen_ids))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            findings.extend(_audit_card_v2(item, f"{path}[{idx}]", seen_ids))
    return findings


class TestErrorCodeClassification:
    @pytest.mark.parametrize(
        "sub_code,err_msg",
        [
            (100290, "There is an invalid user resource(at/person) in your card"),
            (200380, "template does not exist"),
            (200381, "template is not visible to app"),
            (200550, "chart spec is invalid"),
            (200570, "card contains invalid image keys"),
            (200621, "parse card json err"),
            (200732, "variable type inconsistent"),
            (200861, "cards of schema V2 no longer support this capability"),
            (200914, "table rows is invalid"),
        ],
    )
    def test_230099_permanent_sub_codes_are_not_transient(self, sub_code: int, err_msg: str) -> None:
        err = FeishuAPIError(
            f"update_card: code=230099, msg=Failed to create card content, ext=ErrCode: {sub_code}; ErrMsg: {err_msg}",
            CARDKIT_CONTENT_FAILED,
        )
        assert _is_transient_error(err) is False
        assert _is_card_limit_error(err) is False

    def test_230099_11310_action_lock_is_transient_not_limit(self) -> None:
        err = FeishuAPIError(
            "update_card: code=230099, msg=Failed to create card content, ext=ErrCode: 11310; ErrMsg: card action is lock",
            CARDKIT_CONTENT_FAILED,
        )
        assert _is_transient_error(err) is True
        assert _is_card_limit_error(err) is False

    @pytest.mark.parametrize(
        "msg",
        [
            "ErrCode: 11310; ErrMsg: element exceeds the limit",
            "ErrCode: 11310; card table number over limit",
            "Failed to create card content, ext=ErrCode: 11310",
        ],
    )
    def test_230099_11310_limit_variants_trigger_card_limit_not_transient(self, msg: str) -> None:
        err = FeishuAPIError(msg, CARDKIT_CONTENT_FAILED)
        assert _is_transient_error(err) is False
        assert _is_card_limit_error(err) is True

    def test_230099_11310_template_permission_neither_transient_nor_limit(self) -> None:
        err = FeishuAPIError(
            "ErrCode: 11310; ErrMsg: template is not visible to app",
            CARDKIT_CONTENT_FAILED,
        )
        assert _is_transient_error(err) is False
        assert _is_card_limit_error(err) is False

    def test_cardkit_interaction_locked_and_internal_error_are_transient(self) -> None:
        assert CARDKIT_INTERACTION_LOCKED in CARDKIT_TRANSIENT_CODES
        assert 300120 in CARDKIT_TRANSIENT_CODES
        assert _is_transient_error(FeishuAPIError("ongoing interaction", CARDKIT_INTERACTION_LOCKED)) is True
        assert _is_transient_error(FeishuAPIError("internal error", 300120)) is True

    def test_300312_tag_immutable_is_schema_error(self) -> None:
        err = FeishuAPIError("Unable to update element tag", CARDKIT_TAG_IMMUTABLE)
        assert is_schema_error(err) is True

    @pytest.mark.parametrize(
        "code",
        [CARDKIT_STREAMING_CLOSED, CARDKIT_STREAMING_TIMEOUT, CARDKIT_STREAMING_ADD_TIMEOUT],
    )
    def test_streaming_closed_includes_timeout_codes(self, code: int) -> None:
        assert is_streaming_closed_error(FeishuAPIError("streaming closed/timeout", code)) is True

    @pytest.mark.parametrize("code", [230110, 232009, 230031, 200740, 200750, 230011, 231003])
    def test_terminal_api_codes_include_official_im_and_cardkit_codes(self, code: int) -> None:
        assert is_terminal_api_code(code) is True


class TestStreamingTimeoutHandling:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [CARDKIT_STREAMING_TIMEOUT, CARDKIT_STREAMING_ADD_TIMEOUT])
    async def test_drain_answer_falls_back_on_streaming_timeout(self, code: int) -> None:
        ctrl = StreamCardController()
        ctrl._initialized = True
        ctrl._client = AsyncMock(spec=FeishuClient)
        ctrl._client.cardkit_stream_element.side_effect = FeishuAPIError("timeout", code=code)

        session = CardSession("msg_timeout", "chat_1", asyncio.get_running_loop())
        session.linear = True
        session.card_id = "card_timeout"
        session.state = STREAMING
        session._creation_stages.add("answer")
        session.unified_state = UnifiedLinearState()
        session.unified_state.on_answer_delta("hello timeout")

        await ctrl._drain_answer_text(
            session, session.unified_state,
            source="drain", log_key="msg", log_id="msg_timeout",
        )
        assert session._streaming_closed is True
        ctrl._client.cardkit_batch_update.assert_awaited_once()
        assert session._streamed_answer == "hello timeout"


class TestCardJsonV2Structure:
    def test_all_card_builders_pass_v2_audit(self, monkeypatch) -> None:
        rounds = [SimpleNamespace(index=1, elapsed_ms=900, text="reasoning step")]
        steps = [
            {
                "name": "terminal",
                "title": "Terminal",
                "title_zh": "终端命令",
                "status": "success",
                "detail": "echo hi",
                "emoji": "🖥️",
                "result_block": {"language": "json", "content": '{"ok": true}'},
                "error_block": None,
            },
            {
                "name": "terminal",
                "title": "Terminal",
                "title_zh": "终端命令",
                "status": "error",
                "detail": "false",
                "emoji": "🖥️",
                "result_block": None,
                "error_block": {"language": "text", "content": "exit 1"},
            },
        ]
        panel = build_unified_panel(reasoning_rounds=rounds, tool_steps=steps, expanded=True)

        # Simulate /aowen config reload failure branch too
        from hermes_lark_streaming.config import Config
        monkeypatch.setattr(Config, "reload", lambda self: (_ for _ in ()).throw(RuntimeError("bad yaml")))
        reload_err_card = _handle_config_reload()

        cards = {
            "streaming_v2": build_streaming_card_v2(include_unified_panel=True, include_answer_element=True),
            "interactive_v2": build_interactive_card_v2(
                elements=[
                    *build_pinned_reasoning_elements_from_text("thought", expanded=True),
                    panel,
                    *build_answer_elements("hello\n\n|a|b|\n|---|---|\n|1|2|\n\nafter"),
                    *_build_footer_elements({"duration": 1.2, "model": "deepseek-v4"}),
                ],
                summary="hello",
                text_sizes={"body": {"default": "normal", "pc": "normal", "mobile": "large"}},
            ),
            "cron": build_cron_card("cron content"),
            "gateway": build_gateway_card("gw content", status_label="Processing", status_emoji="⏳"),
            "clarify_3": build_clarify_card(question="Pick?", choices=["A", "B", "C"], clarify_id="c1"),
            "clarify_5": build_clarify_card(question="Pick?", choices=["1", "2", "3", "4", "5"], clarify_id="c2"),
            "clarify_submitted": build_clarify_submitted_card(question="Pick?", selected="A", clarify_id="c1"),
            "clarify_confirmed": build_clarify_confirmed_card(question="Pick?", selected="A"),
            "clarify_expired": build_clarify_expired_card(question="Pick?"),
            "aowen_help": build_help_card(),
            "aowen_status": build_status_card(),
            "aowen_monitor": build_monitor_card(),
            "aowen_reset": build_reset_card(),
            "aowen_unknown": _build_unknown_command_card("bad"),
            "aowen_interrupt": build_interrupt_hint_card(),
            "aowen_reload_err": reload_err_card,
        }

        for name, card in cards.items():
            assert card.get("schema") == "2.0", f"{name} missing schema 2.0"
            assert card.get("config", {}).get("update_multi") is True, f"{name} missing update_multi=True"
            findings = _audit_card_v2(card, name)
            assert findings == [], f"{name} failed v2 audit: {findings}"

    def test_tool_step_output_uses_markdown_component(self) -> None:
        el = _build_tool_step_output(
            {"result_block": {"language": "json", "content": '{"a": 1}'}},
            text_sizes={"tool": {"default": "x-small", "pc": "x-small", "mobile": "small"}},
        )
        assert el is not None
        assert el["tag"] == "markdown"
        assert el["margin"] == "0px 0px 0px 22px"
        assert el["text_size"] == "hls_tool"
        assert "```json" in el["content"]


class TestCardKitSequenceValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_seq", [0, -1, 2_147_483_648, True])
    async def test_cardkit_writes_reject_out_of_range_sequence(self, bad_seq) -> None:
        client = FeishuClient(FeishuClientConfig("app_id", "app_secret"))
        with pytest.raises(FeishuAPIError, match="sequence"):
            await client.cardkit_update("card_1", {"schema": "2.0"}, sequence=bad_seq)
        with pytest.raises(FeishuAPIError, match="sequence"):
            await client.cardkit_batch_update("card_1", [], sequence=bad_seq)
        with pytest.raises(FeishuAPIError, match="sequence"):
            await client.cardkit_stream_element("card_1", "el_1", "hi", sequence=bad_seq)
        with pytest.raises(FeishuAPIError, match="sequence"):
            await client.cardkit_close_streaming("card_1", sequence=bad_seq)

    @pytest.mark.asyncio
    async def test_gateway_cardkit_update_increments_positive_sequence(self) -> None:
        ctrl = StreamCardController()
        ctrl._client = AsyncMock(spec=FeishuClient)
        info: dict = {"card_id": "card_gw"}

        await ctrl._update_gateway_card({"schema": "2.0"}, card_id="card_gw", card_msg_id="msg_1", info=info)
        await ctrl._update_gateway_card({"schema": "2.0"}, card_id="card_gw", card_msg_id="msg_1", info=info)

        seqs = [call.kwargs["sequence"] for call in ctrl._client.cardkit_update.await_args_list]
        assert seqs == [1, 2]
