"""读取 Hermes 配置. 刷新: /aowen config reload 或重启网关. 不做 mtime 检测."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

_logger = logging.getLogger("hermes_lark_streaming")

CARD_TEXT_SIZE_VALUES = frozenset(
    {
        "heading-0", "heading-1", "heading-2", "heading-3", "heading-4",
        "heading", "normal", "notation", "xxxx-large", "xxx-large",
        "xx-large", "x-large", "large", "medium", "small", "x-small",
    }
)
CARD_TEXT_SIZE_DEFAULTS = {
    "body": "normal",
    "reasoning": "small",
    "tool": "x-small",
    "notice": "x-small",
    "footer": "x-small",
}
CARD_TEXT_SIZE_DEVICE_KEYS = frozenset({"default", "pc", "mobile"})


def _normalize_text_size_value(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a supported text size")
    normalized = value.strip()
    if normalized not in CARD_TEXT_SIZE_VALUES:
        raise ValueError(f"{path} must be a supported text size")
    return normalized


def normalize_text_sizes(
    value: object,
    *,
    path: str = "hermes_lark_streaming.text_sizes",
) -> dict[str, str | dict[str, str]]:
    """Validate role sizes and resolve device mappings to complete triples."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")

    normalized: dict[str, str | dict[str, str]] = {}
    for raw_role, raw_size in value.items():
        role = str(raw_role)
        role_path = f"{path}.{role}"
        if role not in CARD_TEXT_SIZE_DEFAULTS:
            raise ValueError(f"{role_path} is not a supported text size role")
        if isinstance(raw_size, str):
            normalized[role] = _normalize_text_size_value(raw_size, role_path)
            continue
        if not isinstance(raw_size, Mapping) or not raw_size:
            raise ValueError(f"{role_path} must be a text size or non-empty mapping")

        device_values: dict[str, str] = {}
        for raw_device, raw_device_size in raw_size.items():
            device = str(raw_device)
            device_path = f"{role_path}.{device}"
            if device not in CARD_TEXT_SIZE_DEVICE_KEYS:
                raise ValueError(f"{device_path} is not a supported device field")
            device_values[device] = _normalize_text_size_value(
                raw_device_size, device_path,
            )

        fallback = device_values.get("default", CARD_TEXT_SIZE_DEFAULTS[role])
        normalized[role] = {
            "default": fallback,
            "pc": device_values.get("pc", fallback),
            "mobile": device_values.get("mobile", fallback),
        }
    return normalized


FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
LARK_OPEN_API_BASE = "https://open.larksuite.com/open-apis"
DEFAULT_OPEN_API_BASE = FEISHU_OPEN_API_BASE


