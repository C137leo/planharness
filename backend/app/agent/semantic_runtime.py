"""Production: snapshot -> model -> validate -> confirm -> execute -> reload."""
from app.agent.task_executor import safe_error
import copy
import datetime as dt
import time
from sqlalchemy import select, update
from app.agent.binding_contract import BoundTask, PendingControl
from app.agent.business_snapshot import build_snapshot, business_state, digest
from app.agent import semantic_planner
from app.agent.capabilities import capability
from app.agent.task_executor import TaskFailure, execute_tasks, requires_confirmation, validate_tasks
from app.domain.timeutil import as_utc, user_today
from app.models import AgentRun, CoachBusinessSnapshot, CoachPendingPlan, Profile, User, utcnow, new_id


def lock_user(db, user):
    if db.bind.dialect.name == "sqlite":
        db.execute(update(User).where(User.id == user.id).values(id=User.id, updated_at=User.updated_at))
    else:
        db.scalar(select(User).where(User.id == user.id).with_for_update())
    # Session autoflush is disabled. Keep validated pending/preview metadata
    # before refreshing identities during a handoff; expire_all would drop it.
    db.flush()
    db.expire_all()


def pending_payload(p):
    return {"pending_id": p.id, "version": p.version, "status": p.status, "snapshot_id": p.snapshot_id,
            "stage":'clarification' if any(not t.get('parameters_resolved',True) for t in p.tasks) else 'confirmation',
            "tasks": p.tasks, "question": p.question, "expires_at": as_utc(p.expires_at).isoformat()}


def pending_result(p):
    labels = {'commerce.submit_order': '确认创建待支付订单', 'plan.confirm_initial_draft': '确认启用首份计划', 'plan.confirm_replacement_draft':'确认启用长期新计划',
              'day_plan.apply_revision': '确认应用新安排'}
    confirm_label = labels.get(p.tasks[0]['capability_id'], '确认') if len(p.tasks) == 1 else '确认以上修改'
    checkout=(p.result or {}).get('checkout') or {}
    ways=[{'type':'checkout_way','label':w['title'],'checkout_id':checkout['id'],'version':checkout['version'],'take_way_code':w['code']}
          for w in checkout.get('quote',{}).get('take_ways',[])] if checkout.get('status') in {'awaiting_confirmation','needs_selection'} else []
    return {"overall_status": "confirmation_required", "reply": p.question,
            **({'plan_draft':p.result['plan_draft']} if (p.result or {}).get('plan_draft') else {}),
            **({'day_revision':p.result['day_revision']} if (p.result or {}).get('day_revision') else {}),
            **({'workout_replan':p.result['workout_replan']} if (p.result or {}).get('workout_replan') else {}),
            **({'checkout':p.result['checkout']} if (p.result or {}).get('checkout') else {}),
            "pending_confirmation": pending_payload(p), "task_plan": p.tasks,
            "task_results": [{"task_id": t["task_id"], "status": "confirmation_required", "message": t["user_goal"]} for t in p.tasks],
            "actions": ways+([] if any(not t.get('parameters_resolved',True) for t in p.tasks) or (p.result or {}).get('unresolved_edits') or (p.result or {}).get('unresolved_initial_requirements') or (p.result or {}).get('unresolved_goal_input') else [{"type": "confirm_tasks", "label": confirm_label, "pending_id": p.id, "version": p.version}])+[
                        {"type": "cancel_tasks", "label": "取消", "pending_id": p.id, "version": p.version}]}


