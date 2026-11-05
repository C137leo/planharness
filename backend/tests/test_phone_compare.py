"""手机比价（进程内版）：不依赖真机，用假 adb + 假模型验证逻辑。

覆盖：找到价格、需要接管（登录/验证码）、模型乱输出、动作越权、步数超限。
"""

from __future__ import annotations

import json

import pytest

from app.food import phone_compare


class FakeClient:
    def __init__(self, actions: list[str]):
        self.actions = list(actions)
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        action = self.actions.pop(0) if self.actions else '{"action":"wait"}'
        return {"message": {"role": "assistant", "content": action}}


@pytest.fixture()
def fake_adb(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(phone_compare, "ADB_BIN", "/bin/echo")
    monkeypatch.setattr(phone_compare, "DEVICE", "fake-device")
    monkeypatch.setattr(phone_compare, "screenshot_b64", lambda: "ZmFrZQ==")

    def fake_run(cmd, capture_output=False, timeout=None):
        calls.append(tuple(cmd))
        class R:
            returncode = 0
            stdout = b"fake-device device"
            stderr = b""
        return R()

    monkeypatch.setattr(phone_compare.subprocess, "run", fake_run)
    # 只替换 adb 执行层，保留 execute() 的动作白名单校验
    monkeypatch.setattr(phone_compare, "adb", lambda *args, **kwargs: calls.append(args) or "")
    return calls


def test_finds_price(fake_adb) -> None:
    client = FakeClient([
        '{"action":"tap","x":10,"y":20,"why":"搜索"}',
        '{"action":"done","result":{"product":"鸡胸肉沙拉","price":19.9,"store":"轻食店"}}',
    ])
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client)
    assert result["status"] == "ok"
    assert result["data"]["result"]["price"] == 19.9
    assert result["data"]["steps"] == 2


def test_login_requires_takeover(fake_adb) -> None:
    client = FakeClient(['{"action":"takeover","reason":"需要登录"}'])
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client)
    assert result["status"] == "needs_takeover"
    assert result["error"] == "login_or_captcha"


def test_illegal_action_is_rejected(fake_adb) -> None:
    client = FakeClient(['{"action":"install_payment"}'])
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client)
    assert result["status"] == "error"
    assert "action" in result["error"] or result["error"].startswith("action_failed")


def test_bad_json_is_error_not_crash(fake_adb) -> None:
    client = FakeClient(["我看不清这张图"])
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client)
    assert result["status"] == "error"
    assert result["error"].startswith("model_error")


def test_repeated_action_requires_takeover(fake_adb) -> None:
    """同一个动作连续 3 次 = 卡住，转人工，而不是烧完所有步数。"""
    client = FakeClient(['{"action":"wait"}'] * 5)
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client, max_steps=6)
    assert result["status"] == "needs_takeover"
    assert result["error"] == "stuck_repeating"


def test_max_steps(fake_adb) -> None:
    # 动作各不相同（坐标变化），不会触发重复检测
    client = FakeClient([f'{{"action":"tap","x":{i},"y":{i}}}' for i in range(6)])
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=client, max_steps=3)
    assert result["status"] == "error"
    assert result["error"] == "max_steps_exceeded"


def test_without_model_returns_error() -> None:
    result = phone_compare.compare_prices("鸡胸肉沙拉", client=None)
    assert result["status"] == "error"
    assert result["error"] in {"model_unavailable", "adb_not_found", "device_offline"}
