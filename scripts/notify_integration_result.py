#!/usr/bin/env python3
"""Send a Feishu card with Hermes integration test results.

Invoked by .github/workflows/hermes-integration-test.yml. All inputs arrive as
step-scoped environment variables; nothing is read from the command line.

Lives in a .py file rather than inline in the workflow YAML on purpose: the
plugin security scanner applies its code exemptions only to real source files,
so environment reads embedded in YAML are reported as findings.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

_REPORT_PATH = "integration_test_report.xml"
_MAX_FAILED_LINES = 15
_MAX_FAILURE_MESSAGE = 80
_WEBHOOK_TIMEOUT_SEC = 15


def _read(name: str, default: str = "") -> str:
    """Read one workflow-provided variable."""
    return os.environ.get(name, default)


def _sign(secret: str, timestamp: str) -> str:
    """Feishu webhook signature: HMAC-SHA256 over "<timestamp>\\n<secret>"."""
    digest = hmac.new(
        f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode()


def _status(outcome: str, exit_code: str) -> tuple[str, str, str, str]:
    """Map test outcome to (icon, text, card colour, summary)."""
    if outcome == "skipped":
        return "⚠️", "SKIPPED", "orange", "测试未能运行（依赖安装或源码获取失败）"
    if exit_code == "0":
        return "✅", "PASSED", "turquoise", "所有兼容性测试通过"
    return "❌", "FAILED", "red", "部分兼容性测试失败，请检查"


def _collect_failures() -> tuple[list[str], int, int]:
    """Parse the JUnit report. Returns (failure lines, total, failed)."""
    failed_lines: list[str] = []
    total = 0
    failed = 0
    try:
        root = ET.parse(_REPORT_PATH).getroot()
    except (OSError, ET.ParseError):
        return failed_lines, total, failed

    for case in root.iter("testcase"):
        total += 1
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        failed += 1
        name = case.get("name", "unknown")
        message = (failure.get("message") or "")[:_MAX_FAILURE_MESSAGE]
        failed_lines.append(f"- `{name}`: {message}")
    return failed_lines, total, failed


def _build_card(*, version: str, color: str, elements: list[dict]) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"Hermes 集成测试·{version}"},
            "template": color,
        },
        "body": {"elements": elements},
    }


def main() -> int:
    webhook = _read("FEISHU_WEBHOOK")
    secret = _read("FEISHU_SIGN_SECRET")
    if not webhook or not secret:
        print("Feishu webhook not configured; skipping notification.")
        return 0

    version = _read("HERMES_VERSION", "unknown")
    outcome = _read("TEST_OUTCOME", "skipped")
    exit_code = _read("TEST_EXIT_CODE")
    repo = _read("REPO")
    run_id = _read("RUN_ID")
    server = _read("SERVER_URL", "https://github.com")
    trigger = _read("TRIGGER", "schedule")
    will_update = _read("WILL_UPDATE", "false")

    run_url = f"{server}/{repo}/actions/runs/{run_id}"
    trigger_label = "⏰ 定时触发" if trigger == "schedule" else "👆 手动触发"
    update_label = "✅ 已更新" if will_update == "true" else "⏭ 未更新"

    status_icon, status_text, color, summary = _status(outcome, exit_code)
    failed_lines, total, failed = _collect_failures()
    if total > 0:
        summary = f"{total} tests, {failed} failed — {summary}"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**Hermes 版本**: `{version}`\n"
                    f"**测试结果**: {status_icon} {status_text}\n"
                    f"**触发方式**: {trigger_label}\n"
                    f"**版本记录**: {update_label}\n"
                    f"**汇总**: {summary}"
                ),
            },
        },
    ]

    if failed_lines:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**❌ 失败用例**:\n" + "\n".join(failed_lines[:_MAX_FAILED_LINES]),
            },
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "🔗 查看 Actions 详情"},
        "behaviors": [{"type": "open_url", "default_url": run_url}],
        "type": "primary",
    })

    timestamp = str(int(time.time()))
    payload = {
        "timestamp": timestamp,
        "sign": _sign(secret, timestamp),
        "msg_type": "interactive",
        "card": _build_card(version=version, color=color, elements=elements),
    }

    try:
        request = urllib.request.Request(
            webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT_SEC) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"Failed to notify Feishu (HTTP {exc.code})", file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"Failed to notify Feishu ({type(exc).__name__})", file=sys.stderr)
        return 1

    code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
    if type(code) is not int or code != 0:
        # A webhook can return HTTP 200 for a rejected message. Keep the
        # configured webhook URL and untrusted body out of the runner log.
        label = code if type(code) is int else "missing or invalid code"
        print(f"Failed to notify Feishu ({label})", file=sys.stderr)
        return 1
    print("Feishu notified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
