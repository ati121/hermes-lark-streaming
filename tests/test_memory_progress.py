"""Real phase delivery preserves JSON results, errors, scope and cancellation."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from contextlib import contextmanager
from contextvars import copy_context
from threading import Thread
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from hermes_lark_streaming.integrations.install_openviking_progress import (
    INJECTION,
    patched_app,
)
from hermes_lark_streaming.integrations.openviking_progress import (
    PROGRESS_MEDIA_TYPE,
    ProgressMiddleware,
    _wrap_stage,
    emit_stage,
)
from hermes_lark_streaming.patching.memory_progress import (
    PrefetchProgress,
    _progress_context,
    _stream_request,
    maybe_wrap_viking_http,
)


def result_event(body, status=200):
    return {"type": "result", "status": status,
            "headers": [["content-type", "application/json"]],
            "body": base64.b64encode(json.dumps(body).encode()).decode()}


class EventStream(httpx.SyncByteStream):
    def __init__(self, events):
        self.events = events

    def __iter__(self):
        for event in self.events:
            if callable(event):
                event()
            else:
                yield json.dumps(event).encode() + b"\n"


def transport_client(handler):
    transport = httpx.MockTransport(handler)

    @contextmanager
    def stream(method, url, **kwargs):
        with httpx.Client(transport=transport) as client, client.stream(method, url, **kwargs) as response:
            yield response

    def parse(send):
        response = send({"X-API-Key": "test-key"})
        response.raise_for_status()
        return response.json()

    return SimpleNamespace(_endpoint="https://openviking.test", _httpx=SimpleNamespace(
        stream=stream, Response=httpx.Response,
    ), _send_with_trusted_identity_retry=parse)


def test_client_delivers_stages_before_result_and_preserves_request():
    seen = []
    body = {"status": "ok", "result": {"memories": [{"uri": "viking://user/test/item"}]}}
    events = [
        {"type": "stage", "stage": "intent_analysis"},
        lambda: seen == ["intent_analysis"] or pytest.fail("intent event was buffered"),
        {"type": "stage", "stage": "memory_retrieval"},
        result_event(body),
    ]

    def handler(request):
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["x-hermes-progress"] == "1"
        assert json.loads(request.content) == {"query": "test"}
        return httpx.Response(200, headers={"content-type": PROGRESS_MEDIA_TYPE}, stream=EventStream(events))

    progress = PrefetchProgress(lambda active, stage: seen.append(stage))
    options = {"timeout": 3, "json": {"query": "test"}}
    assert _stream_request(transport_client(handler), "post", "/api/v1/search/search", options, progress, 30) == body
    assert seen == ["intent_analysis", "memory_retrieval"]
    assert options["timeout"] == 3


def test_old_server_returns_json_without_retry_or_invented_stage():
    calls = []
    seen = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"result": {"total": 0}})

    result = _stream_request(transport_client(handler), "post", "/api/v1/search/search", {},
                             PrefetchProgress(lambda *a, **kw: seen.append(kw)), 30)
    assert result == {"result": {"total": 0}}
    assert len(calls) == 1 and seen == []


def test_streamed_http_error_is_still_an_http_error():
    client = transport_client(lambda request: httpx.Response(
        200, headers={"content-type": PROGRESS_MEDIA_TYPE},
        stream=EventStream([result_event({"error": "denied"}, status=403)]),
    ))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        _stream_request(client, "post", "/api/v1/search/search", {}, PrefetchProgress(lambda *a, **kw: None), 30)
    assert raised.value.response.status_code == 403


def test_truncated_stream_is_not_a_successful_empty_result():
    client = transport_client(lambda request: httpx.Response(
        200, headers={"content-type": PROGRESS_MEDIA_TYPE}, stream=EventStream([]),
    ))
    with pytest.raises(RuntimeError, match="without a result"):
        _stream_request(client, "post", "/api/v1/search/search", {}, PrefetchProgress(lambda *a, **kw: None), 30)


def test_closed_prefetch_ignores_late_and_unknown_stages():
    seen = []
    progress = PrefetchProgress(lambda *a, **kw: seen.append(kw))
    progress.stage("unknown")
    progress.closed = True
    progress.stage("intent_analysis")
    assert seen == []


def test_http_hook_follows_copied_worker_context_and_leaves_other_calls_alone(monkeypatch):
    module = ModuleType("test_viking_provider")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    module._TIMEOUT = 3

    class Client:
        def _request(self, method, path, kwargs):
            return "original"

    class Provider:
        pass

    Provider.__module__ = module.__name__
    module._VikingClient = Client
    client = Client()
    client.__dict__.update(vars(transport_client(lambda request: httpx.Response(200, json={"result": "context"}))))
    maybe_wrap_viking_http(Provider())
    wrapper = Client._request
    maybe_wrap_viking_http(Provider())
    assert Client._request is wrapper
    assert client._request("post", "/api/v1/search/find", {}) == "original"
    seen, results = [], []
    progress = PrefetchProgress(lambda active, stage: seen.append(stage))
    token = _progress_context.set(progress)
    try:
        assert client._request("get", "/api/v1/content/read", {}) == "original"
        context = copy_context()  # Hermes spawn_context_thread does this too.
        worker = Thread(target=lambda: context.run(lambda: results.append(client._request("post", "/api/v1/search/find", {}))))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert results == [{"result": "context"}] and seen == ["memory_retrieval"]
        progress.closed = True
        assert client._request("post", "/api/v1/search/find", {}) == "original"
    finally:
        _progress_context.reset(token)


def scope(path="/api/v1/search/search", enabled=True):
    return {"type": "http", "method": "POST", "path": path,
            "headers": [(b"x-hermes-progress", b"1")] if enabled else []}


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def frames(messages):
    return [json.loads(m["body"]) for m in messages if m["type"] == "http.response.body" and m.get("body")]


@pytest.mark.asyncio
async def test_server_sends_real_stages_while_operation_is_still_running():
    messages = []
    first_stage = asyncio.Event()
    release = asyncio.Event()
    body = b'{"result":{"total":1}}'

    async def app(scope, receive, send):
        emit_stage("intent_analysis")
        await release.wait()
        emit_stage("memory_retrieval")
        emit_stage("memory_retrieval")  # Parallel typed queries need one label.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def send(message):
        messages.append(message)
        if message.get("body") and b'intent_analysis' in message["body"]:
            first_stage.set()

    task = asyncio.create_task(ProgressMiddleware(app)(scope(), receive, send))
    try:
        await asyncio.wait_for(first_stage.wait(), 1)
        assert not task.done()
    finally:
        release.set()
        await task
    events = frames(messages)
    assert [e["stage"] for e in events if e["type"] == "stage"] == ["intent_analysis", "memory_retrieval"]
    assert base64.b64decode(events[-1]["body"]) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,status", [(False, 200), (True, 401)])
async def test_no_stage_leaves_normal_response_and_auth_errors_unchanged(enabled, status):
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"status":"test"}', "more_body": False})

    async def send(message):
        messages.append(message)

    await ProgressMiddleware(app)(scope(enabled=enabled), receive, send)
    assert messages[0]["status"] == status
    assert messages[1]["body"] == b'{"status":"test"}'


@pytest.mark.asyncio
async def test_parallel_requests_do_not_share_stage_events():
    async def app(scope, receive, send):
        emit_stage("intent_analysis" if scope["path"].endswith("search") else "memory_retrieval")
        await asyncio.sleep(0)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{}', "more_body": False})

    async def run(path):
        messages = []
        async def send(message):
            messages.append(message)
        await ProgressMiddleware(app)(scope(path), receive, send)
        return [f["stage"] for f in frames(messages) if f["type"] == "stage"]

    assert await asyncio.gather(run("/api/v1/search/search"), run("/api/v1/search/find")) == [
        ["intent_analysis"], ["memory_retrieval"],
    ]


@pytest.mark.asyncio
async def test_disconnect_cancels_request_worker():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        try:
            emit_stage("intent_analysis")
            entered.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def send(message):
        pass

    task = asyncio.create_task(ProgressMiddleware(app)(scope(), receive, send))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_zero_query_plan_does_not_emit_retrieval():
    class Analyzer:
        async def analyze(self):
            return []

    _wrap_stage(Analyzer, "analyze", "intent_analysis")
    first = Analyzer.analyze
    _wrap_stage(Analyzer, "analyze", "intent_analysis")
    assert Analyzer.analyze is first

    async def app(scope, receive, send):
        assert await Analyzer().analyze() == []
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{}', "more_body": False})

    messages = []
    async def send(message):
        messages.append(message)
    await ProgressMiddleware(app)(scope(), receive, send)
    assert [f["stage"] for f in frames(messages) if f["type"] == "stage"] == ["intent_analysis"]


def test_installer_changes_only_factory_return_and_is_reversible():
    source = "def create_app():\n    app = factory()\n    def nested():\n        return app\n    return app\n"
    patched = patched_app(source)
    assert patched.count(INJECTION) == 1
    assert patched_app(patched) == patched
    assert patched.replace(INJECTION, "") == source
    assert "def nested():\n        return app\n" in patched


def test_installer_refuses_unknown_factory():
    with pytest.raises(ValueError, match="Unsupported"):
        patched_app("def different_factory():\n    return object()\n")
