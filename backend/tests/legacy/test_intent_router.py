"""分层意图理解的单元测试（不联网）。

重点验证：
- stage2 抽参失败时保留 stage1 的 intent；
- stage1 输出非法 intent 时用修复请求，而不是丢弃；
- 没有客户端时回退到规则 intent，而不是抛异常。
"""

from __future__ import annotations

import json

from app.agent import intent_router


class FakeClient:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        content = self.contents.pop(0) if self.contents else ""
        return {"message": {"content": content}}


def _stage1(intent: str = "adjust_today", scope: str = "today",
            clarify: bool = False, missing: list[str] | None = None) -> str:
    return json.dumps({
        "intent": intent, "scope": scope, "confidence": 0.9,
        "reason": "test", "clarification_needed": clarify,
        "missing_slots": missing or [],
    }, ensure_ascii=False)


def test_stage2_failure_keeps_intent() -> None:
    client = FakeClient([_stage1(), "not-json", "still-not-json"])
    result = intent_router.understand(client, "今天状态好，加一点强度", context={})
    assert result.intent == "adjust_today"
    assert result.clarification_needed is True
    assert "stage2" in result.error


def test_invalid_intent_is_repaired() -> None:
    client = FakeClient([
        json.dumps({"intent": "unknown_thing", "scope": "today"}),
        _stage1("adjust_today", "today"),
        json.dumps({"slots": {"harder": True}, "missing_slots": [],
                    "clarification_needed": False}),
    ])
    result = intent_router.understand(client, "再来两组", context={})
    assert result.intent == "adjust_today"
    assert result.slots.get("harder") is True
    assert result.clarification_needed is False


def test_stage1_missing_slot_is_reconciled_by_stage2() -> None:
    # 模型 stage1 先写了“缺调整方向”，stage2 抽到了 harder：不能丢任务。
    client = FakeClient([
        _stage1("adjust_today", "today", clarify=True, missing=["intensity_level"]),
        json.dumps({"slots": {"harder": True}, "missing_slots": ["intensity_level"],
                    "clarification_needed": True}),
    ])
    result = intent_router.understand(client, "今天状态好，猛猛练", context={})
    assert result.intent == "adjust_today"
    assert result.slots.get("harder") is True
    assert result.missing_slots == []
    assert result.clarification_needed is False


def test_no_client_returns_fallback_intent() -> None:
    result = intent_router.understand(None, "随便说点什么", context={},
                                      fallback_intent="clarify")
    assert result.source == "fallback"
    assert result.intent == "clarify"
    assert result.error == "client_unavailable"


def test_secondary_intents_and_user_goal_are_kept() -> None:
    client = FakeClient([
        json.dumps({
            "intent": "injury_advice", "scope": "today", "confidence": 0.95,
            "reason": "腰疼 + 今天想练上半身", "user_goal": "安全地改练上半身",
            "secondary_intents": ["adjust_today"],
            "clarification_needed": False, "missing_slots": [],
        }, ensure_ascii=False),
        json.dumps({"slots": {"injured_regions": ["lower_back"]},
                    "missing_slots": [], "clarification_needed": False}),
    ])
    result = intent_router.understand(
        client, "腰疼，今天不想练腿，想练上半身", context={},
    )
    assert result.intent == "injury_advice"
    assert result.secondary_intents == ["adjust_today"]
    assert result.user_goal == "安全地改练上半身"


def test_task_context_reads_pending_quick_replies() -> None:
    from app.agent.legacy_coach_agent import _task_context

    ctx = _task_context([
        {"role": "user", "text": "以后别安排深蹲"},
        {"role": "agent", "text": "确认后我再写入档案", "intent": "update_preference",
         "actions": [{"type": "quick_reply", "label": "确认更新档案"}]},
    ])
    assert ctx["last_agent_intent"] == "update_preference"
    assert ctx["awaiting_confirmation"] is True
    assert ctx["pending_actions"][0]["label"] == "确认更新档案"
