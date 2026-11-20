"""The integration webhook acknowledges delivery only for a successful API response."""

import json
import runpy
from io import BytesIO
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("response", "exit_code"),
    [
        (b'{"code":0,"msg":"success"}', 0),
        (b'{"StatusCode":0,"StatusMessage":"success"}', 0),
        (b'{"code":19024,"msg":"Key Words Not Found","StatusCode":0}', 1),
        (b'{"msg":"missing code"}', 1),
        (b'<html>proxy error</html>', 1),
    ],
)
def test_webhook_checks_api_result(monkeypatch, capsys, response, exit_code):
    script = Path(__file__).resolve().parents[1] / "scripts" / "notify_integration_result.py"
    notify = runpy.run_path(str(script))
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://test.invalid/webhook")
    monkeypatch.setenv("FEISHU_SIGN_SECRET", "test-secret")
    monkeypatch.setenv("SERVER_URL", "https://github.com")
    monkeypatch.setenv("REPO", "test/repo")
    monkeypatch.setenv("RUN_ID", "123")

    def respond(request, *, timeout):
        assert timeout > 0
        card = json.loads(request.data)["card"]
        assert card["schema"] == "2.0"
        assert card["config"]["update_multi"] is True
        assert "elements" not in card
        elements = card["body"]["elements"]
        assert all(element["tag"] != "action" for element in elements)
        button = next(element for element in elements if element["tag"] == "button")
        assert button["behaviors"] == [{
            "type": "open_url", "default_url": "https://github.com/test/repo/actions/runs/123",
        }]
        return BytesIO(response)

    monkeypatch.setattr("urllib.request.urlopen", respond)
    assert notify["main"]() == exit_code
    captured = capsys.readouterr()
    if exit_code:
        assert "Failed to notify Feishu" in captured.err
        assert "Feishu notified" not in captured.out
    else:
        assert "Feishu notified" in captured.out