def pending_question(db, user, snapshot, tasks, question):
    names = {o["id"]: o.get("name") or o.get("title") or o.get("value") or o.get("action_ref") or {"execution":"上次修改","profile":"个人档案","day":"当天安排"}.get(o["type"],"已选择项目") for o in snapshot.payload["objects"]}
    lines = []
    for t in tasks:
        ids = t.resolution.candidate_ids or ([t.resolution.selected_id] if t.resolution.selected_id else [])
        lines.append(t.user_goal + "：" + (" / ".join(str(names[i]) for i in ids) or "尚未定位") +
                     ("；" + t.resolution.question if t.resolution.question else ""))
        if t.capability_id=='workout.replace_action' and t.parameters_resolved:
            source=next((o for o in snapshot.payload['objects'] if o['id']==t.arguments.get('exercise_id')),None)
            target=next((o for o in snapshot.payload['objects'] if o['id']==t.resolution.selected_id),None)
            if source and target:
                from app.domain.catalog import display_action_name
                lines[-1]+='\n待确认预览：'+display_action_name(target)+' → '+display_action_name({**source,'name':source.get('name_zh')})+'；确认后保存，当前安排尚未改变。'
        if t.capability_id in {'nutrition.record_meal', 'nutrition.correct_meal'}:
            labels = {'slot': '餐次', 'title': '实际进食', 'kcal': '热量(kcal)', 'protein_g': '蛋白质(g)', 'note': '备注'}
            slots = {'breakfast':'早餐', 'lunch':'午餐', 'dinner':'晚餐', 'snack':'加餐'}
            lines[-1] += '\n' + '；'.join(labels[k] + '：' + str(slots.get(v, v) if k == 'slot' else v)
                                        for k, v in t.arguments.items() if k in labels and v is not None)
            lines[-1] += '。仅记录实际进食，不改计划或推断其他餐已吃。'
        if t.capability_id=='day_plan.apply_revision' and t.resolution.selected_id:
            from app.domain import day_revision as revisions
            from app.agent.day_plan_runtime import describe
            lines[-1]+='\n'+describe(revisions.preview(db,user,t.resolution.selected_id))
        if t.capability_id=='day_plan.confirm_replan' and t.resolution.selected_id:
            from app.domain import workout_replan
            from app.agent.day_plan_runtime import replan_question
            lines[-1]+='\n'+replan_question(workout_replan.payload(workout_replan.owned(db,user,t.resolution.selected_id)))
        if capability(t.capability_id).execution_boundary=='workout_lifecycle':
            objects={o['id']:o for o in snapshot.payload['objects']}
            for id in ids:
                target=objects[id]
                items=[o for o in objects.values() if o['type']=='session_item' and o.get('parent_id')==id] if target['type']=='session' else [target]
                labels={'pending':'未记录','active':'进行中','partial':'部分完成','unrecorded':'未记录','done':'已完成','skipped':'已跳过'}
                lines[-1]+='\n当前实际记录：'+'；'.join(str(i.get('name') or '训练条目')+' '+str(i.get('completed_sets') or 0)+'/'+str(i.get('target_sets') or '?')+'组（'+labels.get(i.get('status'),'未知')+'）' for i in items)
            args=t.arguments;detail=[]
            if t.capability_id=='workout.finish_session':
                if args.get('completion_basis')=='confirm_current_plan' and t.parameters_resolved and t.resolution.selected_id:
                    from app.agent.workout_capabilities import FinishSession
                    from app.domain.workout_records import completion_preview,owned
                    projected=completion_preview(owned(db,user,t.resolution.selected_id),FinishSession.model_validate(args))
                    detail.append('按今日调整后的安排确认完成：'+'；'.join(str(i.get('name'))+' '+str(i.get('completed_sets') or 0)+' 组'+('，每组 '+str(i['reported_reps'])+' 次（你提供）' if i.get('reported_reps') is not None else '，默认每组 '+str(i.get('target_reps'))+' 次' if i.get('target_reps') is not None else '')+('，约 '+str(i.get('reported_duration_min'))+' 分钟' if i.get('reported_duration_min') is not None else '') for i in projected))
                    detail.append('未逐项采集的值按以上默认摘要确认；重量可留空。你可直接修改组数/次数/时长，不修改并确认则按此记录')
                else:detail.append('其余未记录部分'+('标记跳过' if args.get('unfinished_policy')=='skip_remaining' else '保留未记录'))
                if args.get('actual_minutes') is not None:detail.append('实际时长 '+str(args['actual_minutes'])+' 分钟')
            else:
                if args.get('actual_sets') is not None:detail.append('实际总组数改为 '+str(args['actual_sets']))
                if args.get('set_no') is not None:detail.append('修改第 '+str(args['set_no'])+' 组')
                if args.get('reps') is not None:detail.append('实际次数 '+str(args['reps']))
                if args.get('weight_kg') is not None:detail.append('实际重量 '+str(args['weight_kg'])+' kg')
                if args.get('completion_state') is not None:detail.append('完成状态 '+labels[args['completion_state']])
            lines[-1]+='\n确认内容：'+'；'.join(detail)+('。未确认的数据保持未知。' if args.get('completion_basis')=='confirm_current_plan' else '。未提供的实际次数、重量和时长不按计划补齐。')
    prompt = ("请补充以下任务的信息：\n" if any(not t.parameters_resolved for t in tasks) else "请确认以下待执行任务：\n") + "\n".join(f"{i}. {v}" for i, v in enumerate(lines, 1))
    if question:
        prompt += "\n" + question
    return prompt


def make_pending(db, user, thread, snapshot, tasks, question):
    prompt=pending_question(db,user,snapshot,tasks,question)
    p = CoachPendingPlan(user_id=user.id, thread=thread, snapshot_id=snapshot.id,
        tasks=[t.model_dump() for t in tasks], question=prompt, status="pending", expires_at=utcnow() + dt.timedelta(minutes=20))
    checkout_tasks=[t for t in tasks if t.capability_id in {'commerce.submit_order','commerce.cancel_order'}]
    if len(checkout_tasks)==len(tasks) and checkout_tasks:
        ids={t.resolution.selected_id for t in checkout_tasks}
        if len(ids)==1:
            current=next((o for o in snapshot.payload['objects'] if o['id'] in ids and o['type']=='checkout'),None)
            if current and all(t.arguments.get('version')==current['version'] for t in checkout_tasks):
                p.result={'checkout':copy.deepcopy(current)}
    db.add(p)
    db.flush()
    return p


from app.agent.runtime_observation import observe_execution

