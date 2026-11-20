"""All IM card PATCH paths share dispatch spacing and provider cooldowns."""

import asyncio
import json
from email.utils import formatdate
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from hermes_lark_streaming.feishu import client as client_module
from hermes_lark_streaming.feishu.client import (
    FeishuAPIError,
    FeishuClient,
    MessageDeliveryUncertain,
    _IMUpdateState,
    _unacknowledged_write,
)


def response(code=0, *, headers=None, status=200):
    return SimpleNamespace(
        success=lambda: code == 0, code=code, msg="rate rejected" if code else "ok",
        get_log_id=lambda: "provider-trace", raw=SimpleNamespace(status_code=status, headers=headers or {}),
    )


def client_with(send):
    client = object.__new__(FeishuClient)
    client._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(apatch=send),
    )))
    client._card_json = AsyncMock(side_effect=json.dumps)
    return client


def test_error_keeps_positional_compatibility_and_retry_hint_when_wrapped():
    error = FeishuAPIError("rejected", 230429, "trace", retry_after=35.0)
    wrapped = _unacknowledged_write(MessageDeliveryUncertain, "update_card", error)
    assert (wrapped.code, wrapped.log_id, wrapped.retry_after) == (230429, "trace", 35.0)
    assert FeishuAPIError("old", 1, "old-trace").retry_after is None


@pytest.mark.parametrize("code,status", [
    (230020, 400), (230429, 400), (99991400, 400), (None, 429), (230001, 429), (230429, 429),
])
def test_rate_responses_extract_case_insensitive_provider_hint(code, status):
    with pytest.raises(FeishuAPIError) as raised:
        FeishuClient._check(response(code, status=status, headers={
            "X-Ogw-Ratelimit-Reset": "35.5", "Retry-After": "60",
        }), "update_card")
    assert raised.value.code == (429 if status == 429 else code)
    assert raised.value.retry_after == 35.5
    assert raised.value.log_id == "provider-trace"


@pytest.mark.parametrize("bad_value", ["invalid", "-1", "NaN", "Infinity"])
def test_invalid_reset_falls_back_to_retry_after(bad_value):
    with pytest.raises(FeishuAPIError) as raised:
        FeishuClient._check(response(230429, headers={
            "x-ogw-ratelimit-reset": bad_value, "Retry-After": "21",
        }), "update_card")
    assert raised.value.retry_after == 21.0


def test_retry_after_http_date_and_non_rate_errors(monkeypatch):
    monkeypatch.setattr(client_module._time, "time", lambda: 1_800_000_000)
    with pytest.raises(FeishuAPIError) as raised:
        FeishuClient._check(response(230429, headers={
            "Retry-After": formatdate(1_800_000_045, usegmt=True),
        }), "update_card")
    assert raised.value.retry_after == 45.0
    with pytest.raises(FeishuAPIError) as raised:
        FeishuClient._check(response(230001, headers={"Retry-After": "60"}), "update_card")
    assert raised.value.retry_after is None


def test_im_cooldown_escalates_to_cap_and_honors_longer_hint():
    state = _IMUpdateState()
    assert [state.reject(100.0, None) for _ in range(5)] == [12.0, 24.0, 48.0, 60.0, 60.0]
    assert state.reject(100.0, 90.0) == 90.0


@pytest.mark.asyncio
async def test_same_message_attempts_are_serial_and_spaced_other_messages_are_independent():
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []
    running = 0
    max_running = 0

    async def send(request):
        nonlocal running, max_running
        message_id = request.message_id
        attempts.append((message_id, loop.time()))
        if message_id == "main":
            running += 1
            max_running = max(max_running, running)
            if len(attempts) == 1:
                entered.set()
                await release.wait()
            running -= 1
        return response()

    client = client_with(send)
    first = asyncio.create_task(client.update_card("main", {}))
    await entered.wait()
    queued = [asyncio.create_task(client.update_card("main", {})) for _ in range(5)]
    await client.update_card("other", {})
    assert [message for message, _ in attempts] == ["main", "other"]
    release.set()
    await asyncio.gather(first, *queued)
    main_times = [started for message, started in attempts if message == "main"]
    assert max_running == 1
    assert len(main_times) == 6
    assert all(after - before >= 0.25 for before, after in pairwise(main_times))


@pytest.mark.asyncio
async def test_network_retries_reenter_same_message_gate(monkeypatch):
    monkeypatch.setattr(client_module, "_TRANSIENT_RETRY_DELAYS", (0.0, 0.0, 0.0))
    started = []

    async def send(_request):
        started.append(asyncio.get_running_loop().time())
        if len(started) == 1:
            raise httpx.ConnectError("not transmitted")
        return response()

    await client_with(send).update_card("message", {})
    assert len(started) == 2
    assert started[1] - started[0] >= 0.25


