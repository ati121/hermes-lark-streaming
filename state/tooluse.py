"""工具调用追踪与可视化 — 与 openclaw-lark 工具展示对齐."""

from __future__ import annotations

__all__ = [
    "ToolStep",
    "ToolSession",
    "ToolUseTracker",
    "redact_inline_secrets",
    "_basename_only",
    "_build_display_block",
    "_fenced_block",
    "_format_duration_label",
    "_humanize_tool_name",
    "_redact_paths",
    "_resolve_tool_descriptor",
    "_sanitize_detail",
    "_tool_emoji",
    "_TOOL_DESCRIPTORS",
    "_SENSITIVE_NAME_RE",
    "_INLINE_ASSIGNMENT_RE",
    "_AUTH_HEADER_RE",
    "_SECRET_FLAG_RE",
]

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolStep:
    name: str
    status: str  # running | success | error
    detail: str = ""
    output: str = ""
    error: str = ""
    result_block: dict[str, Any] | None = None  # {"language": "json"|"text", "content": str}
    error_block: dict[str, Any] | None = None
    started_at: float | None = None
    elapsed_ms: float = 0.0

@dataclass
class ToolSession:
    steps: list[ToolStep] = field(default_factory=list)
    started_at: float | None = None

_SENSITIVE_NAME_RE = re.compile(
    r"token|secret|password|api[_-]?key|authorization|cookie|credential"
    r"|bearer|session[_-]?id|client[_-]?secret|access[_-]?key",
    re.IGNORECASE,
)

_INLINE_ASSIGNMENT_RE = re.compile(r'(^|[\s"\'`])([A-Za-z_][A-Za-z0-9_]*)(=(?:"[^"]*"|\'[^\']*\'|[^\s"\'`]+))')
_AUTH_HEADER_RE = re.compile(
    r"(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)([^\'\"\s]+)",
    re.IGNORECASE,
)
_SECRET_FLAG_RE = re.compile(
    r'((?:^|[\s"\'`])(--?[A-Za-z0-9][A-Za-z0-9-]*)(=|\s+)("(?:[^"]*)"|\'(?:[^\']*)\'|[^\s"\'`]+))'
)

def redact_inline_secrets(value: str) -> str:
    """脱敏 key=secret、Authorization header、--flag secret 模式."""

    def _redact_assign(m: re.Match) -> str:
        key = str(m.group(2))
        if _SENSITIVE_NAME_RE.search(key):
            return f"{m.group(1)}{key}=[redacted]"
        return str(m.group(0))

    def _redact_flag(m: re.Match) -> str:
        flag = re.sub(r"^-+", "", str(m.group(2)))
        if _SENSITIVE_NAME_RE.search(flag):
            return f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]"
        return str(m.group(0))

    return _SECRET_FLAG_RE.sub(
        _redact_flag,
        _AUTH_HEADER_RE.sub(r"\1[redacted]", _INLINE_ASSIGNMENT_RE.sub(_redact_assign, value)),
    )

def _sanitize_detail(text: str, sanitizer: str | None) -> str:
    """根据 sanitizer 类型清洗 detail 文本."""
    if not text or not sanitizer:
        return text
    cleaned = re.sub(r"<[^>]+>", "", text).strip()
    if not cleaned:
        return text
    if sanitizer == "command":
        cleaned = redact_inline_secrets(cleaned)
        return _redact_paths(cleaned)
    if sanitizer == "path":
        return _basename_only(re.sub(r"^(?:from|file|path)\s+", "", cleaned, flags=re.IGNORECASE).strip())
    if sanitizer == "search":
        return cleaned.strip("'\"")
    if sanitizer == "url":
        if cleaned.lower().startswith("from "):
            return cleaned.strip("'\"").replace("from ", "", 1)
        return cleaned.strip("'\"")
    return cleaned

def _redact_paths(text: str) -> str:
    """命令中路径只保留 basename."""
    return re.sub(
        r'(^|[\s=\'"()])([~./][^\s\'"()]+)',
        lambda m: f"{m.group(1)}{os.path.basename(m.group(2))}",
        text,
    )

def _basename_only(text: str) -> str:
    if not text:
        return text
    return os.path.basename(text.replace("\\", "/").rstrip("/"))