@observe_execution
def execute_plan(db, user, snapshot, tasks, confirmed=False):
    if snapshot.user_id != user.id:
        raise TaskFailure("ownership_scope", "快照不属于当前用户。")
    from app.llm.cancellation import check_cancelled, begin_execution
    check_cancelled()
    if any(capability(t.capability_id).mutates_state for t in tasks):
        begin_execution()
    lock_user(db, user)
    if digest(business_state(db, user, snapshot.payload["dates"])) != snapshot.revision:
        return {"overall_status": "stale", "reply": "数据已经变化，请重新核对对象和操作范围后再确认。", "task_results": [], "actions": []}
    if any(capability(t.capability_id).execution_boundary == 'initial_plan' for t in tasks):
        from app.agent.initial_plan_runtime import execute
        db.commit()  # No SQLite writer lock during delegated model generation.
        try:
            return execute(db, user, snapshot, tasks, confirmed=confirmed)
        except Exception as exc:
            from app.domain.patches import PatchValidationError
            if isinstance(exc, PatchValidationError):
                state, message = 'clarification_required', '；'.join(map(str, exc.errors))
            elif isinstance(exc, TaskFailure):
                state, message = ('stale' if exc.stage == 'stale' else 'failed'), str(exc)
            else:
                state, message = 'failed', '生成或校验发生异常，原安排未改变，请稍后重试。'
            return {'overall_status': state, 'reply': '首份计划未应用：' + message[:200],
                    'task_results': [{'task_id': t.task_id, 'capability_id': t.capability_id,
                                      'status': state, 'message': message[:200]} for t in tasks], 'actions': []}
    if any(capability(t.capability_id).execution_boundary=='day_revision' for t in tasks):
        from app.agent.day_plan_runtime import execute_revision
        return execute_revision(db,user,snapshot,tasks,confirmed=confirmed)
    if any(t.capability_id.startswith("commerce.") for t in tasks):
        from app.agent.commerce_runtime import execute_mixed
        db.commit()
        return execute_mixed(db,user,snapshot,tasks,confirmed)
    if any(capability(t.capability_id).execution_boundary=='workout_lifecycle' for t in tasks):
        from app.agent.workout_runtime import execute_mixed
        return execute_mixed(db,user,snapshot,tasks,confirmed)
    results = execute_tasks(db, user, snapshot, tasks, confirmed=confirmed)
    n = sum(r["status"] == "success" for r in results)
    status = "success" if n == len(results) else "partial_success" if n else "failed"
    return {"overall_status": status, "task_plan": [t.model_dump() for t in tasks], "task_results": results,
            "reply": "\n".join(r["message"] for r in results),
            "changes": [r["message"] for r in results if r["status"] == "success" and r.get("execution_id")], "actions": []}