@pytest.mark.asyncio
async def test_queued_cancellation_and_obsolete_frame_do_not_transmit():
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    valid = True

    async def send(request):
        sent.append(request.message_id)
        entered.set()
        await release.wait()
        return response()

    client = client_with(send)
    first = asyncio.create_task(client.update_card("message", {}))
    await entered.wait()
    cancelled = asyncio.create_task(client.update_card("message", {}))
    obsolete = asyncio.create_task(client.update_card("message", {}, should_update=lambda: valid))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    valid = False
    release.set()
    await asyncio.gather(first, obsolete)
    assert sent == ["message"]


@pytest.mark.asyncio
async def test_guard_is_rechecked_after_spacing_wait(monkeypatch):
    monkeypatch.setattr(client_module, "_IM_UPDATE_MIN_INTERVAL", 0.05)
    sent = AsyncMock(return_value=response())
    client = client_with(sent)
    await client.update_card("message", {})
    valid = True
    obsolete = asyncio.create_task(client.update_card("message", {}, should_update=lambda: valid))
    await asyncio.sleep(0.01)
    valid = False
    await obsolete
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_guard_is_rechecked_after_serialization_before_cooldown_wait(monkeypatch):
    valid = True

    async def serialize(card):
        nonlocal valid
        valid = False
        return json.dumps(card)

    wait = AsyncMock()
    monkeypatch.setattr(_IMUpdateState, "wait_for_slot", wait)
    send = AsyncMock(return_value=response())
    client = client_with(send)
    client._card_json = AsyncMock(side_effect=serialize)
    await client.update_card("message", {}, should_update=lambda: valid)
    wait.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsolete_frame_stops_after_waking_even_when_endpoint_deadline_was_extended():
    loop = asyncio.get_running_loop()
    prepared = asyncio.Event()
    valid = True

    async def serialize(card):
        prepared.set()
        return json.dumps(card)

    send = AsyncMock(return_value=response())
    client = client_with(send)
    client._card_json = AsyncMock(side_effect=serialize)
    client._im_update_endpoint_cooldown_until = loop.time() + 0.04
    obsolete = asyncio.create_task(client.update_card("message", {}, should_update=lambda: valid))
    await prepared.wait()
    valid = False
    client._im_update_endpoint_cooldown_until = loop.time() + 1.0
    await asyncio.wait_for(obsolete, timeout=0.2)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_frame_sets_shared_cooldown_and_logs_counts_safely(monkeypatch, caplog):
    monkeypatch.setattr(client_module, "_IM_UPDATE_COOLDOWN", 0.06)
    monkeypatch.setattr(client_module, "_IM_UPDATE_MIN_INTERVAL", 0.005)
    times = []

    async def send(request):
        times.append((request.message_id, asyncio.get_running_loop().time()))
        return response(230429 if len(times) == 1 else 0, status=400 if len(times) == 1 else 200)

    client = client_with(send)
    with pytest.raises(FeishuAPIError) as raised:
        await client.update_card("private-full-message-id", {"private": "payload-secret"})
    assert raised.value.retry_after == pytest.approx(0.06)
    await client.update_card("other-message", {})
    assert vars(client).get("_im_update_endpoint_cooldown_until", 0.0) == 0.0
    await client.update_card("private-full-message-id", {})
    assert [message for message, _ in times] == ["private-full-message-id", "other-message", "private-full-message-id"]
    assert times[2][1] - times[0][1] >= 0.06
    assert client._im_update_states["private-full-message-id"].rejections == 0
    assert "message_hash=" in caplog.text and "attempts_1s=1 attempts_60s=1" in caplog.text
    assert "provider-trace" in caplog.text
    assert "private-full-message-id" not in caplog.text and "payload-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,hint", [
    (99991400, 400, None), (99991400, 400, 0.07), (230001, 429, 0.07), (230429, 429, 0.07),
])
async def test_endpoint_rejection_blocks_other_messages_before_actual_patch(monkeypatch, code, status, hint):
    monkeypatch.setattr(client_module, "_APP_FREQUENCY_LIMIT_RETRY_DELAYS", (0.04, 0.08, 0.12))
    monkeypatch.setattr(client_module, "_TRANSIENT_MAX_RETRIES", 0)
    attempts = []

    async def send(request):
        attempts.append((request.message_id, asyncio.get_running_loop().time()))
        if request.message_id == "rejected":
            headers = {"x-ogw-ratelimit-reset": str(hint)} if hint is not None else {}
            return response(code, status=status, headers=headers)
        return response()

    client = client_with(send)
    with pytest.raises(FeishuAPIError) as raised:
        await client.update_card("rejected", {})
    assert raised.value.code == (429 if status == 429 else code)
    assert raised.value.retry_after == pytest.approx(max(0.04, hint or 0.0))
    assert client._im_update_states["rejected"].cooldown_until == 0.0
    await client.update_card("other", {})
    assert [message for message, _ in attempts] == ["rejected", "other"]
    assert attempts[1][1] - attempts[0][1] >= max(0.04, hint or 0.0)


