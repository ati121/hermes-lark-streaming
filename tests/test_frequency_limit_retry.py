"""v1.8: 飞书 IM 频控（230020）的退避重试 —— 只给终态封口开.

背景：飞书 IM 接口对单聊/群维度限流 5 QPS，超了返回 230020
（``This operation triggers the frequency limit``）。插件的终态封口只有一次
机会 —— 那次 PATCH 被拒，卡片就永久停在「思考中」，上层还会退化成裸文本回复
（``_do_linear_complete_with_fallback``）。所以只给这一次调用开退避重试。

流式刷新那条热路径**不**开：它的输出下一轮就会被覆盖，为它阻塞 1/2/4 秒
只会把刷新越堆越多。这个「默认不重试」是刻意的设计，下面的测试把它钉住。
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_lark_streaming.feishu.client import (
    _FREQUENCY_LIMIT_RETRY_DELAYS,
    _TRANSIENT_RETRY_DELAYS,
    CARDKIT_TRANSIENT_CODES,
    FeishuAPIError,
    FeishuClient,
    IM_FREQUENCY_LIMIT,
    _is_frequency_limit,
)


def _client() -> FeishuClient:
    """跳过 __init__ 拿一个裸实例.

    ``_retry_transient`` 不读任何实例属性，而 ``FeishuClient.__init__`` 会构造
    lark SDK 客户端（需要 app_id/app_secret），跟这里要测的重试循环无关。
    """
    return object.__new__(FeishuClient)


class _SleepRecorder:
    """替换 ``asyncio.sleep``：记录每次退避时长，但不真的等."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _always_fail(code: int):
    """构造一个永远抛指定错误码的 coro_factory，并记录调用次数."""
    calls = {"n": 0}

    async def _do():
        calls["n"] += 1
        raise FeishuAPIError("boom", code=code)

    return _do, calls


def test_frequency_limit_is_not_in_the_transient_set() -> None:
    """230020 不能混进默认重试集合 —— 否则流式热路径也会开始退避等待."""
    assert IM_FREQUENCY_LIMIT not in CARDKIT_TRANSIENT_CODES
    assert _is_frequency_limit(FeishuAPIError("x", code=IM_FREQUENCY_LIMIT))
    assert not _is_frequency_limit(FeishuAPIError("x", code=2200))


@pytest.mark.asyncio
async def test_frequency_limit_not_retried_by_default(monkeypatch) -> None:
    """默认（流式刷新）撞频控：一次就抛，不 await 任何退避."""
    client = _client()
    rec = _SleepRecorder()
    monkeypatch.setattr(asyncio, "sleep", rec)
    _do, calls = _always_fail(IM_FREQUENCY_LIMIT)

    with pytest.raises(FeishuAPIError):
        await client._retry_transient("update_card", _do)

    assert calls["n"] == 1
    assert rec.delays == []


@pytest.mark.asyncio
async def test_frequency_limit_retried_when_delays_given(monkeypatch) -> None:
    """终态封口：显式传入长退避序列后才重试，第三次成功即返回."""
    client = _client()
    rec = _SleepRecorder()
    monkeypatch.setattr(asyncio, "sleep", rec)
    calls = {"n": 0}

    async def _do():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FeishuAPIError("boom", code=IM_FREQUENCY_LIMIT)
        return "ok"

    result = await client._retry_transient(
        "update_card",
        _do,
        frequency_limit_delays=_FREQUENCY_LIMIT_RETRY_DELAYS,
    )

    assert result == "ok"
    assert calls["n"] == 3
    assert rec.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_frequency_limit_exhausts_and_raises(monkeypatch) -> None:
    """全部退避用完仍失败：抛出，总调用次数 = 重试次数 + 1."""
    client = _client()
    rec = _SleepRecorder()
    monkeypatch.setattr(asyncio, "sleep", rec)
    _do, calls = _always_fail(IM_FREQUENCY_LIMIT)

    with pytest.raises(FeishuAPIError):
        await client._retry_transient(
            "update_card",
            _do,
            frequency_limit_delays=_FREQUENCY_LIMIT_RETRY_DELAYS,
        )

    assert calls["n"] == len(_FREQUENCY_LIMIT_RETRY_DELAYS) + 1
    assert rec.delays == list(_FREQUENCY_LIMIT_RETRY_DELAYS)


@pytest.mark.asyncio
async def test_other_transient_codes_keep_short_backoff(monkeypatch) -> None:
    """开着频控重试也不影响别的瞬态码 —— 它们照走原来的 0.1/0.3/0.6."""
    client = _client()
    rec = _SleepRecorder()
    monkeypatch.setattr(asyncio, "sleep", rec)
    _do, _calls = _always_fail(2200)

    with pytest.raises(FeishuAPIError):
        await client._retry_transient(
            "update_card",
            _do,
            frequency_limit_delays=_FREQUENCY_LIMIT_RETRY_DELAYS,
        )

    assert rec.delays == list(_TRANSIENT_RETRY_DELAYS)


@pytest.mark.asyncio
async def test_non_transient_code_never_retried(monkeypatch) -> None:
    """非瞬态错误（内容非法之类）连开着开关也不重试 —— 重试没有意义."""
    client = _client()
    rec = _SleepRecorder()
    monkeypatch.setattr(asyncio, "sleep", rec)
    _do, calls = _always_fail(999999)

    with pytest.raises(FeishuAPIError):
        await client._retry_transient(
            "update_card",
            _do,
            frequency_limit_delays=_FREQUENCY_LIMIT_RETRY_DELAYS,
        )

    assert calls["n"] == 1
    assert rec.delays == []


@pytest.mark.asyncio
async def test_update_card_forwards_the_switch(monkeypatch) -> None:
    """update_card 只在被要求时才把长退避序列交下去，默认交 None."""
    client = _client()
    captured: dict = {}

    async def _fake(operation, coro_factory, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(client, "_retry_transient", _fake)

    await client.update_card("om_x", {"body": {}}, retry_frequency_limit=True)
    assert captured["frequency_limit_delays"] == _FREQUENCY_LIMIT_RETRY_DELAYS

    captured.clear()
    await client.update_card("om_x", {"body": {}})
    assert captured["frequency_limit_delays"] is None
