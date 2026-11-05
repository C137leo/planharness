"""模型输出的结构校验（Pydantic）。

为什么不只靠 json.loads：
- json.loads 只保证"是 JSON"，不保证字段类型对；
- 用 Pydantic 定义 schema，字段缺失/类型错会给出具体路径，
  既能作为 repair 的依据，也能在日志里说清"哪里不对"。

注意：**餐食不从模型输出里取**。模型只写 summary/schedule/tips/nutrition_notes，
meals 由代码用工具结果拼装——这样输出体积小一半，从根上减少被截断的概率。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ScheduleItem(BaseModel):
    time: str = ""
    activity: str = ""
    why: str = ""


class Tip(BaseModel):
    text: str
    source_ids: list[str] = Field(default_factory=list)


class TodayAnswer(BaseModel):
    summary: str = Field(min_length=1, max_length=300)
    schedule: list[ScheduleItem] = Field(default_factory=list)
    tips: list[Tip] = Field(default_factory=list)
    nutrition_notes: list[Tip] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary 不能为空")
        return value
