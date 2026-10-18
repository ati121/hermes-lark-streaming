"""Resolve bundled brand artwork using the receiving bot's image credentials."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

OPENVIKING_ASSET = "hls-asset:openviking"
GITHUB_ASSET = "hls-asset:github"

# marker → (bundled file, icon used when the upload fails)
_ASSETS: dict[str, tuple[str, dict]] = {
    OPENVIKING_ASSET: (
        "openviking.png",
        {"tag": "standard_icon", "token": "doc-search_outlined", "size": "16px 16px"},
    ),
    GITHUB_ASSET: (
        "github.png",
        {"tag": "standard_icon", "token": "setting_outlined", "size": "16px 16px"},
    ),
}


def _icon(asset: str) -> dict:
    return {"tag": "custom_icon", "img_key": asset, "size": "16px 16px"}


def openviking_icon() -> dict:
    return _icon(OPENVIKING_ASSET)


def github_icon() -> dict:
    return _icon(GITHUB_ASSET)


_BRAND_ICONS = {"github": github_icon}


def brand_icon(brand: str) -> dict:
    """Icon dict for a ``brand`` key from ``ToolUseTracker`` steps.

    An unknown key gets the neutral terminal token rather than borrowing
    another brand's artwork.
    """
    factory = _BRAND_ICONS.get(brand)
    if factory:
        return factory()
    return {"tag": "standard_icon", "token": "setting_outlined", "size": "16px 16px"}


class BrandAssets:
    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, payload, upload):
        def markers(value):
            if isinstance(value, dict):
                asset = value.get("img_key")
                if asset in _ASSETS:
                    yield asset
                for item in value.values():
                    yield from markers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from markers(item)

        present = list(dict.fromkeys(markers(payload)))
        if not present:
            return payload
        needed = [asset for asset in present if asset not in self._keys]

        if needed:
            async with self._lock:
                for asset in needed:
                    if asset in self._keys or time.monotonic() < self._retry_at.get(asset, 0.0):
                        continue
                    self._retry_at[asset] = time.monotonic() + 60
                    filename = _ASSETS[asset][0]
                    try:
                        key = await asyncio.wait_for(
                            upload(str(Path(__file__).parent / "assets" / filename)), timeout=2,
                        )
                    except (TimeoutError, OSError):
                        key = None
                    if key:
                        self._keys[asset] = key

        def replace(value):
            if isinstance(value, dict):
                asset = value.get("img_key")
                if asset in _ASSETS:
                    key = self._keys.get(asset)
                    if key:
                        return {**value, "img_key": key}
                    return dict(_ASSETS[asset][1])
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return value

        return replace(payload)
