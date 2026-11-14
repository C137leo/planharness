"""Versioned object binding, deliberately without a confidence authorization field."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Resolution(StrictModel):
    status: Literal["resolved", "candidate", "ambiguous", "unresolved"]
    selected_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list, max_length=24)
    basis: Literal["explicit", "ordinal", "history", "inferred", "none"] = "none"
    explanation: str = ""
    question: str = ""


class BoundTask(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    capability_id: str
    user_goal: str
    resolution: Resolution
    arguments: dict = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=16)
    needs_confirmation: bool = False
    parameters_resolved: bool = Field(default=True, description="只判断操作类型及 arguments 是否明确完整；目标对象是否唯一由 resolution 独立表达，不能因为有多个候选而把此字段置 false。")


class ContextRequest(StrictModel):
    day_revision: bool = False
    commerce: bool = False
    dates: list[str] = Field(default_factory=list, max_length=3)
    exercise_catalog: bool = False
    history_limit: int = Field(default=12, ge=1, le=50)


class BoundPlan(StrictModel):
    continue_after_tools: bool = False
    snapshot_id: str
    mode: Literal["execute", "propose", "noop", "load_context"] = "execute"
    question: str = ""
    answer: str = ""
    unfulfilled_goals: list[str] = Field(default_factory=list, max_length=16)
    tasks: list[BoundTask] = Field(default_factory=list, max_length=16)
    context_request: ContextRequest | None = None


class PendingControl(StrictModel):
    action: Literal["confirm", "revise", "reject", "cancel", "new_request", "clarify"]
    pending_id: str
    version: int = Field(ge=1)
    new_request_text: str = Field(default="", max_length=2000)
    execute_revision: bool = False
    # Revision can only retain existing tasks and choose one of their saved candidates.
    keep_task_ids: list[str] = Field(default_factory=list, max_length=16)
    selections: dict[str, str] = Field(default_factory=dict)
    parameter_resolutions: list[str] = Field(default_factory=list, description='仅确认已保存且完整的原参数含义；不能靠此字段补充、替换或生成缺失arguments。缺失参数的回答必须交给new_request重新绑定。')
    question: str = ""
    edit_task_ids: list[str] = Field(default_factory=list,max_length=16)


class NaturalPendingControl(PendingControl):
    action: Literal['confirm','revise','reject','cancel','new_request','clarify'] = Field(description='暂缓执行但保留草案/预览用clarify；明确放弃整份方案才用reject或cancel。确认只批准保存任务定义的当前阶段。')
    execute_revision: bool = Field(description="用户是否明确授权立即执行修订后的任务子集。明确要求保留某些对象并执行其他已保存目标属于部分授权，应为 true；只是选择候选供预览则为 false。")


class DraftDisposition(StrictModel):
    action: Literal['retain','discard']
    reply: str


class MixedAuthorization(StrictModel):
    approved_task_ids: list[str]
    new_request_text: str
    modified_task_ids: list[str] = Field(default_factory=list,max_length=16)
    workflow_action: Literal['edit_preview','independent'] = 'independent'


class CheckoutAction(StrictModel):
    action: Literal["choose_take_way", "query", "cancel", "query_existing", "cancel_existing", "place_new"]
    checkout_id: str
    version: int = Field(ge=1)
    take_way_code: str = ""
    existing_checkout_id: str = Field(default="",max_length=64)
    decision_fingerprint: str = Field(default="",max_length=64)


class CommerceAction(StrictModel):
    action: Literal["connect", "nutrition", "search_stores", "menu", "product_detail", "prepare_order", "checkout_menu", "revise_order"]
    target_id: str = Field(default="", max_length=64)
    city: str = Field(default="", max_length=80)
    keyword: str = Field(default="", max_length=120)
    items: list[dict] = Field(default_factory=list, max_length=20)
    take_way_code: str = Field(default="", max_length=80)
    version: int | None = Field(default=None,ge=1)
    checkout_id: str = Field(default='',max_length=64)
