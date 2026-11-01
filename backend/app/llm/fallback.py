def fallback_explanation(operations: list[dict], diff:dict)->str:
    parts: list[str] = []

    for op in operations:
        if op["op"] == "replace_action":
            before = op.get('before_action_ref') or "原动作"
            after = op.get('after_action',{}).get('action_ref') or "替代动作"
            parts.append(f"{before}替换为 {after}")
        elif op["op"] == "remove_action":
            before = op.get('before_action_ref') or "指定动作"
            parts.append(f"删除{before}")
    changed = int(diff.get("changed_slots") or 0)
    total = int(diff.get("total_slots") or 0)
    unchanged = total - changed
    ratio = diff.get("unchanged_ratio")
    ratio_text = f"{round(float(ratio)*100)}%" if ratio is not None else "-"
    changes_text = "；".join(parts) if parts else "本次没有可解释的变更"
    return (
        f"本次计划调整：{changes_text}。"
        f"共调整 {changed}/{total} 个槽位，保留 {unchanged} 个（{ratio_text}）。"
        "请确认身体没有不适；如训练中出现疼痛，请停止并咨询医生。"
    )