def apply_control(db, user, thread, command, *, cancel_checkout=True, resume_context=None):
    lock_user(db, user)
    p = db.get(CoachPendingPlan, command.pending_id)
    if p is None or p.user_id != user.id or p.thread != thread:
        raise TaskFailure("ownership_scope", "待确认任务不存在或不属于当前会话。")
    if p.status in {"completed", "failed"}:
        return {**p.result, "confirmation_replayed": True}
    if ((p.result or {}).get('unresolved_initial_requirements') or (p.result or {}).get('unresolved_edits') or (p.result or {}).get('unresolved_goal_input')) and (command.action == 'confirm' or (command.action == 'revise' and command.execute_revision)):
        response = pending_result(p)
        response['reply'] = '上次要求的修改尚未满足，旧预览没有应用。请继续修改约束或取消草案，不能把旧方案当作新要求确认。'
        return response
    if p.version != command.version:
        raise TaskFailure("version", "待确认任务已经修订，请使用最新版本。")
    if p.status != "pending":
        return {"overall_status": p.status, "reply": "该待确认任务已结束，未执行新的修改。", "actions": []}
    if as_utc(p.expires_at) <= utcnow():
        p.status = "expired"
        if command.action=='confirm' and len(p.tasks)==1 and p.tasks[0].get('capability_id')=='day_plan.confirm_replan':
            from app.agent.day_plan_runtime import request_consent_for_goal
            from app.domain import workout_replan
            saved=p.tasks[0];row=workout_replan.owned(db,user,saved['resolution']['selected_id'])
            snapshot=db.get(CoachBusinessSnapshot,p.snapshot_id)
            renewed=request_consent_for_goal(db,user,snapshot,row.date,row.scope,row.goal_text,
                '上次确认已过期，已重新核对当前安排，请确认这次新的重排选择。')
            p.result=copy.deepcopy(renewed)
            return renewed
        return {"overall_status": "expired", "reply": "确认已过期，请重新发起请求。", "actions": []}
    if command.action in {"cancel", "reject", "new_request"}:
        revision_result=None
        replan_result=None
        # Internal handoff cancels only the pending authorization, preserving
        # the draft being edited. Explicit user cancellation also cancels it.
        if cancel_checkout and command.action in {'cancel','reject'}:
            from app.domain import day_revision as revisions
            for saved in p.tasks:
                if saved.get('capability_id') in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'}:
                    from app.models import PlanDraft
                    row = db.get(PlanDraft, saved['resolution']['selected_id'])
                    if row and row.user_id == user.id and row.status == 'ready':
                        row.status = 'rejected'
                if saved.get('capability_id')=='day_plan.confirm_replan':
                    from app.domain import workout_replan
                    replan_result=workout_replan.cancel(db,user,saved['resolution']['selected_id'],saved['arguments']['expected_revision'])
                if saved.get('capability_id')=='day_plan.apply_revision':
                    row=revisions._owned(db,user,saved['resolution']['selected_id'])
                    if row.status=='awaiting_confirmation' and revisions.etag(row)==saved['arguments']['expected_revision']:
                        revision_result=revisions.cancel(db,user,row.id,expected=revisions.etag(row))
        checkout = None
        if cancel_checkout and db.info.get("commerce_provider"):
            from app.domain import commerce as commerce_service
            for saved_task in p.tasks:
                if saved_task.get("capability_id") == "commerce.submit_order":
                    row = commerce_service.draft_owned(db,user.id,thread,commerce_service.provider(db).account_key,saved_task["resolution"]["selected_id"])
                    if row.status == "awaiting_confirmation" and row.version == saved_task["arguments"]["version"]:
                        row.status = "cancelled"
                        checkout = commerce_service.public_draft(row)
        p.status = {"cancel": "cancelled", "reject": "rejected", "new_request": "cancelled"}[command.action]
        p.version += 1
        return {"overall_status": p.status, "reply": "已取消待执行修改。", "actions": [], **({"checkout":checkout} if checkout else {}),
                **({'day_revision':revision_result} if revision_result else {}),**({'workout_replan':replan_result} if replan_result else {})}
    if command.action == "clarify":
        response = pending_result(p)
        if len(p.tasks) == 1 and p.tasks[0]['capability_id'] == 'commerce.submit_order':
            response['reply'] = '报价预览已保留，尚未提交订单。保留预览无需确认；只有确认创建待支付订单才会提交。'
        else:
            response["reply"] = command.question or p.question
        return response
    snapshot = db.get(CoachBusinessSnapshot, p.snapshot_id)
    if digest(business_state(db, user, snapshot.payload["dates"])) != snapshot.revision:
        p.status = "stale"
        return {"overall_status": "stale", "reply": "确认前数据已变化，原任务未执行。请重新核对目标及范围。", "actions": []}
    tasks = [BoundTask.model_validate(t) for t in p.tasks]
    # A button/boolean cannot manufacture missing tool arguments. Natural
    # answers are materialized by the semantic planner before reaching here.
    if any(not t.parameters_resolved and (command.action=='confirm' or t.task_id in command.parameter_resolutions) for t in tasks):
        response=pending_result(p)
        response['reply']=command.question or p.question
        return response
    if command.action == "revise":
        known = {t.task_id for t in tasks}
        if (not set(command.keep_task_ids) <= known or not set(command.selections) <= set(command.keep_task_ids)
                or not set(command.parameter_resolutions) <= set(command.keep_task_ids)):
            raise TaskFailure("confirmation", "修订包含不在原计划中的任务。")
        tasks = [t for t in tasks if t.task_id in command.keep_task_ids]
    elif command.parameter_resolutions and not set(command.parameter_resolutions) <= {t.task_id for t in tasks if t.parameters_resolved}:
        raise TaskFailure("confirmation", "新增参数澄清需要修订操作。")
    elif command.selections or command.keep_task_ids:
        known = {t.task_id for t in tasks}
        if command.keep_task_ids and set(command.keep_task_ids) != known:
            raise TaskFailure("confirmation", "部分保留需要修订操作。")
        for key, value in command.selections.items():
            t = next((t for t in tasks if t.task_id == key), None)
            if t is None or t.resolution.selected_id != value:
                raise TaskFailure("confirmation", "改选目标需要修订操作。")
    for t in tasks:
        r = t.resolution
        if t.task_id in command.parameter_resolutions:
            t.parameters_resolved = True
        allowed = set(r.candidate_ids + ([r.selected_id] if r.selected_id else []))
        choice = command.selections.get(t.task_id)
        if choice is not None:
            if choice not in allowed:
                raise TaskFailure("confirmation", "选择不在原任务的已保存候选中。")
            r.selected_id, r.candidate_ids, r.status = choice, [choice], "resolved"
        elif not r.selected_id and len(allowed) == 1:
            r.selected_id, r.status = next(iter(allowed)), "candidate"
        if r.status in {"ambiguous", "unresolved"} or not r.selected_id or not t.parameters_resolved:
            if command.action == "revise":
                p.tasks = [task.model_dump() for task in tasks]
                p.version += 1
            response = pending_result(p)
            response["reply"] = command.question or p.question
            return response
    if not tasks:
        p.status = "cancelled"
        return {"overall_status": "cancelled", "reply": "没有保留待执行任务，已取消修改。", "actions": []}
    validate_tasks(tasks, snapshot)
    if command.action == "revise" and not command.execute_revision:
        p.tasks = [t.model_dump() for t in tasks]
        p.version += 1
        p.question = pending_question(db,user,snapshot,tasks,"")
        return pending_result(p)
    p.tasks, p.status, p.version = [t.model_dump() for t in tasks], "executing", p.version + 1
    result = execute_plan(db, user, snapshot, tasks, confirmed=True)
    read_completed=False
    if resume_context is not None:
        from app.agent.day_plan_runtime import resume_replan
        replanned=resume_replan(db,user,snapshot,result,resume_context['client'],resume_context['day'],resume_context['trace'])
        if replanned is not None:
            result=replanned;read_completed=True
        from app.agent.commerce_runtime import resume_read_pending
        continued=resume_read_pending(db,user,p,snapshot,tasks,result,**resume_context)
        if continued is not None:
            result=continued
            read_completed=True
    p.status = "completed" if read_completed or result["overall_status"] in {"success", "partial_success"} else "failed"
    gaps = (p.result or {}).get("unfulfilled_goals", [])
    if gaps and not read_completed:
        result["unfulfilled_goals"] = gaps
        result["reply"] += "\n尚未完成：" + "；".join(gaps)
    p.result = copy.deepcopy(result)
    result.setdefault("pending_confirmation", pending_payload(p))
    return result


