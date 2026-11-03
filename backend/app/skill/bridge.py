"""与 healthy-fitness-coach skill 的桥接层。

设计原则（为什么要这么绕）：

1. **skill 是真源**：训练计划、安全门、今日适配、营养目标、配餐都在 skill 的 Node
   引擎里。复制到 Python 一定会在某次改动后与真源漂移，所以这里只做协议转换。
2. **失败要显式降级**：skill 目录不在、node 不可用、超时，都返回
   `{"status": "unavailable", ...}`，由上层决定用本地保守兜底，而不是抛异常让整个请求 500。
3. **可缓存**：同一个画像的 plan_program 在一次请求里可能被调用多次，
   缓存能省掉重复的 Node 启动（约 60ms 一次）。

环境变量：
- `SKILL_DIR`：skill 仓库路径（默认本机路径）
- `SKILL_NODE`：node 可执行文件（默认 `node`，找不到时试 `/usr/local/bin/node`）
- `SKILL_TIMEOUT`：单次调用超时秒数（默认 20）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_SKILL_DIR = "/Users/leo/PycharmProjects/healthy-fitness-coach-skill"
RUNNER = Path(__file__).with_name("runner.js")

# 这些命令的结果在一个进程内可复用（纯函数，不含时间/随机）
CACHEABLE = {"meta", "skill_md", "tools", "safety_gate", "screen_profile", "plan_program",
             "nutrition_targets", "nutrition_plan", "today_session", "review_week"}


class SkillError(RuntimeError):
    """skill 调用失败（区分于"skill 说不行"，后者是正常返回值）。"""


def _resolve_node() -> str:
    configured = os.environ.get("SKILL_NODE")
    if configured:
        return configured
    found = shutil.which("node")
    if found:
        return found
    for candidate in ("/usr/local/bin/node", "/opt/homebrew/bin/node"):
        if Path(candidate).exists():
            return candidate
    return ""


class SkillBridge:
    """把 skill 的确定性引擎暴露成 Python 方法。每个方法返回 dict。"""

    def __init__(self, skill_dir: str | None = None, node: str | None = None,
                 timeout: float | None = None) -> None:
        self.skill_dir = skill_dir or os.environ.get("SKILL_DIR") or DEFAULT_SKILL_DIR
        self.node = node if node is not None else _resolve_node()
        self.timeout = timeout or float(os.environ.get("SKILL_TIMEOUT", "20"))
        self._cache: dict[str, Any] = {}

    # ---- 基础设施 ----

    @property
    def available(self) -> bool:
        return bool(self.node) and RUNNER.exists() and Path(self.skill_dir, "agent-integration", "coach.js").exists()

    def _run(self, command: str, payload: dict | None = None) -> dict:
        if not self.available:
            return {
                "status": "unavailable",
                "error": "skill_not_available",
                "detail": f"node={self.node!r} dir={self.skill_dir!r}",
            }
        key = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
        cache_key = f"{command}:{key}"
        if command in CACHEABLE and cache_key in self._cache:
            return self._cache[cache_key]

        env = {**os.environ, "SKILL_DIR": self.skill_dir}
        try:
            proc = subprocess.run(
                [self.node, str(RUNNER), command],
                input=json.dumps(payload or {}, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                cwd=str(Path(__file__).parent),
            )
        except subprocess.TimeoutExpired as exc:
            return {"status": "unavailable", "error": "skill_timeout", "detail": str(exc)}

        if proc.returncode != 0:
            return {
                "status": "unavailable",
                "error": "skill_exit_nonzero",
                "detail": (proc.stdout or proc.stderr or "").strip()[:500],
            }
        try:
            result = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"status": "unavailable", "error": "skill_bad_json",
                    "detail": (proc.stdout or "")[:500]}
        if command in CACHEABLE:
            self._cache[cache_key] = result
        return result

    # ---- 业务方法 ----

    def meta(self) -> dict:
        return self._run("meta")

    def skill_md(self) -> str:
        return str(self._run("skill_md").get("content") or "")

    def tools(self) -> list[dict]:
        return list(self._run("tools").get("tools") or [])

    def safety_gate(self, instruction: str, profile: dict | None = None,
                    current_state: dict | None = None) -> dict:
        return self._run("safety_gate", {
            "instruction": instruction,
            "profile": profile or {},
            "currentState": current_state or {},
        })

    def screen_profile(self, profile: dict, current_state: dict | None = None,
                       instruction: str = "") -> dict:
        return self._run("screen_profile", {
            "profile": profile,
            "currentState": current_state or {},
            "instruction": instruction,
        })

    def plan_program(self, profile: dict, instruction: str = "") -> dict:
        return self._run("plan_program", {"profile": profile, "instruction": instruction})

    def today_session(self, profile: dict, program: dict | None = None,
                      current_state: dict | None = None, session_id: str | None = None,
                      instruction: str = "") -> dict:
        return self._run("today_session", {
            "profile": profile,
            "program": program or {},
            "currentState": current_state or {},
            "sessionId": session_id,
            "instruction": instruction,
        })

    def nutrition_targets(self, profile: dict, body: dict, mode: str = "simplified") -> dict:
        return self._run("nutrition_targets", {"profile": profile, "body": body, "mode": mode})

    def nutrition_plan(self, profile: dict, mode: str = "simplified") -> dict:
        return self._run("nutrition_plan", {"profile": profile, "mode": mode})

    def review_week(self, records: list, **extra: Any) -> dict:
        return self._run("review_week", {"records": records, **extra})

    def read_reference(self, name: str) -> dict:
        return self._run("read_reference", {"name": name})


@lru_cache(maxsize=1)
def get_bridge() -> SkillBridge:
    """进程级单例：懒加载，测试可以注入自己的实例。"""
    return SkillBridge()
