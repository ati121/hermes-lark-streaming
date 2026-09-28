"""Opt-in OpenViking progress transport, scoped to one bounded prefetch."""

from __future__ import annotations

import base64
import json
import sys
from contextvars import ContextVar
from functools import wraps

from ..runtime_globals import shared_object

PROGRESS_MEDIA_TYPE = "application/x-hermes-openviking-progress+ndjson"
_progress_context = shared_object(
    "openviking_progress_context", lambda: ContextVar("hls_openviking_progress", default=None),
)


class PrefetchProgress:
    def __init__(self, notify):
        self.notify = notify
        self.closed = False

    def stage(self, stage: str) -> None:
        if not self.closed and stage in ("intent_analysis", "memory_retrieval"):
            self.notify(True, stage=stage)


def _stream_request(client, method, path, kwargs, progress, default_timeout):
    options = dict(kwargs)
    timeout = options.pop("timeout", default_timeout)

    def send(headers):
        headers = {**headers, "X-Hermes-Progress": "1"}
        with client._httpx.stream(
            method, f"{client._endpoint}{path}", headers=headers, timeout=timeout, **options,
        ) as response:
            if response.headers.get("content-type", "").split(";", 1)[0] != PROGRESS_MEDIA_TYPE:
                # An unmodified server ignores the opt-in header and returns
                # normal JSON. Do not retry or infer a stage from elapsed time.
                response.read()
                return response
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "stage":
                    progress.stage(event.get("stage"))
                elif event.get("type") == "result":
                    return client._httpx.Response(
                        event["status"], headers=event["headers"],
                        content=base64.b64decode(event["body"], validate=True),
                        request=response.request,
                    )
            raise RuntimeError("OpenViking progress response ended without a result")

    # Keep Hermes's JSON/error parsing and trusted-identity retry unchanged.
    return client._send_with_trusted_identity_retry(send)


def maybe_wrap_viking_http(provider) -> None:
    module = sys.modules.get(type(provider).__module__)
    client_type = getattr(module, "_VikingClient", None)
    original = getattr(client_type, "_request", None)
    if not callable(original) or getattr(original, "_hls_progress_wrapper", False):
        return
    default_timeout = getattr(module, "_TIMEOUT", 30)

    @wraps(original)
    def request(self, method, path, kwargs):
        progress = _progress_context.get()
        if (
            progress is None or progress.closed or method.lower() != "post"
            or path not in ("/api/v1/search/search", "/api/v1/search/find")
            or not callable(getattr(getattr(self, "_httpx", None), "stream", None))
            or not callable(getattr(self, "_send_with_trusted_identity_retry", None))
        ):
            return original(self, method, path, kwargs)
        if path.endswith("/find"):
            progress.stage("memory_retrieval")  # find never runs intent analysis.
        return _stream_request(self, method, path, kwargs, progress, default_timeout)

    request._hls_progress_wrapper = True
    client_type._request = request
