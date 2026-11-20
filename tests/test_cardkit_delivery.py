"""CardKit dispatch order, request budgets and idempotent retry at the SDK boundary."""

import asyncio
import importlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from hermes_lark_streaming.feishu import (
    CardKitAmbiguousWrite,
    FeishuAPIError,
    FeishuClient,
    FeishuClientConfig,
)
from hermes_lark_streaming.feishu import client as client_module
from hermes_lark_streaming.feishu.branding import GITHUB_ASSET


@pytest.fixture
def make_client(monkeypatch):
    # Keep the actual SDK request serialization and response parsing. Bypass
    # authentication and run every HTTP request through the test transport.
    sdk_modules = (
        "lark_oapi.api.cardkit.v1.resource.card",
        "lark_oapi.api.cardkit.v1.resource.card_element",
    )
    for name in sdk_modules:
        monkeypatch.setattr(importlib.import_module(name), "verify", lambda *_: None)
    monkeypatch.setattr(client_module, "_TRANSIENT_RETRY_DELAYS", (0.0, 0.0, 0.0))
    http_client = httpx.AsyncClient

    def make(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda: http_client(transport=transport))
        return FeishuClient(FeishuClientConfig("test-app", "test-secret"))

    return make


@pytest.mark.asyncio
async def test_cardkit_mutations_reuse_uuid_on_retry(make_client):
    attempts = []

    async def wire(request):
        body = json.loads(request.content)
        attempts.append((request.url.path, body))
        if len(attempts) % 2:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    api = make_client(wire)
    await api.cardkit_stream_element("card", "answer", "text", sequence=1)
    await api.cardkit_batch_update("card", [{"action": "add_elements"}], sequence=2)
    await api.cardkit_update("card", {"schema": "2.0"}, sequence=3)
    await api.cardkit_close_streaming("card", sequence=4)
    await api.cardkit_update_summary("card", "summary", sequence=5)

    assert len(attempts) == 10
    assert attempts[::2] == attempts[1::2]
    assert [body["sequence"] for _, body in attempts[::2]] == [1, 2, 3, 4, 5]
    assert len({body["uuid"] for _, body in attempts[::2] if len(body["uuid"]) == 32}) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [200770, 300317])
