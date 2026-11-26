"""飞书 Open API 客户端 — 基于 lark-oapi SDK."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import re
import time as _time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..cardkit.budget import CARD_ELEMENT_LIMIT, CARD_HARD_LIMIT_BYTES, count_elements
from ..config.reader import FEISHU_OPEN_API_BASE

# v1.3.4 fix (P1): lark_oapi SDK 的 Transport.aexecute 不捕获网络异常，
try:
    import httpx
    _NETWORK_ERROR_BASES: tuple = (httpx.RequestError,)
    _PRE_REQUEST_ERRORS: tuple = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
except ImportError:  # 极端情况下 httpx 不可用（lark_oapi 依赖它，不应发生）
    _NETWORK_ERROR_BASES = ()
    _PRE_REQUEST_ERRORS = ()
try:
    import requests
    _NETWORK_ERROR_BASES += (requests.exceptions.RequestException,)
    _PRE_REQUEST_ERRORS += (requests.exceptions.ConnectTimeout,)
except ImportError:
    pass
try:
    from lark_oapi.core.exception import ObtainAccessTokenException
    _TOKEN_ERROR_BASES: tuple = (ObtainAccessTokenException,)
except ImportError:
    _TOKEN_ERROR_BASES = ()

_logger = logging.getLogger("hermes_lark_streaming")

def _sanitize_message(msg: str) -> str:
    """从错误消息中移除 token 和 secret."""
    msg = re.sub(r'(tenant_access_token["\s:=]+)([A-Za-z0-9_-]{10,})', r"\1***", msg)
    msg = re.sub(r'(app_secret["\s:=]+)([A-Za-z0-9]{10,})', r"\1***", msg)
    msg = re.sub(r"(Bearer\s+)([A-Za-z0-9_-]{10,})", r"\1***", msg)
    return msg

class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带 API 错误码和 log_id."""

    def __init__(
        self, message: str, code: int = 0, log_id: str = "", *, retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        # v1.3.4: 飞书 log_id，用于开放平台后台排查请求链路
        self.log_id = log_id
        self.retry_after = retry_after

    def __str__(self) -> str:
        base = super().__str__()
        if self.log_id:
            return f"{base} [log_id={self.log_id}]"
        return base

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码."""
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None

    def extract_schema_detail(self) -> str:
        """从 300315 Schema 错误中提取具体非法属性信息."""
        msg = str(self)
        # 尝试匹配 "unknown property 'X' on 'Y'" 模式
        m = re.search(r"unknown property '(\w+)'.*?'(\w+)'", msg)
        if m:
            return f"unknown property '{m.group(1)}' on '{m.group(2)}'"
        # 尝试匹配 "property 'X' is not allowed" 模式
        m = re.search(r"property '(\w+)'.*?not allowed.*?tag '?(\w+)'?", msg)
        if m:
            return f"property '{m.group(1)}' not allowed on '{m.group(2)}'"
        # 尝试匹配 "invalid.*property.*'X'" 模式
        m = re.search(r"(unknown property '[^']+'[^.]*)", msg)
        if m:
            return m.group(1)
        # 兜底：返回完整消息
        return msg[:200]


class CardKitAmbiguousWrite(FeishuAPIError):
    """A CardKit write was sent and may have committed without an acknowledgement."""


class MessageDeliveryUncertain(FeishuAPIError):
    """An IM send, reply, or edit may have succeeded without acknowledgement."""


CARDKIT_CONTENT_FAILED = 230099  # 卡片内容创建失败（通用码，需检查子错误）
CARDKIT_ELEMENT_LIMIT = 11310  # 子码: 元素/表格超限，或卡片交互锁定 (card action is lock)
CARDKIT_TAG_IMMUTABLE = 300312  # 更新组件属性时不能修改 tag
CARDKIT_SCHEMA_ERROR = 300315  # 卡片 Schema 非法属性 (unknown property) OR element not found for insert_before
CARDKIT_STREAMING_CLOSED = 300309  # 卡片流式模式已关闭
CARDKIT_STREAMING_TIMEOUT = 200850  # 流式更新文本接口：流式模式超时自动关闭（10 分钟）
CARDKIT_STREAMING_ADD_TIMEOUT = 200510  # 新增组件接口：流式模式超时自动关闭
CARDKIT_STREAMING_CLOSED_CODES = frozenset({
    CARDKIT_STREAMING_CLOSED,
    CARDKIT_STREAMING_TIMEOUT,
    CARDKIT_STREAMING_ADD_TIMEOUT,
})
CARDKIT_INTERACTION_LOCKED = 200810  # 卡片正在交互回调中，暂时无法更新
CARDKIT_SEQUENCE_CONFLICT = 300317  # sequence 冲突
CARDKIT_ELEMENT_NOT_FOUND = 300313  # 更新组件属性失败（常见于 add_elements 后服务端尚未持久化时的竞态）
CARDKIT_ELEMENT_NOT_FOUND_ALT = 300314  # delete_elements 不存在的元素
MSG_NOT_FOUND = 1000023  # 消息不存在/已删除
# IM 通道频控（含单条消息更新过于频繁）。
# 与 CardKit 那组码不同源，故不放进 CARDKIT_TRANSIENT_CODES —— 详见
# _retry_transient 的 frequency_limit_delays 参数。
IM_FREQUENCY_LIMIT = 230020
IM_FREQUENCY_LIMITS = frozenset({IM_FREQUENCY_LIMIT, 230429})

# v1.3.1 fix: 300315 错误码有两种含义：
_RE_ELEMENT_NOT_FOUND = re.compile(r"not find elementID", re.IGNORECASE)
_RE_CARD_ACTION_LOCKED = re.compile(r"card action is lock", re.IGNORECASE)
_RE_CARD_LIMIT_EXCEEDED = re.compile(r"(?:element|table)[^;\n]*limit", re.IGNORECASE)

# 参考 Cheerwhy / openclaw-lark: 这组错误码是飞书 CardKit 的瞬态错误，
# v1.3.4 fix (P1): 新增 99991400 (接口频率限制) — 飞书开放平台对单个 API
# CardKit 2.0: 新增 200810（卡片交互中锁定）与 300120（服务端内部瞬态错误）
CARDKIT_TRANSIENT_CODES = {
    2200,                        # CardKit 内部超时
    1663,                        # CardKit 服务端瞬态错误
    300000,                      # CardKit 通用内部错误
    300120,                      # CardKit Server Internal Error（官方建议稍后重试）
    CARDKIT_INTERACTION_LOCKED,  # 200810: 卡片正在交互回调中，等待后可重试
    99991400,                    # 接口频率限制（per-API rate limit，HTTP 400）
}

# 瞬态错误重试策略 — 指数退避
_TRANSIENT_RETRY_DELAYS = (0.1, 0.3, 0.6)  # 3 次重试，递增延迟
_TRANSIENT_MAX_RETRIES = len(_TRANSIENT_RETRY_DELAYS)

# IM rejection windows can last tens of seconds. Keep this budget separate
# from short network/CardKit retries so streaming updates never block on it.
_FREQUENCY_LIMIT_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
_APP_FREQUENCY_LIMITS = frozenset({99991400, 429})
_APP_FREQUENCY_LIMIT_RETRY_DELAYS = (1.0, 2.0, 4.0)
_IM_UPDATE_MIN_INTERVAL = 0.25  # Below the documented per-message 5 QPS ceiling.
_IM_UPDATE_COOLDOWN = 12.0
_IM_UPDATE_MAX_COOLDOWN = 60.0
_IM_UPDATE_HISTORY_SECONDS = 60.0

_ELEMENT_NOT_FOUND_RETRY_DELAYS = (0.2, 0.2, 0.2)
_ELEMENT_NOT_FOUND_MAX_RETRIES = len(_ELEMENT_NOT_FOUND_RETRY_DELAYS)
_CARDKIT_WRITES_PER_SECOND = 10
_UNACKNOWLEDGED_WRITE_CODES = frozenset({2200, 1663, 300000})


def _validate_cardkit_length(operation: str, value: str, max_length: int) -> None:
    if not 1 <= len(value) <= max_length:
        raise FeishuAPIError(f"{operation}: payload must contain 1 to {max_length} characters")


def _validate_cardkit_sequence(operation: str, sequence: int) -> None:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 2_147_483_647:
        raise FeishuAPIError(f"{operation}: sequence must be an integer between 1 and 2147483647")


def _may_have_committed(error: Exception) -> bool:
    if isinstance(error, FeishuAPIError):
        return error.code in _UNACKNOWLEDGED_WRITE_CODES
    if isinstance(error, _NETWORK_ERROR_BASES):
        return not isinstance(error, _PRE_REQUEST_ERRORS)
    return isinstance(error, json.JSONDecodeError)


def _unclear_write_response(response: Any) -> bool:
    status = getattr(getattr(response, "raw", None), "status_code", None)
    if status == 429:
        return False  # Explicit rejection, even when the gateway omits an API code.
    return getattr(response, "code", None) is None or (isinstance(status, int) and status >= 500)


def _unacknowledged_write(
    error_type: type[FeishuAPIError], operation: str, error: Exception,
) -> FeishuAPIError:
    if isinstance(error, FeishuAPIError):
        return error_type(
            f"commit not acknowledged: {error.args[0]}", error.code, error.log_id,
            retry_after=error.retry_after,
        )
    return error_type(f"{operation}: commit not acknowledged ({type(error).__name__})")


def _rate_limit_retry_after(response: Any) -> float | None:
    """Read the provider's relative seconds or HTTP-date, without trusting bad hints."""
    headers = getattr(getattr(response, "raw", None), "headers", None)
    try:
        values = {str(key).lower(): value for key, value in headers.items()}
    except (AttributeError, TypeError, ValueError):
        return None
    for name in ("x-ogw-ratelimit-reset", "retry-after"):
        value = values.get(name)
        if value is None:
            continue
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(str(value)).timestamp() - _time.time()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


class _IMUpdateState:
    """One dispatch lock, cooldown and bounded attempt history per IM message."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active = 0
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.rejections = 0
        self.attempted_at: deque[float] = deque()
        self.expiry: asyncio.TimerHandle | None = None

    async def wait_for_slot(
        self,
        endpoint_cooldown: Callable[[], float],
        *,
        should_update: Callable[[], bool] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if should_update is not None and not should_update():
                return
            delay = max(self.next_at, self.cooldown_until, endpoint_cooldown()) - loop.time()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def record_attempt(self, now: float) -> None:
        while self.attempted_at and self.attempted_at[0] + _IM_UPDATE_HISTORY_SECONDS <= now:
            self.attempted_at.popleft()
        self.attempted_at.append(now)

    def reject(self, now: float, retry_after: float | None) -> float:
        self.rejections += 1
        delay = min(_IM_UPDATE_COOLDOWN * 2 ** min(self.rejections - 1, 3), _IM_UPDATE_MAX_COOLDOWN)
        delay = max(delay, retry_after or 0.0)
        self.cooldown_until = max(self.cooldown_until, now + delay)
        return self.cooldown_until - now


class _CardKitWriteState:
    """One FIFO lock and a rolling budget shared by all writes to a card."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active = 0
        self.finished_at: deque[float] = deque()
        self.expiry: asyncio.TimerHandle | None = None

    async def wait_for_slot(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            while self.finished_at and self.finished_at[0] + 1.0 <= now:
                self.finished_at.popleft()
            if len(self.finished_at) < _CARDKIT_WRITES_PER_SECOND:
                return
            await asyncio.sleep(self.finished_at[0] + 1.0 - now)


def is_schema_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为卡片 Schema 非法属性/非法 tag 变更错误。"""
    if e.code == CARDKIT_TAG_IMMUTABLE:
        return True
    if e.code != CARDKIT_SCHEMA_ERROR:
        return False
    # 排除 "not find elementID" 的情况——这是 element not found，不是 schema error
    if _RE_ELEMENT_NOT_FOUND.search(str(e)):
        return False
    return True


def is_streaming_closed_error(e: FeishuAPIError) -> bool:
    """判断 FeishuAPIError 是否为 CardKit 流式模式已关闭或超时自动关闭。"""
    return e.code in CARDKIT_STREAMING_CLOSED_CODES


def is_card_limit_sub_error(e: FeishuAPIError) -> bool:
    """判断 230099/11310 是否属于卡片元素/表格超限（排除 card action is lock 与模板权限）。"""
    if e.code != CARDKIT_CONTENT_FAILED or e.extract_sub_code() != CARDKIT_ELEMENT_LIMIT:
        return False
    msg = str(e)
    if _RE_CARD_ACTION_LOCKED.search(msg):
        return False
    if "template is not visible" in msg.lower():
        return False
    # 兼容未携带标准 ErrMsg 文本的简写报错（仅 11310）以及 element/table 超限
    return _RE_CARD_LIMIT_EXCEEDED.search(msg) is not None or "ErrMsg:" not in msg


def is_element_not_found_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为"元素不存在"错误。"""
    if e.code == CARDKIT_ELEMENT_NOT_FOUND:
        return True
    if e.code == CARDKIT_ELEMENT_NOT_FOUND_ALT:
        return True
    # 300315 + "not find elementID" = insert_before 引用不存在的元素
    if e.code == CARDKIT_SCHEMA_ERROR and _RE_ELEMENT_NOT_FOUND.search(str(e)):
        return True
    return False

@dataclass(frozen=True)
class FeishuClientConfig:
    app_id: str
    app_secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id.strip():
            raise ValueError("app_id is required")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip():
            raise ValueError("app_secret is required")

def _is_frequency_limit(e: FeishuAPIError) -> bool:
    """230020/230429: 飞书 IM 接口频控（含单条消息更新过于频繁）.

    这个码默认不重试 —— 它的成因是"这一秒发太多了"，而插件最密集的调用
    恰恰是流式刷新：那些渲染下一轮就会被更新的内容覆盖，为它们退避等待
    只会把刷新队列越堆越长。只有终态封口那种"一次机会"的调用才值得等。
    """
    return e.code in IM_FREQUENCY_LIMITS


is_frequency_limit = _is_frequency_limit

def _is_transient_error(e: FeishuAPIError) -> bool:
    """判断 FeishuAPIError 是否为 CardKit 瞬态错误（可重试）."""
    if e.code in CARDKIT_TRANSIENT_CODES:
        return True
    # 230099 下绝大部分子码（100290/200380/200381/200550/200570/200621/200732/200861/200914）
    # 都是永久性内容校验错误，仅 11310 + "card action is lock"（交互回调锁定中）为瞬态可重试。
    if e.code == CARDKIT_CONTENT_FAILED and e.extract_sub_code() == CARDKIT_ELEMENT_LIMIT:
        return _RE_CARD_ACTION_LOCKED.search(str(e)) is not None
    return False

class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK."""

    def __init__(self, config: FeishuClientConfig) -> None:
        from .branding import BrandAssets

        self._brand_assets = BrandAssets()
        self.config = config
        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .domain(FEISHU_OPEN_API_BASE.removesuffix("/open-apis"))
            .build()
        )
        self._use_async_stream_element = callable(
            getattr(self._client.cardkit.v1.card_element, 'acontent', None)
        )

    async def _retry_transient(
        self,
        operation: str,
        coro_factory: Callable[[], Any],
        *,
        max_retries: int | None = None,
        frequency_limit_delays: tuple[float, ...] | None = None,
    ) -> Any:
        """执行协程，遇到 CardKit 瞬态错误时自动重试.

        frequency_limit_delays: 只有显式传入时，IM 频控（230020/230429）才算可重试，
        并按这条更长的序列退避；默认 None 表示频控直接抛出。这个默认值是有意
        的 —— 流式刷新撞上限流时应该放弃本轮，让下一轮渲染覆盖掉，而不是在
        热路径上阻塞几秒把刷新越堆越多（见 _is_frequency_limit 的注释）。
        """
        transient_limit = _TRANSIENT_MAX_RETRIES if max_retries is None else max_retries
        frequency_limit = (
            len(frequency_limit_delays)
            if max_retries is None and frequency_limit_delays
            else transient_limit
        )
        last_error: FeishuAPIError | None = None
        for attempt in range(max(transient_limit, frequency_limit) + 1):
            try:
                result = await coro_factory()
                # v1.1.0: Record successful API call
                try:
                    from ..aowen import record_api_call
                    record_api_call(operation)
                except Exception:
                    _logger.debug("metrics: record_api_call failed", exc_info=True)
                return result
            except FeishuAPIError as e:
                last_error = e
                if _is_frequency_limit(e) and frequency_limit_delays:
                    delays: tuple[float, ...] = frequency_limit_delays
                    retry_limit = frequency_limit
                    note = " (frequency limit)"
                elif e.code in _APP_FREQUENCY_LIMITS:
                    delays = _APP_FREQUENCY_LIMIT_RETRY_DELAYS
                    retry_limit = transient_limit
                    note = " (API frequency limit)"
                elif _is_transient_error(e):
                    delays = _TRANSIENT_RETRY_DELAYS
                    retry_limit = transient_limit
                    note = ""
                else:
                    raise
                if attempt < retry_limit:
                    delay = max(delays[min(attempt, len(delays) - 1)], e.retry_after or 0.0)
                    _logger.info(
                        "transient retry%s: %s attempt=%d/%d code=%s delay=%.2fs",
                        note, operation, attempt + 1, retry_limit, e.code, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except asyncio.CancelledError:
                # v1.3.4: 不要吞 CancelledError，让它正常传播
                raise
            except _NETWORK_ERROR_BASES as e:
                # v1.3.4 fix (P1): 网络错误（httpx ConnectError/ReadTimeout 等）
                if await self._backoff_retry(operation, attempt, transient_limit, "network", e):
                    continue
                raise
            except _TOKEN_ERROR_BASES as e:
                # v1.3.4 fix (P1): token 刷新失败（ObtainAccessTokenException）
                if await self._backoff_retry(operation, attempt, transient_limit, "token", e):
                    continue
                raise
        raise last_error  # unreachable, but type-safe

    @staticmethod
    async def _backoff_retry(operation: str, attempt: int, limit: int, label: str, error: BaseException) -> bool:
        """Sleep one transient-retry backoff step; False when attempts ran out."""
        if attempt >= limit:
            return False
        delay = _TRANSIENT_RETRY_DELAYS[min(attempt, len(_TRANSIENT_RETRY_DELAYS) - 1)]
        _logger.info(
            "transient retry (%s): %s attempt=%d/%d error=%s delay=%.2fs",
            label, operation, attempt + 1, limit, type(error).__name__, delay,
        )
        await asyncio.sleep(delay)
        return True

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        """检查 SDK 响应，失败时抛出 FeishuAPIError（携带 log_id）."""
        status = getattr(getattr(response, "raw", None), "status_code", None)
        if not response.success() or status == 429:
            # The gateway's status applies to the whole endpoint even when its
            # body contains a message-specific or otherwise unrelated API code.
            code = 429 if status == 429 else response.code or 0
            msg = response.msg or ""
            # 返回 None（SDK bug），需从 response.error dict 兜底提取。
            log_id = ""
            try:
                log_id = response.get_log_id() or ""
            except Exception:
                _logger.debug("response.get_log_id failed", exc_info=True)
            if not log_id:
                # SDK async 路径 bug 兜底：error 是 dict，含 log_id 字段
                err = getattr(response, 'error', None)
                if isinstance(err, dict):
                    log_id = err.get("log_id", "") or ""
                elif isinstance(err, str):
                    # error 可能是 JSON 字符串或含 log_id 的文本
                    m = re.search(r'log_id["\']?\s*[:=]\s*["\']?([A-Za-z0-9]{20,})', err)
                    if m:
                        log_id = m.group(1)
            # v1.1.0: Record API error metrics
            try:
                from ..aowen import record_api_error
                record_api_error(code, operation)
            except Exception:
                _logger.debug("metrics: record_api_error failed", exc_info=True)
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
                log_id=log_id,
                retry_after=(
                    _rate_limit_retry_after(response)
                    if code in IM_FREQUENCY_LIMITS or code in _APP_FREQUENCY_LIMITS or status == 429
                    else None
                ),
            )

    @staticmethod
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def _card_json(self, payload: Any) -> str:
        resolved = await self._brand_assets.resolve(payload, self.upload_local_image)
        content = self._dumps(resolved)
        # CardKit batch actions are a list, not a complete card. Their own
        # field-length check still applies, while whole-card limits belong to
        # the rendered JSON after bundled image markers have been resolved.
        if isinstance(resolved, dict) and any(key in resolved for key in ("schema", "body", "elements")):
            if len(content.encode("utf-8")) > CARD_HARD_LIMIT_BYTES:
                raise ValueError(f"complete card exceeds {CARD_HARD_LIMIT_BYTES} UTF-8 bytes")
            if count_elements(resolved) > CARD_ELEMENT_LIMIT:
                raise ValueError(f"complete card exceeds {CARD_ELEMENT_LIMIT} tagged elements")
        return content

    @asynccontextmanager
    async def _serialize_im_update(self, message_id: str) -> AsyncIterator[_IMUpdateState]:
        states: dict[str, _IMUpdateState] = vars(self).setdefault("_im_update_states", {})
        state = states.get(message_id)
        if state is None:
            state = states[message_id] = _IMUpdateState()
        if state.expiry is not None:
            state.expiry.cancel()
            state.expiry = None
        state.active += 1
        try:
            async with state.lock:
                yield state
        finally:
            state.active -= 1
            if state.active == 0:
                if state.attempted_at:
                    expires_at = max(
                        state.attempted_at[-1] + _IM_UPDATE_HISTORY_SECONDS,
                        state.cooldown_until, state.next_at,
                    )
                    state.expiry = asyncio.get_running_loop().call_at(expires_at, states.pop, message_id, None)
                else:
                    states.pop(message_id, None)

    @asynccontextmanager
    async def _serialize_cardkit_write(self, card_id: str) -> AsyncIterator[_CardKitWriteState]:
        states: dict[str, _CardKitWriteState] = vars(self).setdefault("_cardkit_write_states", {})
        state = states.get(card_id)
        if state is None:
            state = states[card_id] = _CardKitWriteState()
        if state.expiry is not None:
            state.expiry.cancel()
            state.expiry = None
        state.active += 1
        try:
            # Join before building the request. Image resolution may await while
            # another caller already has the next sequence number for this card.
            async with state.lock:
                yield state
        finally:
            state.active -= 1
            if state.active == 0:
                if state.finished_at:
                    state.expiry = asyncio.get_running_loop().call_at(
                        state.finished_at[-1] + 1.0, states.pop, card_id, None,
                    )
                else:
                    states.pop(card_id, None)

    async def _send_cardkit_write(
        self,
        state: _CardKitWriteState,
        operation: str,
        request: Any,
        send: Callable[[Any], Awaitable[Any]],
        *,
        retry_missing_element: bool = False,
    ) -> None:
        uncertain = False
        loop = asyncio.get_running_loop()

        async def attempt() -> None:
            nonlocal uncertain
            await state.wait_for_slot()
            try:
                response = await send(request)
                unclear_response = _unclear_write_response(response)
                uncertain |= unclear_response
                self._check(response, operation)
                if unclear_response:
                    raise FeishuAPIError(f"{operation}: invalid write acknowledgement")
            except Exception as error:
                uncertain |= _may_have_committed(error)
                raise
            finally:
                # The SDK may obtain a token or connect before transmitting.
                # Counting until one second after each attempt ends therefore
                # also bounds the actual rolling window on the wire.
                state.finished_at.append(loop.time())

        try:
            retry_count = _ELEMENT_NOT_FOUND_MAX_RETRIES if retry_missing_element else 0
            for retry in range(retry_count + 1):
                try:
                    await self._retry_transient(operation, attempt)
                    return
                except FeishuAPIError as error:
                    if uncertain or retry == retry_count or not is_element_not_found_error(error):
                        raise
                    await asyncio.sleep(_ELEMENT_NOT_FOUND_RETRY_DELAYS[retry])
        except Exception as error:
            if uncertain:
                # Feishu documents 200770 as a UUID collision and 300317 as
                # a sequence conflict. Neither confirms that the first attempt
                # applied. In particular, don't replay inserts with a new UUID.
                raise _unacknowledged_write(CardKitAmbiguousWrite, operation, error) from error
            raise

    async def _deliver_message(self, operation: str, request: Any, send: Callable) -> str:
        # Both IM create/reply endpoints deduplicate by body.uuid. Keep one key
        # across attempts, including a read timeout after Feishu accepted the
        # message, so transport retries cannot create duplicate cards/replies.
        if not request.request_body.uuid:
            request.request_body.uuid = uuid4().hex
        uncertain = False

        async def _do() -> str:
            nonlocal uncertain
            try:
                resp = await send(request)
                unclear_response = _unclear_write_response(resp)
                uncertain |= unclear_response
                self._check(resp, operation)
                if unclear_response:
                    raise FeishuAPIError(f"{operation}: invalid send acknowledgement")
            except Exception as error:
                uncertain |= _may_have_committed(error)
                raise
            if resp.data and resp.data.message_id:
                return str(resp.data.message_id)
            raise MessageDeliveryUncertain(f"{operation}: response missing message_id")

        try:
            return await self._retry_transient(operation, _do)
        except MessageDeliveryUncertain:
            raise
        except Exception as error:
            if uncertain:
                raise _unacknowledged_write(MessageDeliveryUncertain, operation, error) from error
            raise

    async def _create_message(self, operation: str, request: Any) -> str:
        return await self._deliver_message(operation, request, self._client.im.v1.message.acreate)

    async def _reply_message(self, operation: str, request: Any) -> str:
        return await self._deliver_message(operation, request, self._client.im.v1.message.areply)

    async def send_card_to_chat(self, chat_id: str, card: dict[str, Any]) -> str:
        """发送独立卡片到聊天（非回复），返回 message_id."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(await self._card_json(card))
                .build()
            )
            .build()
        )
        return await self._create_message("send_card_to_chat", request)

    async def send_card_by_id_to_chat(self, chat_id: str, card_id: str) -> str:
        """发送 CardKit 实体到聊天（非回复），返回 message_id."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .build()
            )
            .build()
        )
        return await self._create_message("send_card_by_id_to_chat", request)

    async def send_text_to_chat(self, chat_id: str, text: str) -> str:
        """Send visible fallback text when there is no platform reply anchor."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(self._dumps({"text": text}))
                .build()
            )
            .build()
        )
        return await self._create_message("send_text_to_chat", request)

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        """回复消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(ReplyMessageRequestBody.builder().msg_type("interactive").content(await self._card_json(card)).build())
            .build()
        )
        return await self._reply_message("reply_card", request)

    async def reply_text(self, message_id: str, text: str) -> str:
        """回复纯文本消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(self._dumps({"text": text}))
                .build()
            )
            .build()
        )
        return await self._reply_message("reply_text", request)

    async def reply_card_by_id(self, message_id: str, card_id: str) -> str:
        """通过 card_id 回复 CardKit 卡片消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .build()
            )
            .build()
        )
        return await self._reply_message("reply_card_by_id", request)

    async def update_card(
        self,
        message_id: str,
        card: dict[str, Any],
        *,
        retry_frequency_limit: bool = False,
        should_update: Callable[[], bool] | None = None,
    ) -> None:
        """PATCH 更新已发送的卡片（IM PATCH 通道）.

        retry_frequency_limit: 撞上飞书 IM 频控（230020/230429）时是否退避重试。
        流式刷新保持 False；只有终态封口那种「只有一次机会、失败就永久卡住」
        的调用才传 True。
        """
        operation = "update_card"
        uncertain_error: Exception | None = None

        async def _do() -> None:
            nonlocal uncertain_error
            # All callers and every retry share the same boundary. Controller
            # intervals alone cannot cover toggles, terminal writes or retries.
            async with self._serialize_im_update(message_id) as state:
                if should_update is not None and not should_update():
                    return
                content = await self._card_json(card)
                if should_update is not None and not should_update():
                    return
                await state.wait_for_slot(
                    lambda: vars(self).get("_im_update_endpoint_cooldown_until", 0.0),
                    should_update=should_update,
                )
                # The session may stop or replace this frame while queued,
                # resolving images, or waiting out a shared cooldown.
                if should_update is not None and not should_update():
                    return
                request = (
                    PatchMessageRequest.builder()
                    .message_id(message_id)
                    .request_body(PatchMessageRequestBody.builder().content(content).build())
                    .build()
                )
                unclear_response = False
                loop = asyncio.get_running_loop()
                attempted_at = loop.time()
                state.record_attempt(attempted_at)
                endpoint_attempts: deque[float] = vars(self).setdefault("_im_update_endpoint_attempts", deque())
                while endpoint_attempts and endpoint_attempts[0] + _IM_UPDATE_HISTORY_SECONDS <= attempted_at:
                    endpoint_attempts.popleft()
                endpoint_attempts.append(attempted_at)
                try:
                    resp = await self._client.im.v1.message.apatch(request)
                    unclear_response = _unclear_write_response(resp)
                    self._check(resp, operation)
                    if unclear_response:
                        raise FeishuAPIError(f"{operation}: invalid write acknowledgement")
                except FeishuAPIError as error:
                    if _is_frequency_limit(error) or error.code in _APP_FREQUENCY_LIMITS:
                        now = loop.time()
                        if _is_frequency_limit(error):
                            error.retry_after = state.reject(now, error.retry_after)
                        else:
                            rejections = vars(self).get("_im_update_endpoint_rejections", 0) + 1
                            self._im_update_endpoint_rejections = rejections
                            delay = max(
                                error.retry_after or 0.0,
                                _APP_FREQUENCY_LIMIT_RETRY_DELAYS[
                                    min(rejections - 1, len(_APP_FREQUENCY_LIMIT_RETRY_DELAYS) - 1)
                                ],
                            )
                            deadline = max(vars(self).get("_im_update_endpoint_cooldown_until", 0.0), now + delay)
                            self._im_update_endpoint_cooldown_until = deadline
                            error.retry_after = deadline - now
                        _logger.warning(
                            "IM PATCH rate limited: message_hash=%s code=%s retry_after=%.2fs "
                            "attempts_1s=%d attempts_60s=%d "
                            "endpoint_attempts_1s=%d endpoint_attempts_60s=%d log_id=%s",
                            hashlib.sha256(message_id.encode()).hexdigest()[:12], error.code,
                            error.retry_after or _APP_FREQUENCY_LIMIT_RETRY_DELAYS[0],
                            sum(at + 1.0 > now for at in state.attempted_at),
                            sum(at + _IM_UPDATE_HISTORY_SECONDS > now for at in state.attempted_at),
                            sum(at + 1.0 > now for at in endpoint_attempts),
                            sum(at + _IM_UPDATE_HISTORY_SECONDS > now for at in endpoint_attempts),
                            error.log_id or "-",
                        )
                    if unclear_response or _may_have_committed(error):
                        uncertain_error = error
                    raise
                except Exception as error:
                    if unclear_response or _may_have_committed(error):
                        uncertain_error = error
                    raise
                else:
                    state.rejections = 0
                    # An older in-flight success must not erase a rejection
                    # reported by another message after this attempt started.
                    if attempted_at >= vars(self).get("_im_update_endpoint_cooldown_until", 0.0):
                        self._im_update_endpoint_rejections = 0
                    uncertain_error = None
                finally:
                    # Count spacing from completion, since the SDK can obtain
                    # credentials before the actual PATCH is transmitted.
                    state.next_at = loop.time() + _IM_UPDATE_MIN_INTERVAL

        try:
            await self._retry_transient(
                operation,
                _do,
                frequency_limit_delays=(
                    _FREQUENCY_LIMIT_RETRY_DELAYS if retry_frequency_limit else None
                ),
            )
        except Exception as error:
            if uncertain_error is not None:
                raise _unacknowledged_write(MessageDeliveryUncertain, operation, error) from error
            raise
        # The validity guard may skip a retry without confirming the original PATCH.
        if uncertain_error is not None:
            raise _unacknowledged_write(MessageDeliveryUncertain, operation, uncertain_error) from uncertain_error

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        """创建 CardKit 实体，返回 card_id."""
        content = await self._card_json(card)
        _validate_cardkit_length("cardkit_create", content, 3_000_000)
        request = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(content).build())
            .build()
        )

        async def _do():
            t0 = _time.monotonic()
            resp = await self._client.cardkit.v1.card.acreate(request)
            elapsed_ms = (_time.monotonic() - t0) * 1000
            _logger.debug("perf: feishu_card_create elapsed=%.0fms", elapsed_ms)
            self._check(resp, "cardkit_create")
            if resp.data and resp.data.card_id:
                return str(resp.data.card_id)
            raise FeishuAPIError("cardkit_create: response missing card_id")

        return await self._retry_transient("cardkit_create", _do)

    async def cardkit_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 1,
    ) -> None:
        """流式更新卡片内指定 element 的内容（打字机效果）."""
        _validate_cardkit_sequence("cardkit_stream_element", sequence)
        _validate_cardkit_length("cardkit_stream_element", content, 100_000)
        async with self._serialize_cardkit_write(card_id) as state:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content).sequence(sequence).uuid(uuid4().hex).build(),
                )
                .build()
            )
            send = (
                self._client.cardkit.v1.card_element.acontent
                if self._use_async_stream_element else self._sync_stream_content
            )
            await self._send_cardkit_write(
                state, "cardkit_stream_element", request, send, retry_missing_element=True,
            )

    async def _sync_stream_content(self, request: ContentCardElementRequest) -> Any:
        # Cancelling an executor future cannot stop an in-flight synchronous
        # request. Hold the write queue until it has actually finished.
        loop = asyncio.get_running_loop()
        result = loop.run_in_executor(None, self._client.cardkit.v1.card_element.content, request)
        cancelled = False
        try:
            while True:
                try:
                    return await asyncio.shield(result)
                except asyncio.CancelledError:
                    if result.cancelled():
                        raise
                    cancelled = True
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def cardkit_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 1,
    ) -> None:
        """全量更新 CardKit 卡片."""
        _validate_cardkit_sequence("cardkit_update", sequence)
        async with self._serialize_cardkit_write(card_id) as state:
            content = await self._card_json(card)
            _validate_cardkit_length("cardkit_update", content, 1_000_000)
            body = (
                UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(content).build())
                .sequence(sequence).uuid(uuid4().hex).build()
            )
            request = UpdateCardRequest.builder().card_id(card_id).request_body(body).build()
            await self._send_cardkit_write(
                state, "cardkit_update", request, self._client.cardkit.v1.card.aupdate,
            )

    async def cardkit_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 1,
    ) -> None:
        """局部更新 CardKit 卡片（增删改组件）."""
        _validate_cardkit_sequence("cardkit_batch_update", sequence)
        async with self._serialize_cardkit_write(card_id) as state:
            content = await self._card_json(actions)
            _validate_cardkit_length("cardkit_batch_update", content, 1_000_000)
            body = (
                BatchUpdateCardRequestBody.builder().sequence(sequence)
                .actions(content).uuid(uuid4().hex).build()
            )
            request = BatchUpdateCardRequest.builder().card_id(card_id).request_body(body).build()
            await self._send_cardkit_write(
                state, "cardkit_batch_update", request, self._client.cardkit.v1.card.abatch_update,
            )

    @staticmethod
    def _summary_settings(summary: str) -> dict[str, Any]:
        truncated = summary[:120]
        return {
            "content": truncated,
            "i18n_content": {"zh_cn": truncated, "en_us": truncated},
        }

    async def _apply_card_settings(self, card_id: str, settings: dict[str, Any], operation: str, *, sequence: int = 1) -> None:
        _validate_cardkit_sequence(operation, sequence)
        async with self._serialize_cardkit_write(card_id) as state:
            content = self._dumps(settings)
            _validate_cardkit_length(operation, content, 100_000)
            body = (
                SettingsCardRequestBody.builder().settings(content)
                .sequence(sequence).uuid(uuid4().hex).build()
            )
            request = SettingsCardRequest.builder().card_id(card_id).request_body(body).build()
            await self._send_cardkit_write(
                state, operation, request, self._client.cardkit.v1.card.asettings,
            )

    async def cardkit_close_streaming(
        self,
        card_id: str,
        sequence: int = 1,
        *,
        summary: str = "",
    ) -> None:
        """关闭 CardKit 卡片的流式模式，并可选更新会话摘要."""
        settings: dict[str, Any] = {
            "config": {
                "streaming_mode": False,
            }
        }
        if summary:
            settings["config"]["summary"] = self._summary_settings(summary)
        await self._apply_card_settings(card_id, settings, "cardkit_close_streaming", sequence=sequence)

    async def cardkit_update_summary(
        self,
        card_id: str,
        summary: str,
        *,
        sequence: int = 1,
    ) -> None:
        """Update the card summary text WITHOUT closing streaming mode."""
        if not summary:
            return
        settings: dict[str, Any] = {
            "config": {"summary": self._summary_settings(summary)},
        }
        await self._apply_card_settings(card_id, settings, "cardkit_update_summary", sequence=sequence)

    async def _upload_image_file(self, image_file: io.BytesIO) -> str | None:
        request = (
            CreateImageRequest.builder()
            .request_body(CreateImageRequestBody.builder().image_type("message").image(image_file).build())
            .build()
        )
        resp = await self._client.im.v1.image.acreate(request)
        if resp.success() and resp.data and resp.data.image_key:
            return str(resp.data.image_key)
        return None

    async def upload_local_image(self, image_path: str) -> str | None:
        """Upload a local image file to Feishu and return the img_key."""
        import os
        try:
            if not os.path.exists(image_path):
                return None
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            image_file = io.BytesIO(image_bytes)
            image_file.name = os.path.basename(image_path)
            return await self._upload_image_file(image_file)
        except Exception:
            _logger.debug("local image upload failed for %s", image_path, exc_info=True)
            return None