# ── Exact-match specs for real Hermes tool names ───────────────────────────
# Hermes hands the model's raw ``function_name`` to the progress callback
# (agent/tool_executor.py), so these keys are the literal strings we receive.
# Format: name → (zh title, en title, icon, sanitizer, no_result)
_TOOL_SPECS: dict[str, tuple[str, str, str, str | None, bool]] = {
    # Shell / files
    "terminal": ("终端命令", "Terminal", "setting_outlined", "command", False),
    "close_terminal": ("关闭终端", "Close terminal", "setting_outlined", None, True),
    "read_terminal": ("读取终端", "Read terminal", "setting_outlined", None, False),
    "read_file": ("读取文件", "Read", "file-link-text_outlined", "path", True),
    "write_file": ("写入文件", "Edit", "edit_outlined", "path", True),
    "patch": ("修改文件", "Patch", "edit_outlined", "path", True),
    "search_files": ("搜索文件", "Search files", "doc-search_outlined", "search", False),
    "execute_code": ("执行代码", "Execute code", "setting_outlined", None, False),
    # Web
    "web_search": ("联网搜索", "Search", "search_outlined", "search", False),
    "web_extract": ("抓取网页", "Fetch web page", "language_outlined", "url", True),
    "x_search": ("X 搜索", "X search", "search_outlined", "search", False),
    # Skills
    "skill_view": ("查看技能", "View skill", "app-default_outlined", None, False),
    "skill_manage": ("管理技能", "Manage skill", "app-default_outlined", None, False),
    "skills_list": ("技能列表", "List skills", "app-default_outlined", None, False),
    "setup_mcp": ("配置 MCP", "Set up MCP", "setting-inter_outlined", None, False),
    # Memory — keep the provider visible in both locales.
    "memory": ("Hermes · 内置记忆", "Hermes · Built-in memory", "time_outlined", None, False),
    "session_search": ("Hermes · 会话检索", "Hermes · Search sessions", "doc-search_outlined", "search", False),
    "hindsight_retain": ("Hindsight · 记忆写入", "Hindsight · Retain memory", "time_outlined", None, True),
    "hindsight_recall": ("Hindsight · 记忆回溯", "Hindsight · Recall memory", "time_outlined", "search", False),
    "hindsight_reflect": ("Hindsight · 记忆推演", "Hindsight · Reflect on memory", "time_outlined", "search", False),
    "hindsight_operation": ("Hindsight · 记忆操作", "Hindsight · Memory operation", "time_outlined", None, False),
    # OpenViking memory provider — Hermes exposes six literal viking_* names.
    # Search/read/browse also cover knowledge resources in the memory store.
    "viking_search": ("OpenViking · 检索记忆", "OpenViking · Search memory", "doc-search_outlined", None, False),
    "viking_read": ("OpenViking · 读取记忆", "OpenViking · Read memory", "file-link-text_outlined", None, False),
    "viking_browse": ("OpenViking · 浏览记忆库", "OpenViking · Browse memory store", "folder_outlined", None, False),
    "viking_remember": ("OpenViking · 记住信息", "OpenViking · Remember information", "time_outlined", None, False),
    "viking_forget": ("OpenViking · 删除记忆", "OpenViking · Delete memory", "time_outlined", None, False),
    "viking_add_resource": ("OpenViking · 导入资料", "OpenViking · Import resources", "file-link-text_outlined", None, False),
    # Delegation / planning
    "delegate_task": ("派发子任务", "Delegate task", "robot_outlined", None, False),
    "todo": ("待办清单", "Todo", "list-check_outlined", None, True),
    "clarify": ("追问确认", "Clarify", "list-check_outlined", None, True),
    "cronjob": ("定时任务", "Cron job", "time_outlined", None, False),
    # Tool discovery
    "tool_search": ("工具检索", "Search tools", "doc-search_outlined", "search", False),
    "tool_describe": ("工具说明", "Describe tool", "setting-inter_outlined", None, False),
    "tool_call": ("调用工具", "Call tool", "setting-inter_outlined", None, False),
    # Media
    "vision_analyze": ("图像分析", "Analyze image", "report_outlined", None, False),
    "video_analyze": ("视频分析", "Analyze video", "report_outlined", None, False),
    "image_generate": ("生成图片", "Generate image", "report_outlined", None, True),
    "video_generate": ("生成视频", "Generate video", "report_outlined", None, True),
    "text_to_speech": ("语音合成", "Text to speech", "report_outlined", None, True),
    # Browser
    "browser_navigate": ("浏览器跳转", "Browser navigate", "browser-mac_outlined", "url", True),
    "browser_click": ("浏览器点击", "Browser click", "browser-mac_outlined", None, True),
    "browser_type": ("浏览器输入", "Browser type", "browser-mac_outlined", None, True),
    "browser_snapshot": ("浏览器快照", "Browser snapshot", "browser-mac_outlined", None, True),
    "browser_exec": ("浏览器执行", "Browser exec", "browser-mac_outlined", None, True),
    "browser_vision": ("浏览器视觉", "Browser vision", "browser-mac_outlined", None, True),
    "browser_scroll": ("浏览器滚动", "Browser scroll", "browser-mac_outlined", None, True),
    "browser_back": ("浏览器返回", "Browser back", "browser-mac_outlined", None, True),
    "browser_press": ("浏览器按键", "Browser press", "browser-mac_outlined", None, True),
    "browser_console": ("浏览器控制台", "Browser console", "browser-mac_outlined", None, False),
    "browser_dialog": ("浏览器弹窗", "Browser dialog", "browser-mac_outlined", None, True),
    "browser_get_images": ("浏览器取图", "Browser images", "browser-mac_outlined", None, True),
    "browser_cdp": ("浏览器 CDP", "Browser CDP", "browser-mac_outlined", None, False),
    # Projects / panes
    "project_create": ("创建项目", "Create project", "folder_outlined", None, True),
    "project_list": ("项目列表", "List projects", "folder_outlined", None, False),
    "project_switch": ("切换项目", "Switch project", "folder_outlined", None, True),
    "focus_pane": ("聚焦窗格", "Focus pane", "folder_outlined", None, True),
    "open_preview": ("打开预览", "Open preview", "folder_outlined", None, True),
    "read_preview": ("读取预览", "Read preview", "folder_outlined", None, False),
    "read_window_below": ("读取下方窗口", "Read window below", "folder_outlined", None, False),
    # Messaging
    "send_message": ("发送消息", "Send message", "app-default_outlined", None, True),
    "react_to_message": ("消息回应", "React to message", "app-default_outlined", None, True),
    # Feishu docs / drive
    "feishu_doc_read": ("读取飞书文档", "Read Feishu doc", "file-link-text_outlined", None, False),
    "feishu_drive_list_comments": ("飞书评论列表", "List Feishu comments", "app-default_outlined", None, False),
    "feishu_drive_add_comment": ("飞书添加评论", "Add Feishu comment", "app-default_outlined", None, True),
    "feishu_drive_reply_comment": ("飞书回复评论", "Reply Feishu comment", "app-default_outlined", None, True),
    "feishu_drive_list_comment_replies": ("飞书评论回复", "List comment replies", "app-default_outlined", None, False),
    # Home Assistant
    "ha_call_service": ("智能家居调用", "Call HA service", "setting-inter_outlined", None, True),
    "ha_get_state": ("智能家居状态", "Get HA state", "setting-inter_outlined", None, False),
    "ha_list_entities": ("智能家居设备", "List HA entities", "setting-inter_outlined", None, False),
    "ha_list_services": ("智能家居服务", "List HA services", "setting-inter_outlined", None, False),
    # ── Hermes tool names added after the original table (v1.6.29) ────────
    # The table above was written against older Hermes builds; the live
    # names are todo_list / cronjob_manage / process_manage. Anything not
    # listed here falls through to _humanize_tool_name and renders English
    # in a zh_cn card. Exact specs also win over the prefix aliases in
    # _TOOL_DESCRIPTORS, which is what stops browser_vault_* from collapsing
    # into the bare "Browser" row.
    #
    # Background-process manager for terminal(background=true)
    # (poll/wait/kill/log/write/submit/close/handoff) — processes, not flows.
    "process_manage": ("进程管理", "Manage processes", "setting-inter_outlined", None, False),
    "todo_list": ("待办清单", "Todo list", "list-check_outlined", None, True),
    "cronjob_manage": ("定时任务", "Cron job", "time_outlined", None, False),
    "manage_connections": ("管理连接账户", "Manage connections", "app-default_outlined", None, False),
    "computer_use": ("电脑操作", "Computer use", "setting_outlined", None, False),
    # Browser credential vault — five distinct labels so the card says which
    # half of the flow ran (list / unlock / fill / save / one-time code).
    "browser_vault_list": ("浏览器 · 凭据列表", "Browser vault list", "browser-mac_outlined", None, False),
    "browser_vault_unlock": ("浏览器 · 解锁凭据", "Unlock browser vault", "browser-mac_outlined", None, True),
    "browser_vault_fill": ("浏览器 · 填充凭据", "Fill browser credential", "browser-mac_outlined", None, True),
    "browser_vault_save_login": ("浏览器 · 保存登录", "Save browser login", "browser-mac_outlined", None, True),
    "browser_vault_enter_code": ("浏览器 · 输入验证码", "Enter browser code", "browser-mac_outlined", None, True),
    # Kanban multi-agent coordination (kanban_* in toolsets.py).
    "kanban_show": ("看板 · 查看任务", "Show task", "list-check_outlined", None, False),
    "kanban_list": ("看板 · 任务列表", "List tasks", "list-check_outlined", None, False),
    "kanban_complete": ("看板 · 完成任务", "Complete task", "list-check_outlined", None, True),
    "kanban_block": ("看板 · 阻塞任务", "Block task", "list-check_outlined", None, True),
    "kanban_request_review": ("看板 · 请求审查", "Request review", "report_outlined", None, True),
    "kanban_request_changes": ("看板 · 驳回修改", "Request changes", "report_outlined", None, True),
    "kanban_heartbeat": ("看板 · 心跳", "Heartbeat", "time_outlined", None, True),
    "kanban_comment": ("看板 · 评论", "Comment", "app-default_outlined", None, True),
    "kanban_create": ("看板 · 新建任务", "Create task", "list-check_outlined", None, True),
    "kanban_link": ("看板 · 关联任务", "Link tasks", "app-default_outlined", None, True),
    "kanban_unblock": ("看板 · 解除阻塞", "Unblock task", "list-check_outlined", None, True),
    "kanban_attach": ("看板 · 添加附件", "Attach file", "file-link-text_outlined", None, True),
    "kanban_attach_url": ("看板 · 添加链接", "Attach URL", "file-link-text_outlined", None, True),
    "kanban_attachments": ("看板 · 附件列表", "List attachments", "folder_outlined", None, False),
    # Spotify playback (spotify_* in toolsets.py).
    "spotify_playback": ("音乐 · 播放控制", "Playback control", "app-default_outlined", None, False),
    "spotify_devices": ("音乐 · 播放设备", "List devices", "app-default_outlined", None, False),
    "spotify_queue": ("音乐 · 播放队列", "Queue", "app-default_outlined", None, False),
    "spotify_search": ("音乐 · 搜索", "Search music", "search_outlined", "search", False),
    "spotify_playlists": ("音乐 · 播放列表", "Playlists", "app-default_outlined", None, False),
    "spotify_albums": ("音乐 · 专辑", "Albums", "app-default_outlined", None, False),
    "spotify_library": ("音乐 · 音乐库", "Library", "folder_outlined", None, False),
    # Discord read/admin.
    "discord": ("Discord · 消息", "Discord messages", "app-default_outlined", None, False),
    "discord_admin": ("Discord · 服务器管理", "Discord admin", "setting-inter_outlined", None, False),
    # Yuanbao (yb_*) platform tools.
    "yb_query_group_info": ("元宝 · 群信息", "Group info", "app-default_outlined", None, False),
    "yb_query_group_members": ("元宝 · 群成员", "Group members", "app-default_outlined", None, False),
    "yb_send_dm": ("元宝 · 发送私聊", "Send DM", "app-default_outlined", None, True),
    "yb_search_sticker": ("元宝 · 搜索表情", "Search sticker", "doc-search_outlined", "search", False),
    "yb_send_sticker": ("元宝 · 发送表情", "Send sticker", "app-default_outlined", None, True),
    # Desktop GUI affordances (desktop_ui toolset — GUI sessions only).
    "annotate_preview": ("标注预览", "Annotate preview", "edit_outlined", None, True),
    "apply_layout": ("应用布局", "Apply layout", "report_outlined", None, True),
    "close_preview": ("关闭预览", "Close preview", "folder_outlined", None, True),
    "desktop_preview": ("桌面预览", "Desktop preview", "folder_outlined", None, True),
    "desktop_project": ("桌面项目", "Desktop project", "folder_outlined", None, False),
    "drive_preview": ("云盘预览", "Drive preview", "folder_outlined", None, True),
    "gui_tour": ("界面导览", "GUI tour", "robot_outlined", None, False),
    "show_tip": ("显示提示", "Show tip", "info_outlined", None, False),
    # xAI video generation variants (video_gen toolset).
    "xai_video_edit": ("视频编辑", "Edit video", "report_outlined", None, True),
    "xai_video_extend": ("视频延长", "Extend video", "report_outlined", None, True),
    # Hermes-internal schemas. They rarely reach a card, but an English row is
    # worse than a translated one when they do.
    "allowlist": ("域名白名单", "Domain allowlist", "setting-inter_outlined", None, False),
    "secrets": ("密钥规则", "Secret rules", "setting-inter_outlined", None, False),
    "session_title": ("会话标题", "Session title", "app-default_outlined", None, False),
    "example_tool": ("示例工具", "Example tool", "app-default_outlined", None, False),
    "plugin_structured_output": ("插件结构化输出", "Plugin structured output", "app-default_outlined", None, False),
}

