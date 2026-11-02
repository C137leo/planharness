from app.llm.fallback import fallback_explanation


def test_fallback_lists_all_changes():
    text = fallback_explanation(
        operations=[
            {
                "op": "replace_action",
                "before_action_ref": "goblet_squat",
                "after_action": {"action_ref": "glute_bridge"},
            },
            {
                "op": "remove_action",
                "before_action_ref": "row",
            },
        ],
        diff={
            "changed_slots": 2,
            "total_slots": 12,
            "unchanged_ratio": 0.8333,
        },
    )
    assert "goblet_squat" in text
    assert "glute_bridge" in text
    assert "row" in text
    assert text.count("共调整") == 1
    assert "10 个" in text


def test_fallback_handles_empty_operations():
    text = fallback_explanation([], {"changed_slots": 0, "total_slots": 12})
    assert "没有可解释的变更" in text