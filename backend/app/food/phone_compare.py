"""外卖比价（进程内版）：ADB 截屏 → 模型决定点哪里 → 执行动作 → 读价格。

为什么不用 orderwise 的 MCP Server：
- 它依赖 mcp/fastmcp/openai/PIL/pymongo，我们环境装不上；
- 真正有用的只有"截图→动作"这个循环，其余是传输和持久化框架；
- 这里复用现有 httpx（`ToolCallingClient`）和系统 adb，零新依赖。

安全边界：只允许 tap/swipe/text/back/home；不安装、不支付、不代下单。
遇到登录/验证码 → 返回 needs_takeover，交给用户处理。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

ADB_BIN = os.environ.get("ADB_BIN") or shutil.which("adb") or "/opt/homebrew/bin/adb"
DEVICE = os.environ.get("ORDERWISE_DEVICE", "103.36.203.208:500")
MAX_STEPS = int(os.environ.get("ORDERWISE_MAX_STEPS", "12"))
STEP_TIMEOUT = float(os.environ.get("ORDERWISE_STEP_TIMEOUT", "30"))
ALLOWED_ACTIONS = {"tap", "swipe", "text", "back", "home", "wait", "done", "takeover"}
TAKEOVER_HINTS = ("登录", "验证码", "滑块", "验证", "权限", "定位")

PROMPT = """你在帮我用手机 App 查一个商品的到手价。
每轮我给你一张截图和当前目标，你只输出一个 JSON 动作：
{"action":"tap","x":123,"y":456,"why":"打开搜索"}
{"action":"text","text":"鸡胸肉沙拉"}
{"action":"swipe","x1":..,"y1":..,"x2":..,"y2":..}
{"action":"back"} / {"action":"home"} / {"action":"wait"}
{"action":"takeover","reason":"需要登录或验证码"}
{"action":"done","result":{"product":"..","price":12.9,"delivery_fee":2,"packing_fee":1,"store":".."}}
规则：只输出 JSON；每步只做一个动作；找到价格立刻 done；遇到登录/验证码立刻 takeover。
坐标是截图的像素坐标（左上为 0,0）。
"""


@dataclass
class Step:
    action: str
    detail: dict


def adb(*args: str, timeout: float = STEP_TIMEOUT) -> str:
    proc = subprocess.run([ADB_BIN, "-s", DEVICE, *args], capture_output=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "ignore")[:200] or "adb failed")
    return proc.stdout.decode("utf-8", "ignore")


def screenshot_b64() -> str:
    proc = subprocess.run([ADB_BIN, "-s", DEVICE, "exec-out", "screencap", "-p"],
                          capture_output=True, timeout=STEP_TIMEOUT)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("screenshot failed")
    return base64.b64encode(proc.stdout).decode()


def execute(step: dict) -> None:
    action = str(step.get("action") or "")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"action not allowed: {action}")
    if action == "tap":
        adb("shell", "input", "tap", str(int(step["x"])), str(int(step["y"])))
    elif action == "swipe":
        adb("shell", "input", "swipe", str(int(step["x1"])), str(int(step["y1"])),
            str(int(step["x2"])), str(int(step["y2"])), str(int(step.get("ms", 300))))
    elif action == "text":
        raw = str(step.get("text") or "")
        try:
            adb("shell", "input", "text", raw.replace(" ", "%s"))
        except RuntimeError:
            # adb input text 不支持中文；需要手机装 ADBKeyboard，
            # 用它的广播输入。没装就明确报错，让上层转人工。
            try:
                adb("shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
                    "--es", "msg", raw)
            except RuntimeError as exc:
                raise RuntimeError(
                    "中文输入失败：请在手机上安装 ADBKeyboard 并用 adb 设为默认输入法"
                ) from exc
    elif action == "back":
        adb("shell", "input", "keyevent", "4")
    elif action == "home":
        adb("shell", "input", "keyevent", "3")
    elif action == "wait":
        time.sleep(float(step.get("seconds", 1.5)))


def _parse(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    left, right = text.find("{"), text.rfind("}")
    if left == -1 or right <= left:
        raise ValueError("model did not return json action")
    data = json.loads(text[left:right + 1])
    if not isinstance(data, dict):
        raise ValueError("action must be object")
    return data


def compare_prices(product_name: str, *, apps: list[str] | None = None,
                   client=None, max_steps: int | None = None) -> dict:
    """在手机 App 里搜商品，返回价格。失败一律返回结构化错误，不抛到接口层。"""
    apps = apps or ["美团"]
    if not os.path.exists(ADB_BIN):
        return {"status": "error", "error": "adb_not_found", "retryable": False}
    try:
        devices = subprocess.run([ADB_BIN, "devices"], capture_output=True, timeout=10)
        if DEVICE not in devices.stdout.decode("utf-8", "ignore"):
            return {"status": "error", "error": "device_offline", "retryable": True}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"adb_error:{type(exc).__name__}", "retryable": True}
    if client is None:
        return {"status": "error", "error": "model_unavailable", "retryable": True}

    steps = max_steps or MAX_STEPS
    trace: list[dict] = []
    sticky: tuple | None = None
    repeat = 0
    for index in range(steps):
        try:
            shot = screenshot_b64()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"screenshot_failed:{type(exc).__name__}",
                    "retryable": True, "trace": trace}
        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": (
                    f"目标：在 {apps[0]} 搜索「{product_name}」并读到价格。第 {index + 1} 步。"
                    + ("上一次动作无效或重复，请换一个动作（例如点输入框后用 text 输入，"
                       "再点搜索）。" if repeat >= 2 else "")
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot}"}},
            ]},
        ]
        try:
            reply = client.complete(messages, tools=None, max_tokens=300)
            step = _parse((reply.get("message") or {}).get("content") or "")
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"model_error:{type(exc).__name__}",
                    "retryable": True, "trace": trace}
        trace.append({"step": index + 1, "action": step.get("action"),
                      "why": str(step.get("why") or "")[:40]})

        # 同一步反复点同一个位置 = 卡住：先提示换动作，连续 3 次交给人
        signature = (step.get("action"), step.get("x"), step.get("y"))
        repeat = repeat + 1 if signature == sticky else 1
        sticky = signature
        if repeat >= 3:
            return {"status": "needs_takeover", "error": "stuck_repeating",
                    "reason": f"连续 {repeat} 次相同动作：{signature[0]}，可能界面没响应",
                    "retryable": True, "trace": trace}
        action = str(step.get("action") or "")
        if action == "done":
            result = step.get("result") or {}
            return {"status": "ok", "data": {"product": product_name, "platform": apps[0],
                                             "result": result, "steps": index + 1},
                    "trace": trace}
        if action == "takeover":
            return {"status": "needs_takeover", "error": "login_or_captcha",
                    "reason": str(step.get("reason") or "")[:80], "retryable": True,
                    "trace": trace}
        try:
            execute(step)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"action_failed:{type(exc).__name__}",
                    "retryable": True, "trace": trace}
    return {"status": "error", "error": "max_steps_exceeded", "retryable": True, "trace": trace}
