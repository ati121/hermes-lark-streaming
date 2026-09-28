"""OpenViking server extension: stream real stages and the original HTTP result.

Only POST search/find requests with X-Hermes-Progress: 1 use the protocol.
Authentication, request validation and search behavior stay in OpenViking.
This module is copied into the OpenViking package by the installer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextvars import ContextVar
from functools import wraps

PROGRESS_MEDIA_TYPE = "application/x-hermes-openviking-progress+ndjson"
_events = ContextVar("hermes_openviking_stage_events", default=None)
_logger = logging.getLogger(__name__)


def emit_stage(stage: str) -> None:
    queue = _events.get()
    if queue is not None:
        queue.put_nowait({"type": "stage", "stage": stage})


def _wrap_stage(cls, method: str, stage: str) -> None:
    original = getattr(cls, method)
    if getattr(original, "_hermes_progress", False):
        return

    @wraps(original)
    async def wrapped(*args, **kwargs):
        emit_stage(stage)
        return await original(*args, **kwargs)

    wrapped._hermes_progress = True
    setattr(cls, method, wrapped)


class ProgressMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http" or scope.get("method") != "POST"
            or scope.get("path") not in ("/api/v1/search/search", "/api/v1/search/find")
            or dict(scope.get("headers", [])).get(b"x-hermes-progress") != b"1"
        ):
            return await self.app(scope, receive, send)

        queue = asyncio.Queue()

        async def run():
            token = _events.set(queue)
            try:
                await self.app(scope, receive, queue.put)
            except Exception:
                # Preserve errors already handled by the app; otherwise send a
                # generic failure, never exception text or internal identifiers.
                _logger.exception("OpenViking progress request failed")
                await queue.put({"type": "failure"})
            finally:
                _events.reset(token)
                await queue.put({"type": "done"})

        worker = asyncio.create_task(run())
        streaming = False
        response_start = None
        chunks = []
        last_stage = None

        async def frame(event):
            await send({"type": "http.response.body", "body": json.dumps(event).encode() + b"\n", "more_body": True})

        try:
            while True:
                event = await queue.get()
                kind = event["type"]
                if kind == "stage":
                    if not streaming:
                        await send({
                            "type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", PROGRESS_MEDIA_TYPE.encode()),
                                        (b"cache-control", b"no-store"), (b"x-accel-buffering", b"no")],
                        })
                        streaming = True
                    if event["stage"] != last_stage:
                        await frame(event)
                        last_stage = event["stage"]
                elif kind == "http.response.start":
                    response_start = event
                elif kind == "http.response.body":
                    chunks.append(event.get("body", b""))
                elif kind == "failure":
                    response_start = {"type": "http.response.start", "status": 500,
                                      "headers": [(b"content-type", b"application/json")]}
                    chunks = [b'{"status":"error","error":{"message":"Internal server error"}}']
                elif kind == "done":
                    if response_start is None:
                        raise RuntimeError("OpenViking produced no HTTP response")
                    body = b"".join(chunks)
                    if streaming:
                        await frame({
                            "type": "result", "status": response_start["status"],
                            "headers": [[k.decode("latin-1"), v.decode("latin-1")]
                                        for k, v in response_start.get("headers", [])],
                            "body": base64.b64encode(body).decode("ascii"),
                        })
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    else:
                        # No stage ran (e.g. authentication/validation failed):
                        # return the ordinary response, with its original status.
                        await send(response_start)
                        await send({"type": "http.response.body", "body": body, "more_body": False})
                    return
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def install(app) -> None:
    if getattr(app.state, "hermes_progress_installed", False):
        return
    from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
    from openviking.retrieve.intent_analyzer import IntentAnalyzer

    _wrap_stage(IntentAnalyzer, "analyze", "intent_analysis")
    _wrap_stage(HierarchicalRetriever, "retrieve", "memory_retrieval")
    app.add_middleware(ProgressMiddleware)
    app.state.hermes_progress_installed = True