@pytest.mark.asyncio
async def test_waiting_message_rechecks_endpoint_deadline_extended_by_inflight_rejection(monkeypatch):
    monkeypatch.setattr(client_module, "_APP_FREQUENCY_LIMIT_RETRY_DELAYS", (0.04, 0.08, 0.12))
    monkeypatch.setattr(client_module, "_TRANSIENT_MAX_RETRIES", 0)
    entered, release, prepared = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []
    second_rejected_at = 0.0

    async def send(request):
        nonlocal second_rejected_at
        attempts.append((request.message_id, asyncio.get_running_loop().time()))
        if request.message_id == "inflight":
            entered.set()
            await release.wait()
            second_rejected_at = asyncio.get_running_loop().time()
            return response(99991400, headers={"x-ogw-ratelimit-reset": "0.11"})
        return response(99991400 if request.message_id == "first-rejected" else 0)

    async def serialize(card):
        if card.get("waiting"):
            prepared.set()
        return json.dumps(card)

    client = client_with(send)
    client._card_json = AsyncMock(side_effect=serialize)
    inflight = asyncio.create_task(client.update_card("inflight", {}))
    await entered.wait()
    with pytest.raises(FeishuAPIError):
        await client.update_card("first-rejected", {})
    original_deadline = client._im_update_endpoint_cooldown_until
    waiting = asyncio.create_task(client.update_card("waiting", {"waiting": True}))
    await prepared.wait()
    release.set()
    with pytest.raises(FeishuAPIError):
        await inflight
    assert client._im_update_endpoint_cooldown_until > original_deadline
    await waiting
    assert attempts[-1][0] == "waiting"
    assert attempts[-1][1] - second_rejected_at >= 0.11


@pytest.mark.asyncio
async def test_endpoint_rejections_escalate_shared_retry_budget(monkeypatch):
    delays = (0.02, 0.04, 0.08)
    monkeypatch.setattr(client_module, "_APP_FREQUENCY_LIMIT_RETRY_DELAYS", delays)
    monkeypatch.setattr(client_module, "_IM_UPDATE_MIN_INTERVAL", 0.0)
    attempts = []

    async def send(_request):
        attempts.append(asyncio.get_running_loop().time())
        return response(99991400)

    client = client_with(send)
    with pytest.raises(FeishuAPIError) as raised:
        await client.update_card("message", {})
    assert len(attempts) == 4
    assert all(after - before >= delay for (before, after), delay in zip(pairwise(attempts), delays))
    assert raised.value.retry_after == pytest.approx(delays[-1])
    assert client._im_update_endpoint_rejections == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("code,hint,expected", [(99991400, None, 1.0), (429, 35.0, 35.0), (230429, 40.0, 40.0)])
async def test_retries_honor_hint_and_app_quotas_avoid_short_transient_delay(monkeypatch, code, hint, expected):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FeishuAPIError("rate limit", code, retry_after=hint)

    await object.__new__(FeishuClient)._retry_transient(
        "update_card", attempt, frequency_limit_delays=(1.0, 2.0),
    )
    sleep.assert_awaited_once_with(expected)
    assert calls == 2


@pytest.mark.asyncio
async def test_idle_im_states_are_removed_and_unused_guard_leaves_no_state(monkeypatch):
    monkeypatch.setattr(client_module, "_IM_UPDATE_HISTORY_SECONDS", 0.02)
    monkeypatch.setattr(client_module, "_IM_UPDATE_MIN_INTERVAL", 0.005)
    client = client_with(AsyncMock(return_value=response()))
    await client.update_card("unused", {}, should_update=lambda: False)
    assert not client._im_update_states
    await client.update_card("used", {})
    assert "used" in client._im_update_states
    await asyncio.sleep(0.04)
    assert not client._im_update_states