_TOOL_DESCRIPTORS: list[dict[str, Any]] = [
    {"aliases": ["skill"], "icon": "app-default_outlined", "title": "Load skill", "sanitizer": None},
    {
        "aliases": ["read", "open"],
        "icon": "file-link-text_outlined",
        "title": "Read",
        "sanitizer": "path",
        "no_result": True,
    },
    {
        "aliases": ["write", "edit"],
        "icon": "edit_outlined",
        "title": "Edit",
        "sanitizer": "path",
        "no_result": True,
    },
    {
        "aliases": ["web_search", "web-search", "search"],
        "icon": "search_outlined",
        "title": "Search",
        "sanitizer": "search",
    },
    {
        "aliases": ["web_fetch", "web-fetch", "fetch"],
        "icon": "language_outlined",
        "title": "Fetch web page",
        "sanitizer": "url",
        "no_result": True,
    },
    {"aliases": ["grep"], "icon": "doc-search_outlined", "title": "Search text", "sanitizer": "search"},
    {"aliases": ["glob"], "icon": "folder_outlined", "title": "Search files", "sanitizer": "path"},
    {
        "aliases": ["exec", "bash", "command", "run"],
        "icon": "setting_outlined",
        "title": "Run command",
        "sanitizer": "command",
    },
    {
        "aliases": ["browser", "playwright", "navigate"],
        "icon": "browser-mac_outlined",
        "title": "Browser",
        "no_result": True,
    },
    {"aliases": ["agent", "task", "spawn"], "icon": "robot_outlined", "title": "Run sub-agent"},
    {"aliases": ["check", "determine", "verify"], "icon": "list-check_outlined", "title": "Check"},
    {"aliases": ["summarize", "analyze", "prepare"], "icon": "report_outlined", "title": "Analyze"},
]

