"""多任务拆解的数据模型。

目标：一次用户输入可以产生一个 TaskPlan，里面包含多个可独立执行的 AtomicTask；
每个任务有自己的参数、状态和结果，最后由 orchestrator 汇总，而不是让 run()
只挑一个 primary_intent 执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AtomicTask:
    task_id: str
    domain: str
    intent: str
    operation: str
    action_type: str = "update"
    scope: str = "today"
    slots: dict = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    clarification_needed: bool = False
    dependencies: list[str] = field(default_factory=list)
    task_order: int = 1
    user_goal: str = ""
    confidence: float = 0.0
    context_reference: dict = field(default_factory=dict)
    status: str = "pending"


@dataclass
class TaskPlan:
    tasks: list[AtomicTask]
    original_text: str = ""
    context_snapshot: dict = field(default_factory=dict)
    overall_status: str = "pending"


@dataclass
class TaskResult:
    task_id: str
    status: str = "success"
    message: str = ""
    changes: list[str] = field(default_factory=list)
    error: str = ""
    needs_confirmation: bool = False
    payload: dict = field(default_factory=dict)
