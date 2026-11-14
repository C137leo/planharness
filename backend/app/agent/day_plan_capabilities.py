"""Typed day-revision menu; semantic planning stays with the model."""
from typing import Literal
from pydantic import Field
from app.agent.binding_contract import StrictModel
from app.agent.capabilities import Capability, NoArgs
from app.domain.day_revision import TargetProposal, Constraints


class CreateRevision(StrictModel):
    workout_policy: Literal['preserve_current','replace_remaining'] = 'preserve_current'
    replan_authorization_id: str | None = None
    expected_base_revision: str = Field(min_length=64,max_length=64)
    scope: list[Literal['workout','breakfast','lunch','dinner','snack']] = Field(min_length=1,max_length=5)
    proposal: TargetProposal
    constraints: Constraints = Field(default_factory=Constraints)


class RevisionVersion(StrictModel):
    expected_revision: str = Field(min_length=64,max_length=64)


class UpdateRevision(RevisionVersion):
    proposal: TargetProposal
    constraints: Constraints | None = None


class ExtendRevision(UpdateRevision):
    additional_scope: list[Literal['breakfast','lunch','dinner','snack']] = Field(min_length=1,max_length=4)


class RequestReplan(StrictModel):
    expected_base_revision: str = Field(min_length=64,max_length=64)
    scope: list[Literal['workout','breakfast','lunch','dinner','snack']] = Field(default_factory=lambda:['workout'])


CAPABILITIES = {c.capability_id:c for c in [
    Capability('day_plan.request_replan','day_revision','请求确认放弃原训练方案未完成部分，保留已记录训练，之后再制定新目标。selected_id为day，复制revision_base_revision。此操作不放弃旧安排、不补记完成，系统先展示并保存确认。',RequestReplan,('day',),True,'never','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.confirm_replan','day_revision','仅确认已经展示的重排选择。selected_id为workout_replan，expected_revision复制etag；确认后允许生成新方案预览，尚不应用新方案。',RevisionVersion,('workout_replan',),True,'always','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.create_revision','day_revision',
        '创建协调后的当天草案，不应用。保留当前安排局部调整用preserve_current；明确重排新目标用replace_remaining且必须给已approved的replan_authorization_id，不能复用旧记录计新方案进度。selected_id为day，expected_base_revision复制day.revision_base_revision。scope明确训练workout和各餐次；proposal.actions完整目标状态，proposal.meals为各授权餐次完整食材。',
        CreateRevision,('day',),True,'never','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.preview_revision','day_revision','查看真实草案与结构化差异，不应用。',NoArgs,('day_revision',),False,'never','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.update_revision','day_revision',
        '修改同一个草案。expected_revision复制草案etag；未传入的训练/餐次/约束保留此前选择，actions传入时是完整目标列表，meals传入的餐次是该餐完整组成。不允许扩大原scope。编辑废止旧确认。',
        UpdateRevision,('day_revision',),True,'never','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.extend_revision','day_revision',
        '用户追加原草案范围外的餐次要求时，扩展同一草案并更新该餐的完整组成。additional_scope只列本轮明确新增餐次；保留原训练与已有餐食及约束，不重建草案，不复用训练授权作为餐食授权。仅更新预览，废止旧确认，最终应用仍另行确认。',
        ExtendRevision,('day_revision',),True,'never','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.apply_revision','day_revision','仅在用户确认后应用真实草案，expected_revision复制最新etag。训练和饮食同一事务提交，保护完成事实。不可与create/update放同一轮，必须先展示差异再确认。',
        RevisionVersion,('day_revision',),True,'always','day_revision',execution_boundary='day_revision'),
    Capability('day_plan.cancel_revision','day_revision','取消尚未应用的草案，不改当天实际计划；expected_revision复制etag。',
        RevisionVersion,('day_revision',),True,'never','day_revision',execution_boundary='day_revision'),
]}