def _normalize_tool_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")

# ── Terminal programs that deserve a row of their own ─────────────────────
# Hermes reports a CLI the owner installed as a tool through ``terminal``,
# with the command line as the preview. Listed programs render as themselves
# (own title and emoji) instead of a generic 🖥️ 终端命令 row, and the
# program name is dropped from the detail line so it is not printed twice.
# program → (zh title, en title, emoji)
_TERMINAL_PROGRAM_SPECS: dict[str, tuple[str, str, str]] = {
    "smart-search": ("smart-search", "smart-search", "🔍"),
    # Unicode has no GitHub glyph; the octopus is the usual Octocat stand-in.
    "gh": ("GitHub", "GitHub", "🐙"),
}

# Words that can precede the real program on a command line.
_COMMAND_WRAPPERS = frozenset({"sudo", "env", "nohup", "time", "exec", "command", "nice"})
_COMMAND_SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_programs(command: str) -> list[str]:
    """Program names (basenames) at the head of each segment of a shell command."""
    programs: list[str] = []
    for segment in _COMMAND_SEGMENT_RE.split(command or ""):
        for token in segment.split():
            token = token.strip("'\"")
            if not token or _ENV_ASSIGN_RE.match(token):
                continue
            base = os.path.basename(token.replace("\\", "/"))
            if base.lower() in _COMMAND_WRAPPERS or base.startswith("-"):
                continue
            programs.append(base)
            break
    return programs


