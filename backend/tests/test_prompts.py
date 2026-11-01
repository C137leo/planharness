from app.llm.prompts import build_explanation_payload


def test_build_explanation_payload_maps_operations():
    payload = build_explanation_payload(
        reason_text="膝盖疼痛",
        base_version=1,
        operations=[
            {
                "op": "replace_action",
                "slot_key": "1:strength:1",
                "before_action_ref": "goblet_squat",
                "after_action": {"action_ref": "glute_bridge"},
                "reason": "膝痛",
            },
            {
                "op": "remove_action",
                "slot_key": "5:strength:1",
                "before_action_ref": "row",
                "reason": "只能练 2 天",
            },
        ],
        diff={"changed_slots": 2, "total_slots": 12, "churn": 0.1667, "unchanged_ratio": 0.8333},
        safety=[],
    )


    assert payload["changes"][0]["to"] == "glute_bridge"
    assert payload["changes"][1]["to"] is None
    assert payload["safety"]["passed"] is True
    print(payload)



import json

from app.llm.prompts import build_messages


def test_build_messages_has_system_and_user():
    payload = {"reason": "右膝疼痛", "changes": []}
    messages = build_messages(payload)

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "只输出 JSON" in messages[0]["content"]

    # user content 必须能还原成原 payload
    assert json.loads(messages[1]["content"]) == payload