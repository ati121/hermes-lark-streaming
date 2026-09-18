"""Process-global state shared across the plugin's per-profile module copies.

Hermes' multiplexing gateway serves every profile from one process and loads a
*directory* plugin once per served profile: ``hermes_cli/plugins_loader.py``
imports ``hermes_plugins.hermes_lark_streaming`` for the first home and
``hermes_plugins.hermes_lark_streaming__home_<digest>`` for every other one.
This package therefore exists N times in a single interpreter, while everything
it patches (``gateway.run``, ``feishu_platform.adapter``, ``agent.conversation_loop``)
exists exactly once.

Module-level mutable state is therefore per-copy, which is wrong for anything
that must be *process-wide* — most importantly the per-home controller registry:
two copies each building their own controller for the same profile defeats the
controller's own "same message id ⇒ same session" dedup and the profile's cards
get created twice (observed on NAS as 3× ``feishu inbound ids`` / 3×
``HLS: session created`` / ``3× 230002``).

:func:`shared_store` anchors such state on a host module that is imported exactly
once, so every copy sees the same dict.  When no host module is importable yet
(unit tests, standalone CLI) it returns a process-local fallback and the plugin
behaves exactly as it did before multiplexing existed.
"""

from __future__ import annotations

import sys
from typing import Any

# One anchor is enough, but the first host module that is already imported wins;
# all copies of the plugin run inside the same process, so they converge on the
# same host object regardless of which copy asks first.
_ANCHOR_MODULES = (
    "hermes_constants",
    "gateway.run",
    "agent.secret_scope",
)

# Attribute planted on the anchor module holding {name: object}.
_ANCHOR_ATTR = "__hls_shared_store__"

# Fallback store, used when no anchor module is available (tests / CLI).
_LOCAL_FALLBACK: dict[str, Any] = {}


def shared_store(name: str) -> Any | None:
    """Return the process-wide store for ``name`` (a dict), or ``None``.

    ``None`` means no host anchor is importable, so the caller should keep using
    its own module-level state.
    """
    for module_name in _ANCHOR_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        store = getattr(module, _ANCHOR_ATTR, None)
        if not isinstance(store, dict):
            store = {}
            try:
                setattr(module, _ANCHOR_ATTR, store)
            except (AttributeError, TypeError):  # pragma: no cover - exotic module proxy
                continue
        bucket = store.get(name)
        if not isinstance(bucket, dict):
            bucket = {}
            store[name] = bucket
        return bucket
    return None


def shared_dict(name: str, local: dict) -> dict:
    """Return the shared dict for ``name``, or ``local`` when unsupported.

    Note the explicit ``is None`` check: an EMPTY shared bucket is still the
    right answer (``or`` would fall back to the per-copy dict exactly while the
    registry is still empty, which is the common case).
    """
    store = shared_store(name)
    return local if store is None else store


def anchor_module_name() -> str:
    """Diagnostics: which host module currently anchors the shared store."""
    for module_name in _ANCHOR_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and isinstance(getattr(module, _ANCHOR_ATTR, None), dict):
            return module_name
    return ""


def shared_object(name: str, factory: Any) -> Any:
    """Return the process-wide object ``name``, creating it with ``factory()`` once.

    Used for cross-copy locks.  Falls back to a fresh ``factory()`` result when
    no host anchor is available (each copy then gets its own lock, which is the
    pre-multiplex behaviour).
    """
    store = shared_store(name)
    if store is None:
        return factory()
    key = "__hls_shared_object__"
    if key not in store:
        store[key] = factory()
    return store[key]