def _terminal_program_spec(name: str | None, detail: str | None) -> tuple[str, tuple[str, str, str]] | None:
    """``(program, spec)`` when a ``terminal`` command runs a listed program."""
    if not name or not detail or _normalize_tool_name(name) != "terminal":
        return None
    for program in _command_programs(detail):
        spec = _TERMINAL_PROGRAM_SPECS.get(program.lower())
        if spec is not None:
            return program, spec
    return None


def _strip_leading_program(detail: str, program: str) -> str:
    """Drop ``program`` from the front of a (sanitised) command line."""
    stripped = (detail or "").lstrip()
    if stripped.lower().startswith(program.lower()):
        rest = stripped[len(program):]
        if not rest or rest[0].isspace():
            return rest.strip()
    return detail

# ── Emoji shown beside the spinner while a tool runs ──────────────────────
# Resolved in two layers. The icon token already encodes the design's own
# grouping, so keying off it covers both _TOOL_SPECS and the legacy
# _TOOL_DESCRIPTORS without a per-tool entry; the by-name table above it only
# holds the cases where one token spans unrelated tools (memory and cron both
# sit on time_outlined, every media tool on report_outlined).
_TOOL_EMOJI_BY_NAME: dict[str, str] = {
    "memory": "🧠",
    "hindsight_retain": "👁️",
    "hindsight_recall": "👁️",
    "hindsight_reflect": "👁️",
    "hindsight_operation": "👁️",
    "viking_search": "📖",
    "viking_read": "📖",
    "viking_browse": "📖",
    "viking_remember": "📖",
    "viking_forget": "📖",
    "viking_add_resource": "📖",
    "cronjob": "⏰",
    "clarify": "❓",
    "todo": "📋",
    "execute_code": "⚡",
    "web_extract": "🌐",
    "vision_analyze": "👁️",
    "video_analyze": "🎬",
    "image_generate": "🎨",
    "video_generate": "🎬",
    "text_to_speech": "🔊",
    "send_message": "💬",
    "react_to_message": "💬",
    # ── v1.6.29: one colourful emoji per tool ─────────────────────────────
    # Feather/Lark standard_icon tokens are monochrome line art, so a row
    # built from them is grey no matter which token is picked. Emoji carry
    # their own colour, and the spinner row has always used them — these
    # entries extend that to every tool so the panel steps match.
    # Grouped by family; within a family no emoji repeats, so two rows are
    # never ambiguous. Values here override _TOOL_EMOJI_BY_ICON.
    # ── Feishu gateway ──
    "process_manage": "⚙️",
    "todo_list": "📋",
    "cronjob_manage": "⏰",
    "manage_connections": "🔌",
    "computer_use": "🖱️",
    # ── Browser credential vault ──
    "browser_vault_list": "🗂️",
    "browser_vault_unlock": "🔓",
    "browser_vault_fill": "⌨️",
    "browser_vault_save_login": "💾",
    "browser_vault_enter_code": "🔢",
    # ── Kanban ──
    "kanban_show": "👀",
    "kanban_list": "🗒️",
    "kanban_complete": "✅",
    "kanban_block": "⛔",
    "kanban_request_review": "📤",
    "kanban_request_changes": "↩️",
    "kanban_heartbeat": "💓",
    "kanban_comment": "🗨️",
    "kanban_create": "➕",
    "kanban_link": "🔗",
    "kanban_unblock": "🔄",
    "kanban_attach": "📎",
    "kanban_attach_url": "🌐",
    "kanban_attachments": "📂",
    # ── Spotify ──
    "spotify_playback": "▶️",
    "spotify_devices": "🔊",
    "spotify_queue": "📜",
    "spotify_search": "🔎",
    "spotify_playlists": "🎵",
    "spotify_albums": "💿",
    "spotify_library": "📚",
    # ── Discord / Yuanbao ──
    "discord": "💬",
    "discord_admin": "🛡️",
    "yb_query_group_info": "👥",
    "yb_query_group_members": "👤",
    "yb_send_dm": "✉️",
    "yb_search_sticker": "😀",
    "yb_send_sticker": "🎴",
    # ── Desktop GUI ──
    "annotate_preview": "📝",
    "apply_layout": "🖼️",
    "close_preview": "✖️",
    "desktop_preview": "🪟",
    "desktop_project": "📁",
    "drive_preview": "☁️",
    "gui_tour": "🧭",
    "show_tip": "💡",
    # ── xAI video ──
    "xai_video_edit": "✂️",
    "xai_video_extend": "➡️",
    # ── Hermes internals ──
    "allowlist": "🚧",
    "secrets": "🔑",
    "session_title": "🏷️",
    "example_tool": "🧪",
    "plugin_structured_output": "🧬",
    # ── Home Assistant / tool discovery ──
    # These sit on setting-inter_outlined, whose 🧩-tier fallback (🔧) is also
    # _DEFAULT_TOOL_EMOJI — without an override they read as "unknown tool".
    "ha_list_entities": "🏠",
    "ha_get_state": "📟",
    "ha_list_services": "🛎️",
    "ha_call_service": "🎛️",
    "setup_mcp": "🛠️",
    "tool_describe": "📖",
    "tool_call": "🎯",
}
_TOOL_EMOJI_BY_ICON: dict[str, str] = {
    "setting_outlined": "🖥️",
    "file-link-text_outlined": "📄",
    "edit_outlined": "✏️",
    "doc-search_outlined": "🔎",
    "search_outlined": "🔍",
    "language_outlined": "🌐",
    "app-default_outlined": "🧩",
    "setting-inter_outlined": "🔧",
    "time_outlined": "⏰",
    "robot_outlined": "🤖",
    "list-check_outlined": "📋",
    "report_outlined": "📊",
    "browser-mac_outlined": "🌐",
    "folder_outlined": "📁",
    "info_outlined": "💡",
}

