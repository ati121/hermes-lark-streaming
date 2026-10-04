"""Resolve bundled brand artwork using the receiving bot's image credentials."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

OPENVIKING_ASSET = "hls-asset:openviking"


def openviking_icon() -> dict:
    return {"tag": "custom_icon", "img_key": OPENVIKING_ASSET, "size": "16px 16px"}


class BrandAssets:
    def __init__(self) -> None:
        self._key: str | None = None
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    async def resolve(self, payload, upload):
        def contains(value):
            if isinstance(value, dict):
                return value.get("img_key") == OPENVIKING_ASSET or any(contains(v) for v in value.values())
            return isinstance(value, list) and any(contains(v) for v in value)

        if not contains(payload):
            return payload
        async with self._lock:
            if not self._key and time.monotonic() >= self._retry_at:
                self._retry_at = time.monotonic() + 60
                try:
                    self._key = await asyncio.wait_for(
                        upload(str(Path(__file__).parent / "assets" / "openviking.png")), timeout=2,
                    )
                except (TimeoutError, OSError):
                    self._key = None

        def replace(value):
            if isinstance(value, dict):
                if value.get("img_key") == OPENVIKING_ASSET:
                    if self._key:
                        return {**value, "img_key": self._key}
                    return {"tag": "standard_icon", "token": "doc-search_outlined", "size": "16px 16px"}
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return value

        return replace(payload)
