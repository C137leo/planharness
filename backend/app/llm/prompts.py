import json

SYSTEM_PROMPT = """你是健身教练。你只负责解释一份【已经由代码生成、并已通过安全校验】的训练计划补丁。

硬性边界：
- 不提出新的动作或新的变更，只解释 changes 里已经发生的替换/删除；
- 不允许改变计划内容，你只是"解释"它，不是"生成"它；
- 不做疾病诊断，不做医疗建议；
- 不承诺任何疗效（如"保证增肌""一定能瘦"）；
- 只输出 JSON，不要输出任何 JSON 以外的文字。

输出 JSON 格式（四个字段均为字符串，每项不超过 60 字）：
{"summary": "一句话说明改了什么", "why": "为什么这样改", "risk": "需要注意什么", "next": "用户下一步做什么"}
"""

def build_explanation_payload(*,reason_text: str,base_version:int,operations:list[dict],diff:dict,safety:list[str])->dict:
    changes = []
    for op in operations:
        if op["op"] == "replace_action":
            changes.append(
                {
                    "from": op.get("before_action_ref"),
                    "to": op.get("after_action",{}).get("action_ref"),
                    "reason": op.get("reason",""),
                }
            )
        elif op["op"] == "remove_action":
            changes.append(
                {
                    "from": op.get("before_action_ref"),
                    "to": None,
                    "reason": op.get("reason",""),
                }
            )

    return {
        'reason':reason_text,
        'base_version':base_version,
        "changes":changes,
        "diff":{
            "changed_slots": diff.get("changed_slots"),
            "total_slots": diff.get("total_slots"),
            "churn": diff.get("churn"),
            "unchanged_ratio": diff.get("unchanged_ratio"),
        },
        "safety": {
            "passed": not safety,
            "violations": safety
        }
    }



def build_messages(payload:dict) -> list[dict]:

    return [
        {
            "role": "system", "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False)
        }
    ]