def run(db, user, text, *, page="today", day=None, session_id=None, page_context=None,
        thread="coach", client=None, confirmation=None, checkout_action=None, commerce_action=None):
    from app.agent.coach_agent import normalize_thread, recent_messages
    started, trace = time.monotonic(), []
    db.info["runtime_trace"] = trace
    try:
        db.info["runtime_confirmation"] = confirmation.model_dump(mode='json') if hasattr(confirmation,'model_dump') else copy.deepcopy(confirmation)
    except Exception:
        db.info["runtime_confirmation"] = None
    day = day or user_today(db.get(Profile, user.id))

    thread = normalize_thread(thread)
    db.info["coach_thread"] = thread
    db.info['coach_user_text']=text
    db.info['semantic_client'] = client
    db.info.pop('initial_edit_pending_id', None)
    db.info.pop('revision_edit_pending_id', None)
    result = {"intent": "semantic_binding", "intent_label": "教练任务", "scope": "bound_objects",
              "actions": [], "changes": [], "memory_saved": [], "session": None, "plan_draft": None,
              "patch_request": None, "today_brief": None, "memories": None, "guidance": [], "warnings": []}
    snapshot = None
    carried_tasks, prior_result = [], None
    handoff = False
    deferred_handoff = None
    deferred_commerce_authorization = False
    editing_context=[]
    completing_tasks=[]
    workflow_day_context=False
    resume_context={"client":client,"day":day,"trace":trace,"confirmation_text":text}
    try:
        if commerce_action is not None:
            from app.agent.commerce_runtime import catalog_action
            result.update(catalog_action(db,user,thread,day,commerce_action))
        elif checkout_action is not None:
            from app.agent.commerce_runtime import structured_action
            result.update(structured_action(db,user,thread,day,checkout_action))
        elif confirmation is not None:
            command = PendingControl.model_validate(confirmation) if isinstance(confirmation, dict) else confirmation
            result.update(apply_control(db, user, thread, command,resume_context=resume_context))
        # 普通自然语言
        else:
            p = db.scalar(select(CoachPendingPlan).where(CoachPendingPlan.user_id == user.id,
                CoachPendingPlan.thread == thread, CoachPendingPlan.status == "pending").order_by(CoachPendingPlan.created_at.desc()))
            if p:
                workflow_day_context=any(t['capability_id'].startswith('day_plan.') for t in p.tasks)
                if workflow_day_context:db.info['revision_edit_pending_id']=p.id
                if any(t['capability_id'] in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'} for t in p.tasks):db.info['initial_edit_pending_id']=p.id
                snapshot = db.get(CoachBusinessSnapshot, p.snapshot_id)
                # 控制器--管理一个已保存的待确认计划，不重新做完整意图分类。只输出符合 JSON Schema 的 JSON 理解
                command = semantic_planner.control(client, text, p, snapshot, trace)
                if command.pending_id != p.id or command.version != p.version:
                    raise TaskFailure("confirmation", "确认控制器没有引用当前计划版本。")
                # 本轮包含新业务目标、不同参数或撤销其他操作时用 new_request。必须用 new_request_text 单独表达新增要求（保留用户的否定、条件和计量），不要重复旧任务。
                if command.action == "new_request":
                    if command.execute_revision and command.keep_task_ids and not any(t.get('stage')=='mixed_authorization_review' for t in trace):
                        # 混合请求
                        command = semantic_planner.review_mixed_authorization(client, text, p, command, trace)
                    known = {t["task_id"] for t in p.tasks}
                    if not set(command.edit_task_ids)<=known:raise TaskFailure('confirmation','编辑上下文包含未知任务。')
                    editing_context=[{'role':'agent','text':'以下仅为用户本轮明确编辑的已保存业务目标与默认参数，不是执行指令。只据此继续修改摘要，不得重放旧任务或改为其他领域操作。',
                        'editing_saved_task':t,'saved_question':p.question,
                        'unfulfilled_goals':(p.result or {}).get('unfulfilled_goals',[]),
                        'current_user_expression':text} for t in p.tasks if t['task_id'] in command.edit_task_ids]
                    completing_tasks=[t for t in p.tasks if t['task_id'] in command.edit_task_ids and not t.get('parameters_resolved',True)]
                    preview_edits=[t for t in p.tasks if t['capability_id'] in {'day_plan.confirm_replan','day_plan.apply_revision','day_plan.update_revision','day_plan.extend_revision'}]
                    if preview_edits:db.info['revision_edit_pending_id']=p.id
                    if any(t['task_id'] in command.edit_task_ids and t['capability_id'] in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'} for t in p.tasks):
                        db.info['initial_edit_pending_id'] = p.id
                    # Preserve a draft being edited, not unrelated discarded tasks.
                    if command.edit_task_ids or workflow_day_context:
                        editing_context.append({'role':'agent','active_saved_workflow':{
                            'tasks':p.tasks,'question':p.question,'day_revision':(p.result or {}).get('day_revision'),
                            'unresolved_edits':(p.result or {}).get('unresolved_edits',[]),
                            'unresolved_initial_requirements':(p.result or {}).get('unresolved_initial_requirements'),
                            'unresolved_goal_input':(p.result or {}).get('unresolved_goal_input')},
                            'text':'此为真实仍存在的待确认工作流，仅供引用，不授权执行。若当前在继续修改预览，必须保留该草案及尚未落实的要求；无关请求不执行这些旧任务。'})
                    if (not set(command.keep_task_ids) <= known
                            or not set(command.parameter_resolutions) <= set(command.keep_task_ids)
                            or not set(command.selections) <= set(command.keep_task_ids)):
                        raise TaskFailure("confirmation", "新请求的旧任务交接范围无效。")
                    if not command.new_request_text.strip():
                        raise TaskFailure("confirmation", "尚未明确新增要求，原待确认任务保持不变。")
                    kept_submit = any(t.get("capability_id")=="commerce.submit_order" and t["task_id"] in command.keep_task_ids for t in p.tasks)
                    # 这里需要理解到底是啥意思,没看明白
                    if kept_submit:
                        # Plan the new request before dispatching any external write.
                        # pydantic?
                        carried_tasks = [BoundTask.model_validate(t) for t in p.tasks if t["task_id"] in command.keep_task_ids]
                        deferred_handoff = command
                        deferred_commerce_authorization = command.execute_revision
                    elif command.keep_task_ids and command.execute_revision:
                        # ?
                        controlled = apply_control(db, user, thread, command.model_copy(update={"action": "revise"}))
                        if controlled["overall_status"] != "success":
                            result.update(controlled)
                            return finish(db, user, text, page, day, thread, result, trace, started, snapshot)
                        prior_result = controlled
                    else:
                        carried_tasks = [BoundTask.model_validate(t) for t in p.tasks if t["task_id"] in command.keep_task_ids]
                        if carried_tasks:
                            deferred_handoff = command
                        else:
                            # Keep the recoverable clarification until a replacement
                            # goal/preview is actually saved. Model failure loses nothing.
                            deferred_handoff = command
                    planning_text = command.new_request_text
                    handoff = True
                    trace.append({"stage": "pending_handoff", "status": "ok", "retained_task_ids": command.keep_task_ids,
                                  "discarded_task_ids": sorted(known - set(command.keep_task_ids)), "new_request_text": planning_text})
                else:
                    controlled = apply_control(db, user, thread, command,resume_context=resume_context)
                if command.action != "new_request":
                    result.update(controlled)
                    return finish(db, user, text, page, day, thread, result, trace, started, snapshot)
            snapshot = build_snapshot(db, user, thread, [day.isoformat()],day_revision=workflow_day_context)
            db.commit()  # Never hold a writer lock during a model network request.
            # Old unexecuted requests cannot become implicit instructions in the new plan.
            recent = editing_context if handoff else recent_messages(db, user, thread, limit=8)
            if not handoff:
                planning_text = text
            #     为啥这里会range(3)
            for attempt in range(3):
                # plan是什么规划?
                parsed = semantic_planner.plan(client, planning_text, snapshot, recent, day, user_today(db.get(Profile, user.id)), trace,source_text=text)
                if parsed.snapshot_id != snapshot.id:
                    raise TaskFailure("snapshot", "模型引用了错误的快照。")
                if parsed.mode != "load_context":
                    break
                request = parsed.context_request
                if request is None or attempt == 2:
                    raise TaskFailure("context", "上下文请求不完整或超过加载上限。")
                dates = list(dict.fromkeys(snapshot.payload["dates"] + request.dates))
                if len(dates) > 3:
                    raise TaskFailure("scope", "一次最多处理三个日期，请缩小范围。")
                for date in dates:
                    if abs((dt.date.fromisoformat(date) - day).days) > 366:
                        raise TaskFailure("scope", "目标日期超出允许范围。")
                if request.commerce:
                    from app.domain.commerce import load_context, provider
                    load_context(db,user.id,thread,provider(db))
                # 什么的快照
                snapshot = build_snapshot(db, user, thread, dates,
                    exercise_catalog=snapshot.payload["exercise_catalog"] or request.exercise_catalog,
                    history_limit=request.history_limit,
                    day_revision=snapshot.payload.get('day_revision',False) or request.day_revision)
                db.commit()
            # handoff是交接啥东西啊
            if completing_tasks and parsed.mode=='execute' and not (all(old['capability_id']=='memory.create' for old in completing_tasks) and all(t.capability_id=='memory.archive' for t in parsed.tasks)):
                allowed={(t['capability_id'],t['resolution']['selected_id']) for t in completing_tasks}
                if any((t.capability_id,t.resolution.selected_id) not in allowed and (t.capability_id,None) not in allowed for t in parsed.tasks):
                    raise TaskFailure('parameter_completion','补充信息只能完善原目标，不能改成应用草案或其他操作。')
            if handoff and preview_edits and parsed.mode=='execute' and any(capability(t.capability_id).mutates_state and t.capability_id.startswith(('meal.', 'workout.')) and capability(t.capability_id).execution_boundary != 'workout_lifecycle' for t in parsed.tasks):
                raise TaskFailure('draft_edit_boundary','本轮只编辑已保存草案，不能转成对实际计划的局部写入。')
            if handoff:
                identities = {t.task_id: new_id() for t in parsed.tasks}
                for t in parsed.tasks:
                    t.task_id = identities[t.task_id]
                    t.dependencies = [identities[key] for key in t.dependencies]
            for carried in carried_tasks:
                if carried.task_id in command.parameter_resolutions:
                    carried.parameters_resolved = True
                choice = command.selections.get(carried.task_id)
                if choice:
                    allowed = set(carried.resolution.candidate_ids + ([carried.resolution.selected_id] if carried.resolution.selected_id else []))
                    if choice not in allowed:
                        raise TaskFailure("confirmation", "交接选择不在已保存候选内。")
                    carried.resolution.selected_id = choice
                    carried.resolution.candidate_ids = [choice]
                    carried.resolution.status = "resolved"
            # A proposed concrete local mutation is a saved preview, not disposable prose.
            local_preview = parsed.mode == 'propose' and any(capability(t.capability_id).mutates_state and capability(t.capability_id).execution_boundary == 'local_database' for t in parsed.tasks)
            new_tasks = parsed.tasks if parsed.mode == "execute" or local_preview else []
            edited_checkouts = {t.resolution.selected_id for t in new_tasks if t.capability_id in {"commerce.revise_order", "commerce.cancel_checkout"}}
            preparing_quote = any(t.capability_id=="commerce.prepare_order" for t in new_tasks)
            invalidated = [t.task_id for t in carried_tasks if t.capability_id=="commerce.submit_order" and
                           (t.resolution.selected_id in edited_checkouts or preparing_quote)]
            if invalidated:
                carried_tasks = [t for t in carried_tasks if t.task_id not in invalidated]
                trace.append({"stage":"checkout_authorization", "status":"invalidated_before_dispatch", "task_ids":invalidated})
            if not deferred_commerce_authorization:
                carried_tasks = [t for t in carried_tasks if not capability(t.capability_id).mutates_state]
            tasks = carried_tasks + new_tasks
            objects = validate_tasks(tasks, snapshot)
            if deferred_commerce_authorization and carried_tasks:
                retained={t.task_id for t in carried_tasks}
                safe_command=deferred_handoff.model_copy(update={"action":"revise", "keep_task_ids":list(retained),
                    "parameter_resolutions":[i for i in deferred_handoff.parameter_resolutions if i in retained],
                    "selections":{i:v for i,v in deferred_handoff.selections.items() if i in retained}})
                controlled=apply_control(db,user,thread,safe_command)
                if controlled["overall_status"]!="success":
                    result.update(controlled)
                    return finish(db,user,text,page,day,thread,result,trace,started,snapshot)
                prior_result=controlled
                carried_tasks=[]
                tasks=new_tasks
                objects=validate_tasks(tasks,snapshot)
                deferred_handoff=None
            result["task_plan"] = [t.model_dump() for t in tasks]
            if carried_tasks and parsed.mode in {"propose", "noop"}:
                result.update(pending_result(make_pending(db, user, thread, snapshot, carried_tasks, parsed.answer or parsed.question)))
            elif local_preview:
                result.update(pending_result(make_pending(db,user,thread,snapshot,tasks,parsed.question)))
            elif parsed.mode in {"propose", "noop"} or not tasks:
                result.update(overall_status="proposed" if parsed.mode == "propose" else "noop", execution_mode=parsed.mode,
                              reply=parsed.answer or parsed.question or "请补充希望执行的目标。")
            elif any(requires_confirmation(t, objects) or t.resolution.status != "resolved" for t in tasks):
                result.update(pending_result(make_pending(db, user, thread, snapshot, tasks, parsed.question)))
            else:
                result.update(execute_plan(db, user, snapshot, tasks))
            if deferred_handoff and result.get('overall_status') in {'success','partial_success','confirmation_required'}:
                apply_control(db,user,thread,deferred_handoff.model_copy(update={'action':'cancel'}),cancel_checkout=False)
            continued_tools = False
            from app.agent.commerce_runtime import continue_plan, can_advance, save_read_continuation
            if parsed.continue_after_tools and can_advance(tasks,result) and result.get("overall_status")=="success":
                result.update(continue_plan(db,user,planning_text,snapshot,recent,day,client,trace,result))
                continued_tools = True
            save_read_continuation(db,result,tasks,planning_text,parsed.continue_after_tools or bool(parsed.unfulfilled_goals))
            if parsed.unfulfilled_goals and not continued_tools:
                from app.agent.reply_evidence import remaining_commerce_goals
                parsed.unfulfilled_goals = remaining_commerce_goals(client,parsed.unfulfilled_goals,result,trace)
            if parsed.unfulfilled_goals and not continued_tools:
                result["unfulfilled_goals"] = parsed.unfulfilled_goals
                result["reply"] += "\n尚未完成：" + "；".join(parsed.unfulfilled_goals)
                pending_id = (result.get("pending_confirmation") or {}).get("pending_id")
                if pending_id:
                    saved = db.get(CoachPendingPlan, pending_id)
                if pending_id and saved.tasks == [t.model_dump() for t in tasks]:
                    saved.result = {**(saved.result or {}),"unfulfilled_goals": parsed.unfulfilled_goals}
                    saved.question = result["reply"]
            # 这又是啥
            if prior_result:
                for key in ("checkout", "order"):
                    if prior_result.get(key) and not result.get(key):result[key]=prior_result[key]
                result["task_results"] = prior_result.get("task_results", []) + result.get("task_results", [])
                result["changes"] = prior_result.get("changes", []) + result.get("changes", [])
                result["reply"] = prior_result["reply"] + "\n" + result["reply"]
    except Exception as exc:
        from app.domain.commerce import CommerceCredentialError
        from app.food.mcd_transport import McpAuthenticationError
        if isinstance(exc,(CommerceCredentialError,McpAuthenticationError)):
            result["commerce_connection"]={"status":"token_required","guide_url":"https://open.mcd.cn/mcp"}
        if prior_result:
            result.update(prior_result)
            result["reply"] += "\n新增要求尚未执行：" + safe_error(exc)[:300]
            result["overall_status"] = "partial_success"
        else:
            result.update(overall_status="clarification_required", error=getattr(exc, "stage", type(exc).__name__),
                      reply="未能可靠执行：" + safe_error(exc)[:300], actions=[])
        trace.append({"stage": "validation_or_model", "status": "failed", "error_type": type(exc).__name__})
    return finish(db, user, text, page, day, thread, result, trace, started, snapshot)

# 保存并记录结果?
def finish(db, user, text, page, day, thread, result, trace, started, snapshot):
    from app.agent.coach_agent import _record_messages
    from app.domain.day_plan import effective_day
    initial_edit_id = db.info.pop('initial_edit_pending_id', None)
    if initial_edit_id and result.get('overall_status') in {'failed','clarification_required','stale'}:
        saved = db.get(CoachPendingPlan, initial_edit_id)
        understood = next((e['parsed'] for e in reversed(trace) if e.get('stage')=='initial_requirements_merged' and e.get('status')=='ok'), None)
        if saved and saved.status=='pending':
            saved.result = {**(saved.result or {}), 'unresolved_goal_input': text, **({'unresolved_initial_requirements':understood} if understood else {})}
            saved.version += 1
    editing_id=db.info.pop('revision_edit_pending_id',None)
    if editing_id and result.get('overall_status') in {'failed','clarification_required','stale'}:
        saved=db.get(CoachPendingPlan,editing_id)
        if saved and saved.status=='pending':
            history=(saved.result or {}).get('unresolved_edits',[])
            understood=next((t.get('parsed') for t in reversed(trace) if t.get('stage') in {'revision_requirements','revision_requirements_merged'} and t.get('status')=='ok'),None)
            saved.result={**(saved.result or {}),'unresolved_goal_input':text,'unresolved_edits':[*history,{'user_text':text,'status':'not_materialized','requirements':understood}][-6:]}
            saved.version += 1  # Invalidate every button issued before this failed goal revision.
    from app.agent.reply_evidence import public_reply
    result["reply"] = public_reply(result.get("reply", ""), snapshot)
    result["latency_ms"], result["trace"] = round((time.monotonic() - started) * 1000), trace
    result["day"] = effective_day(db, user, day, fallback_next=False)
    result["context_used"] = {"snapshot_id": snapshot.id, "revision": snapshot.revision} if snapshot else {}
    row = AgentRun(user_id=user.id, kind="coach_semantic_binding", mode="agent", status=result["overall_status"],
        input={"text": text, "page": page, "date": str(day), "thread": thread, "context": result["context_used"], "confirmation": db.info.get("runtime_confirmation")},
        output=copy.deepcopy(result), trace=trace, latency_ms=result["latency_ms"], finished_at=utcnow())
    db.add(row)
    db.flush()
    result["run_id"] = row.id
    _record_messages(db, user, text, result, thread=thread)
    # Commit the exact result with its messages and local effects. A process exit
    # after this transaction cannot strand a completed request as "running".
    claim_id = db.info.get('coach_claim_id')
    if claim_id:
        claim = db.get(AgentRun, claim_id)
        if claim and claim.user_id == user.id and claim.kind == 'coach_request':
            claim.output = copy.deepcopy(result)
            claim.status = 'succeeded'
            claim.finished_at = utcnow()
    db.commit()
    return result
