"""Actual training facts, isolated from the original fourteen plan capabilities."""
from typing import Literal
from pydantic import Field, model_validator
from app.agent.binding_contract import StrictModel
from app.agent.capabilities import Capability, NoArgs


class FinishSession(StrictModel):
    expected_revision: str = Field(min_length=64, max_length=64)
    completion_basis: Literal['recorded_only','confirm_current_plan'] = 'recorded_only'
    item_overrides: list['CompletionOverride'] = Field(default_factory=list, max_length=100)
    unfinished_policy: Literal['preserve_unrecorded','skip_remaining'] = 'preserve_unrecorded'
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    fatigue: int | None = Field(default=None, ge=1, le=5)
    note: str = Field(default='', max_length=1000)


class CompletionOverride(StrictModel):
    item_id: str
    actual_sets: int = Field(ge=0,le=100)
    reps: int | None = Field(default=None,ge=0,le=200)
    duration_min: int | None = Field(default=None,ge=0,le=1440)


class RecordProgress(StrictModel):
    expected_revision: str = Field(min_length=64,max_length=64)
    completion_basis: Literal['as_planned','actual'] = 'as_planned'
    actual_sets: int | None = Field(default=None,ge=0,le=100)
    reps: int | None = Field(default=None,ge=0,le=200)
    duration_min: int | None = Field(default=None,ge=0,le=1440)

    @model_validator(mode='after')
    def exact_count(self):
        if self.completion_basis=='actual' and self.actual_sets is None:
            raise ValueError('实际模式必须给出已完成总组数')
        return self


class CorrectRecord(StrictModel):
    expected_revision: str = Field(min_length=64, max_length=64)
    actual_sets: int | None = Field(default=None, ge=0, le=100)
    set_no: int | None = Field(default=None, ge=1, le=100)
    reps: int | None = Field(default=None, ge=0, le=200)
    weight_kg: float | None = Field(default=None, ge=0, le=500)
    completion_state: Literal['done','partial','skipped','unrecorded'] | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def exact_set(self):
        if (self.reps is not None or self.weight_kg is not None) and self.set_no is None:
            raise ValueError('次数/重量纠错必须指定真实组号')
        if self.actual_sets is None and self.reps is None and self.weight_kg is None and self.completion_state is None:
            raise ValueError('没有提供实际记录修订')
        return self


CAPABILITIES={c.capability_id:c for c in [
    Capability('workout.finish_session','workout_lifecycle',
        '结束训练并确认保存。明确表示全部做完：completion_basis=confirm_current_plan，展示今天会话最新目标组数/次数/默认时长，确认后按用户自报完成保存；用户修正某项目用item_overrides真实session_item ID和实际组数。仅结束/提前停止/不补记录：recorded_only。重量可未知，不强制逐组填。expected_revision复制session.record_revision。',
        FinishSession,('session',),True,'always','workout_lifecycle',execution_boundary='workout_lifecycle'),
    Capability('workout.correct_record','workout_lifecycle',
        '独立修订已结束训练的一个真实条目，保留审计；不修改计划或其他条目。actual_sets是实际总组数，set_no是实际组号；缺失次数/重量不得用计划值补。expected_revision复制所属session.record_revision，必须确认。',
        CorrectRecord,('session_item',),True,'always','workout_lifecycle',execution_boundary='workout_lifecycle'),
    Capability('workout.read_records','workout_lifecycle','查询一个真实训练会话及其已持久化日志、逐组数据和未记录项目，不修改。',
        NoArgs,('session',),False,'never','workout_lifecycle',execution_boundary='workout_lifecycle'),
    Capability('workout.record_progress','workout_lifecycle',
        '记录进行中一个session_item的完成进度。用户明确说该动作/热身完成用as_planned按当前目标记录；明确实际做几组用actual并提供actual_sets总数。保留已记录组，不重复累计；不结束整场、不修改计划。expected_revision复制父session.record_revision，reps/时长用户明确提供时才填，否则保留计划依据或未知。',
        RecordProgress,('session_item',),True,'uncertain','workout_lifecycle',execution_boundary='workout_lifecycle'),
]}