def hermes_home() -> Path:
    """当前 Hermes home 的唯一来源.

    优先级: ``hermes_constants.get_hermes_home()`` (multiplex 下由
    ``_profile_runtime_scope`` 按 profile 安装的 context-local override) →
    ``HERMES_HOME`` 环境变量 → ``~/.hermes``.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    try:
        home = get_hermes_home()
    except (AttributeError, TypeError, OSError, ValueError, RuntimeError):
        # Host API shape change / unresolvable home: fall back to the env var.
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return Path(home)


def _get_secret(name: str) -> str:
    """读取凭据: 优先 Hermes secret scope (fail-closed), 仅在无该 API 时回退 os.environ.

    multiplex 下 ``agent.secret_scope.get_secret`` 会读当前 profile 的 ``.env``
    scope, 绝不借用 ``os.environ`` (那是启动 profile 的凭据) —— 见
    ``gateway/AGENTS.md`` §Profile scope. 无 scope 且 multiplex 开启时它会抛
    ``UnscopedSecretError``, 由调用方的 scope 兜底负责, 这里不吞异常.
    """
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]
    except ImportError:
        return os.environ.get(name, "")
    return get_secret(name, "") or ""


def _get_hermes_config_path(home: Path | None = None) -> Path:
    """Hermes 主配置路径, 可绑定到指定 profile home."""
    return (Path(home) if home is not None else hermes_home()) / "config.yaml"

_RELOAD_CACHE_TTL = 60.0  # 运行时可变配置缓存 TTL.

def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return val != 0
    return default

def _to_int(val: Any, default: int) -> int:
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # int(float('inf')/'nan') raises OverflowError/ValueError.
        try:
            return int(val)
        except (OverflowError, ValueError):
            _logger.warning("HLS: config float value %r cannot convert to int, using default %d", val, default)
            return default
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            _logger.warning("HLS: config value %r is not a valid int, using default %d", val, default)
            return default
    return default

def _to_float(val: Any, default: float) -> float:
    """安全 float 转换, 拒绝 nan/inf (NaN 破坏 max/min 比较节流逻辑)."""
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            _logger.warning("HLS: config float value %r is nan/inf, using default %f", val, default)
            return default
        return result
    if isinstance(val, str):
        try:
            result = float(val)
        except ValueError:
            _logger.warning("HLS: config value %r is not a valid float, using default %f", val, default)
            return default
        if math.isnan(result) or math.isinf(result):
            _logger.warning("HLS: config float value %r is nan/inf, using default %f", val, default)
            return default
        return result
    return default

class Config:
    """插件配置, 惰性读取.

    ``Config(home)`` 绑定某个 Hermes home (multiplex 下每个 profile 一份),
    ``Config()`` 走 :func:`hermes_home()` (当前 profile/进程 home). 绑定 home 的
    实例不做单例共享 —— 同一进程可能同时服务多个 profile.
    """

    _instance: "Config | None" = None
    # v1.6.26: class-level generation.  ``reload()`` bumps it and every
    # instance (the unbound singleton AND each per-profile bound instance a
    # controller holds) drops its caches on the next read.  A bound instance
    # deliberately bypasses ``_instance``, so a singleton-only reload used to
    # leave the controllers reading stale values forever.
    _generation: int = 0
    _generation_lock = threading.Lock()

    def __new__(cls, home: Path | None = None) -> "Config":
        if home is not None:
            return super().__new__(cls)
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, home: Path | None = None) -> None:
        bound = Path(home) if home is not None else None
        if bound is None and getattr(self, "_initialized", False):
            return
        self._home = bound
        self._raw: dict[str, Any] | None = None
        self._reload_cache: dict[str, Any] | None = None
        self._reload_cache_at: float = 0.0
        self._seen_generation: int = Config._generation
        # _lock: Config singleton shared across event-loop + worker threads.
        self._lock = threading.Lock()
        self._initialized = True

    def _sync_generation(self) -> None:
        """Drop caches when another holder called ``reload()`` since our last read."""
        current = Config._generation
        if self._seen_generation != current:
            self._raw = None
            self._reload_cache = None
            self._reload_cache_at = 0.0
            self._seen_generation = current

    def reload(self) -> None:
        """Force reload from disk. Called by /aowen config reload.

        Bumps the class-level generation so EVERY instance re-reads — including
        the per-profile instances controllers hold (they never share the
        singleton, see ``__new__``).
        """
        with Config._generation_lock:
            Config._generation += 1
        with self._lock:
            self._sync_generation()
        _logger.info("HLS: config reload triggered — caches cleared")

    @property
    def enabled(self) -> bool:
        """默认 True."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("enabled", True), default=True)

    @property
    def linear(self) -> bool:
        """默认 True."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("linear", True), default=True)

    @property
    def panel_expanded(self) -> bool:
        sec = self._plugin_sec()
        return _to_bool(sec.get("panel_expanded", False))

    @property
    def streaming_panel_expanded(self) -> bool:
        """与 panel_expanded 独立."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("streaming_panel_expanded", False))

    @property
    def max_tool_steps(self) -> int:
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_tool_steps", 20), default=20)
        return max(1, min(100, val))

    @property
    def max_reasoning_rounds(self) -> int:
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_reasoning_rounds", 20), default=20)
        return max(1, min(100, val))

    @property
    def print_strategy(self) -> str:
        """"fast" 或 "delay". 默认 delay."""
        sec = self._plugin_sec()
        strategy = sec.get("print_strategy", "delay")
        return strategy if strategy in ("fast", "delay") else "delay"

    @property
    def print_step(self) -> int:
        """飞书打字机每次渲染字符数. 默认 4, 范围 1~10."""
        sec = self._plugin_sec()
        val = _to_int(sec.get("print_step", 4), default=4)
        return max(1, min(10, val))

    @property
    def flush_interval_ms(self) -> float:
        """流式刷新节流间隔 (ms). 默认 500，即每秒 2 次.

        v1.8: 默认值从 200 上调。200ms 恰好等于飞书 IM 接口单聊/群维度的
        5 QPS 上限（错误码 230020），等于零余量 —— 刷新自己就把配额吃干，
        再叠一次建卡或终态封口就撞限流。2 次/秒留出余量。
        """
        sec = self._plugin_sec()
        ms = _to_float(sec.get("flush_interval_ms", 500), default=500.0)
        return max(70.0, min(2000.0, ms))

    @property
    def flush_interval_sec(self) -> float:
        return self.flush_interval_ms / 1000.0

    @property
    def show_reasoning(self) -> bool:
        """TTL 缓存读取 (/reasoning 命令运行时修改配置)."""
        display = self._reload_cached().get("display")
        if not isinstance(display, dict):
            return False
        platforms = display.get("platforms")
        if isinstance(platforms, dict):
            feishu = platforms.get("feishu")
            if isinstance(feishu, dict) and "show_reasoning" in feishu:
                return _to_bool(feishu["show_reasoning"])
        return _to_bool(display.get("show_reasoning", False))

    @property
    def feishu_app_id(self) -> str:
        return str(self._platform_cfg().get("app_id", ""))

    @property
    def feishu_app_secret(self) -> str:
        return str(self._platform_cfg().get("app_secret", ""))

    @property
    def feishu_base_url(self) -> str:
        return str(self._platform_cfg().get("base_url", "https://open.feishu.cn/open-apis"))

    @property
    def card_duration_sec(self) -> int:
        return _to_int(self._plugin_sec().get("card_ttl_sec", 600), default=600)

    @property
    def footer_fields(self) -> list[list[str]]:
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return self._default_footer_fields()
        fields = footer.get("fields")
        if not fields:
            return self._default_footer_fields()
        if not isinstance(fields, list):
            return self._default_footer_fields()
        if fields and isinstance(fields[0], str):
            return [fields]
        return fields

    @property
    def footer_show_label(self) -> bool:
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        return _to_bool(footer.get("show_label", False))

    @property
    def text_sizes(self) -> dict[str, str | dict[str, str]]:
        """Per-role CardKit sizes, optionally split between PC and mobile."""
        value = self._plugin_sec().get("text_sizes", {})
        return normalize_text_sizes(value)

    @property
    def gateway_cards(self) -> bool:
        """默认 True. TTL 缓存读取."""
        sec = self._reload_cached().get("hermes_lark_streaming")
        if not isinstance(sec, dict):
            return True
        return _to_bool(sec.get("gateway_cards", True), default=True)

    @property
    def busy_supersede_new_card(self) -> bool:
        """agent 忙时收到新消息 → 封口旧卡、后续输出开新卡。默认 True.

        Hermes 的 busy 路径绕过 inbound 入口，插件拿不到新的 message_id；
        关掉的话，打断后的输出会继续写在被打断的那张卡上。
        """
        sec = self._reload_cached().get("hermes_lark_streaming")
        if not isinstance(sec, dict):
            return True
        return _to_bool(sec.get("busy_supersede_new_card", True), default=True)

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "speed", "model", "cost", "compression_exhausted"]]

    @property
    def env_app_id(self) -> str:
        return _get_secret("FEISHU_APP_ID") or _get_secret("LARK_APP_ID")

    @property
    def env_app_secret(self) -> str:
        return _get_secret("FEISHU_APP_SECRET") or _get_secret("LARK_APP_SECRET")

    def _plugin_sec(self) -> dict[str, Any]:
        raw = self._load()
        sec = raw.get("hermes_lark_streaming")
        if isinstance(sec, dict):
            return sec
        return {}

    def _platform_cfg(self) -> dict[str, Any]:
        """从环境变量 / secret scope 或平台配置找飞书凭据."""
        env_app_id = self.env_app_id
        env_app_secret = self.env_app_secret
        if env_app_id and env_app_secret:
            base_url = _get_secret("FEISHU_BASE_URL") or _get_secret("LARK_BASE_URL") or ""
            if not base_url:
                domain = _get_secret("FEISHU_DOMAIN").lower()
                base_url = (
                    LARK_OPEN_API_BASE if domain == "lark" else FEISHU_OPEN_API_BASE
                )
            return {
                "app_id": env_app_id,
                "app_secret": env_app_secret,
                "base_url": base_url,
            }
        raw = self._load()
        for key in ("feishu", "lark"):
            pf = raw.get(key)
            if isinstance(pf, dict) and pf.get("app_id"):
                return pf
        # Hermes gateway 平台形状: gateway.platforms.<feishu|lark>.extra 或
        # platforms.<feishu|lark>.extra (凭据在 extra 里, domain 决定 lark 域名).
        candidate_parents: list[dict[str, Any]] = []
        gateway = raw.get("gateway")
        if isinstance(gateway, dict) and isinstance(gateway.get("platforms"), dict):
            candidate_parents.append(gateway["platforms"])
        platforms = raw.get("platforms")
        if isinstance(platforms, dict):
            candidate_parents.append(platforms)
        for parent in candidate_parents:
            for key in ("feishu", "lark"):
                platform = parent.get(key)
                if not isinstance(platform, dict):
                    continue
                extra = platform.get("extra")
                if not isinstance(extra, dict) or not extra.get("app_id"):
                    continue
                result = dict(extra)
                if "base_url" not in result and platform.get("base_url"):
                    result["base_url"] = platform["base_url"]
                if "base_url" not in result:
                    domain = str(extra.get("domain", platform.get("domain", ""))).lower()
                    if domain == "lark":
                        result["base_url"] = LARK_OPEN_API_BASE
                return result
        return {}

    def _load(self) -> dict[str, Any]:
        with self._lock:
            self._sync_generation()
            if self._raw is not None:
                return self._raw
            config_path = _get_hermes_config_path(self._home)
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._raw = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s, using empty config", config_path)
                    self._raw = {}
                except (OSError, UnicodeDecodeError):
                    _logger.warning("HLS: config file read error in %s, using empty config", config_path)
                    self._raw = {}
            else:
                self._raw = {}
            return self._raw

    def _reload_cached(self) -> dict[str, Any]:
        """带 TTL 缓存的磁盘重读 (运行时可变配置项). 避免高频属性访问读磁盘."""
        now = time.monotonic()
        with self._lock:
            self._sync_generation()
            if self._reload_cache is not None and (now - self._reload_cache_at) < _RELOAD_CACHE_TTL:
                return self._reload_cache
            config_path = _get_hermes_config_path(self._home)
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._reload_cache = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
                except (OSError, UnicodeDecodeError):
                    _logger.warning("HLS: config file read error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
            else:
                self._reload_cache = {}
            self._reload_cache_at = now
            return self._reload_cache