_DEFAULT_TOOL_EMOJI = "🔧"

def _tool_emoji(name: str | None, detail: str | None = None) -> str:
    """Emoji for a raw tool name — never empty, so the row can't jump around.

    ``detail`` is the tool's preview; for ``terminal`` it is the command line,
    which can promote a listed program to its own emoji.
    """
    if not name:
        return _DEFAULT_TOOL_EMOJI
    aliased = _terminal_program_spec(name, detail)
    if aliased is not None:
        return aliased[1][2]
    direct = _TOOL_EMOJI_BY_NAME.get(_normalize_tool_name(name))
    if direct:
        return direct
    desc = _resolve_tool_descriptor(name)
    if desc is not None:
        return _TOOL_EMOJI_BY_ICON.get(str(desc.get("icon") or ""), _DEFAULT_TOOL_EMOJI)
    return _DEFAULT_TOOL_EMOJI

def _spec_to_descriptor(name: str, spec: tuple[str, str, str, str | None, bool]) -> dict[str, Any]:
    zh, en, icon, sanitizer, no_result = spec
    return {
        "aliases": [name],
        "icon": icon,
        "title": en,
        "title_zh": zh,
        "sanitizer": sanitizer,
        "no_result": no_result,
    }

def _resolve_tool_descriptor(name: str | None) -> dict[str, Any] | None:
    """Exact spec first, then the legacy alias-prefix table as a fallback."""
    if not name:
        return None
    normalized = _normalize_tool_name(name)
    spec = _TOOL_SPECS.get(normalized)
    if spec is not None:
        return _spec_to_descriptor(normalized, spec)
    for desc in _TOOL_DESCRIPTORS:
        for alias in desc["aliases"]:
            if normalized == alias or normalized.startswith(f"{alias}_"):
                return desc
    return None

