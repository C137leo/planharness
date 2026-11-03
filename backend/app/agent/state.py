"""LangGraph 审批流程的状态定义。

字段全部 optional（total=False）：图在不同阶段只填充其中一部分 ——
`prepare` 之后才可能有 `patch_id`，`human_approval` 之后才有 `decision`。
"""

from typing import TypedDict


class PatchState(TypedDict, total=False):
    # 输入
    user_id: str
    plan_id: str
    available_days: int

    # prepare 产出
    patch_id: str
    run_id: str
    explanation: str
    diff: dict

    # human_approval 产出
    decision: str
    comment: str

    # commit / reject 产出
    version: int
    status: str

    # verify 产出（见 app/agent/verify.py）
    verified: bool
    verification: dict

    # rollback 产出（验证不通过时自动回滚）
    rollback_version: int