@pytest.mark.parametrize("lost_first_response", [True, False])
async def test_cardkit_conflict_does_not_confirm_commit(make_client, code, lost_first_response):
    attempts = []

    async def wire(request):
        attempts.append(json.loads(request.content))
        if lost_first_response and len(attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(400, json={"code": code, "msg": "conflict"})

    api = make_client(wire)
    with pytest.raises(FeishuAPIError) as raised:
        await api.cardkit_batch_update("card", [{"action": "add_elements"}], sequence=5)

    assert raised.value.code == code
    assert isinstance(raised.value, CardKitAmbiguousWrite) is lost_first_response
    assert len(attempts) == 1 + int(lost_first_response)
    assert all(body == attempts[0] for body in attempts)


@pytest.mark.asyncio
async def test_only_ten_cardkit_attempts_per_rolling_second_and_other_cards_can_write(make_client, monkeypatch):
    attempts = []
    loop = asyncio.get_running_loop()
    preparing, allow_preparation = asyncio.Event(), asyncio.Event()

    async def wire(request):
        card_id = request.url.path.split("/")[5]
        seq = json.loads(request.content)["sequence"]
        attempts.append((card_id, seq, loop.time()))
        if card_id == "main" and seq == 1 and sum(card == "main" for card, _, _ in attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    api = make_client(wire)
    original_card_json = api._card_json

    async def prepare(payload):
        preparing.set()
        await allow_preparation.wait()
        return await original_card_json(payload)

    monkeypatch.setattr(api, "_card_json", prepare)
    first = asyncio.create_task(api.cardkit_batch_update("main", [], sequence=1))
    await preparing.wait()
    other_pending = [
        asyncio.create_task(api.cardkit_stream_element("main", "answer", "text", sequence=seq))
        for seq in range(2, 13)
    ]
    # Cancel one operation while queued. Its sequence must never reach Feishu.
    await asyncio.sleep(0)
    other_pending[1].cancel()
    await api.cardkit_close_streaming("other", sequence=1)
    assert [(card, seq) for card, seq, _ in attempts] == [("other", 1)]

    allow_preparation.set()
    outcomes = await asyncio.gather(first, *other_pending, return_exceptions=True)
    for idx, outcome in enumerate(outcomes):
        if idx == 2:
            assert isinstance(outcome, asyncio.CancelledError)
        else:
            assert outcome is None

    sent = [(seq, started) for card, seq, started in attempts if card == "main"]
    assert [seq for seq, _ in sent] == [1, 1, 2, *range(4, 13)]
    assert all(sent[i + 10][1] - sent[i][1] >= 1 for i in range(len(sent) - 10))
    await asyncio.sleep(1.05)
    assert not api._cardkit_write_states


@pytest.mark.asyncio
async def test_cardkit_rejects_oversize_fields_without_transmitting(make_client):
    async def reject_unexpected_request(request):
        pytest.fail(f"Unexpected network request to {request.url.path}")

    api = make_client(reject_unexpected_request)
    with pytest.raises(FeishuAPIError, match="100000 characters"):
        await api.cardkit_stream_element("card", "answer", "a" * 100_001, sequence=1)
    actions = [{"action": "partial_update_element", "params": {"content": "a" * 1_000_001}}]
    with pytest.raises(FeishuAPIError, match="1000000 characters"):
        await api.cardkit_batch_update("card", actions, sequence=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_complete_card_utf8_budget_is_checked_before_sdk_dispatch(make_client, operation):
    sent = []

    async def wire(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"card_id": "created-card"}})

    api = make_client(wire)

    async def deliver(card):
        if operation == "create":
            return await api.cardkit_create(card)
        return await api.cardkit_update("card", card, sequence=1)

    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": ""}]}}
    overhead = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    card["body"]["elements"][0]["content"] = "a" * (30_000 - overhead)
    await deliver(card)
    assert len(sent) == 1
    serialized = sent[0]["data"] if operation == "create" else sent[0]["card"]["data"]
    assert len(serialized.encode("utf-8")) == 30_000

    card["body"]["elements"][0]["content"] += "a"
    with pytest.raises(ValueError, match="30000 UTF-8 bytes"):
        await deliver(card)

    # Well below 30k Python characters, but above the UTF-8 byte budget.
    card["body"]["elements"][0]["content"] = "龘🙂" * 5_000
    assert len(json.dumps(card, ensure_ascii=False)) < 30_000
    with pytest.raises(ValueError, match="30000 UTF-8 bytes"):
        await deliver(card)
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["json2", "legacy"])
async def test_complete_card_budget_counts_nested_text_tags(make_client, legacy):
    sent = []

    async def wire(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    api = make_client(wire)
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": "text"}} for _ in range(100)]
    card = {"elements": elements} if legacy else {"schema": "2.0", "body": {"elements": elements}}
    await api.cardkit_update("card", card, sequence=1)
    elements.append({"tag": "hr"})
    with pytest.raises(ValueError, match="200 tagged elements"):
        await api.cardkit_update("card", card, sequence=2)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_complete_card_budget_uses_resolved_brand_image_key(make_client, monkeypatch):
    async def reject_unexpected_request(request):
        pytest.fail(f"Unexpected network request to {request.url.path}")

    api = make_client(reject_unexpected_request)
    upload = AsyncMock(return_value="img_" + "a" * 30_000)
    monkeypatch.setattr(api, "upload_local_image", upload)
    card = {"schema": "2.0", "body": {"elements": [{"tag": "img", "img_key": GITHUB_ASSET}]}}
    with pytest.raises(ValueError, match="30000 UTF-8 bytes"):
        await api.cardkit_create(card)
    upload.assert_awaited_once()
    assert card["body"]["elements"][0]["img_key"] == GITHUB_ASSET


@pytest.mark.asyncio
async def test_batch_actions_do_not_use_whole_card_size_or_element_limits(make_client):
    sent = []

    async def wire(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    api = make_client(wire)
    actions = [{
        "action": "add_elements",
        "params": {"type": "append", "elements": [
            {"tag": "markdown", "content": "龘" * 100} for _ in range(201)
        ]},
    }]
    assert len(json.dumps(actions, ensure_ascii=False).encode("utf-8")) > 30_000
    await api.cardkit_batch_update("card", actions, sequence=1)
    assert len(sent) == 1
    assert json.loads(sent[0]["actions"]) == actions