def _humanize_tool_name(name: str) -> str:
    # MCP tools arrive as mcp__<server>__<tool>; the bare underscore split
    # would render "Mcp  server  tool" with doubled spaces.
    if name.startswith("mcp__"):
        parts = [p for p in name[len("mcp__"):].split("__") if p]
        if parts:
            return " · ".join(p.replace("_", " ").strip() for p in parts)
    cleaned = name.replace("-", " ").replace("_", " ").strip()
    if not cleaned:
        return "Tool"
    return cleaned[0].upper() + cleaned[1:]

def _tool_display_names(name: str | None, detail: str | None = None) -> tuple[str, str]:
    """Return ``(en, zh)`` display names for a raw tool name.

    ``detail`` is the tool's preview; for ``terminal`` it is the command line,
    which can promote a listed program to its own title.
    """
    if not name:
        return "Tool", "工具"
    aliased = _terminal_program_spec(name, detail)
    if aliased is not None:
        _zh, _en, _emoji = aliased[1]
        return _en, _zh
    desc = _resolve_tool_descriptor(name)
    if desc is not None:
        en = desc["title"]
        return en, desc.get("title_zh") or en
    humanized = _humanize_tool_name(name)
    return humanized, humanized

def _format_duration_label(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{(ms / 1000):.1f} s"

def _build_display_block(
    value: Any,
    fallback_lang: str = "json",
    *,
    sanitizer: str | None = None,
) -> dict[str, Any] | None:
    """构建结果/错误的显示块 — 返回 {language, content, fenced} 含 markdown 代码围栏."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.replace("\r\n", "\n").strip()
        if not normalized:
            return None
        if sanitizer == "command":
            normalized = redact_inline_secrets(normalized)
        if normalized.startswith("{") or normalized.startswith("["):
            try:
                parsed = json.loads(normalized)
                pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
                return _fenced_block("json", pretty)
            except json.JSONDecodeError:
                pass
        return _fenced_block("text" if fallback_lang == "json" else fallback_lang, normalized)
    if isinstance(value, (dict, list)):
        try:
            return _fenced_block("json", json.dumps(value, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            pass
    normalized = str(value).strip()
    return _fenced_block("text", normalized) if normalized else None

def _fenced_block(language: str, content: str) -> dict[str, Any]:
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", content)), default=0) + 1)
    return {"language": language, "content": content, "fenced": f"{fence}{language}\n{content}\n{fence}"}

class ToolUseTracker:
    """按 session 隔离，每个会话独立生命周期."""

    def __init__(self, max_steps: int = 128) -> None:
        self._session: ToolSession | None = None
        self._max_steps = max_steps

    @property
    def elapsed_ms(self) -> float:
        if self._session is None or self._session.started_at is None:
            return 0.0
        return (time.time() - self._session.started_at) * 1000

    @property
    def last_tool_names(self) -> tuple[str, str] | None:
        """``(en, zh)`` display names of the most recent tool, running or not.

        Deliberately sticky: the spinner keeps showing this tool until the next
        one takes over, instead of blanking the moment a fast tool returns.
        """
        if self._session is None or not self._session.steps:
            return None
        last = self._session.steps[-1]
        return _tool_display_names(last.name, last.detail)

    @property
    def last_tool_emoji(self) -> str | None:
        """Emoji for whichever tool ``last_tool_names`` is reporting."""
        if self._session is None or not self._session.steps:
            return None
        last = self._session.steps[-1]
        return _tool_emoji(last.name, last.detail)

    def record_start(self, name: str, detail: str = "") -> None:
        if self._session is None:
            self._session = ToolSession(started_at=time.time())
        if len(self._session.steps) >= self._max_steps:
            return
        self._session.steps.append(
            ToolStep(
                name=name,
                status="running",
                detail=detail,
                started_at=time.time(),
            )
        )

    def record_end(self, name: str, *, error: str = "", output: str = "") -> None:
        """通过名字匹配最近的一个 running 步骤来结束."""
        if self._session is None:
            return
        desc = _resolve_tool_descriptor(name)
        sanitizer = desc.get("sanitizer") if desc else None
        for step in reversed(self._session.steps):
            if step.name == name and step.status == "running":
                step.status = "error" if error else "success"
                step.error = error
                step.output = output
                step.elapsed_ms = (time.time() - step.started_at) * 1000 if step.started_at is not None else 0.0
                if error:
                    step.error_block = _build_display_block(error, "text", sanitizer=sanitizer)
                elif output:
                    step.result_block = _build_display_block(output, "json", sanitizer=sanitizer)
                return
        self._session.steps.append(
            ToolStep(
                name=name,
                status="error" if error else "success",
                detail=error or output,
                output=output,
                error=error,
                started_at=time.time(),
                error_block=_build_display_block(error, "text", sanitizer=sanitizer) if error else None,
                result_block=_build_display_block(output, "json", sanitizer=sanitizer) if output else None,
            )
        )

    def build_display_steps(self) -> list[dict[str, Any]]:
        """构建用于卡片渲染的步骤列表 — 与 openclaw 结构对齐."""
        if self._session is None:
            return []
        steps = []
        for s in self._session.steps:
            desc = _resolve_tool_descriptor(s.name)
            base_title, base_title_zh = _tool_display_names(s.name, s.detail)
            if s.elapsed_ms > 0:
                suffix = f" ({_format_duration_label(s.elapsed_ms)})"
                base_title += suffix
                base_title_zh += suffix
            sanitizer = desc.get("sanitizer") if desc else None
            detail = _sanitize_detail(s.detail, sanitizer)
            aliased = _terminal_program_spec(s.name, s.detail)
            if aliased is not None:
                detail = _strip_leading_program(detail, aliased[0])
            steps.append(
                {
                    "name": s.name,
                    "title": base_title,
                    "title_zh": base_title_zh,
                    "status": s.status,
                    "detail": detail,
                    "output": s.output,
                    "error": s.error,
                    # Kept as the emoji-grouping key (_TOOL_EMOJI_BY_ICON);
                    # the card renders ``emoji``, not this token.
                    "icon": desc["icon"] if desc else "setting-inter_outlined",
                    "emoji": _tool_emoji(s.name, s.detail),
                    "elapsed_ms": s.elapsed_ms,
                    "result_block": None if (desc and desc.get("no_result")) else s.result_block,
                    "error_block": s.error_block,
                }
            )
        return steps
