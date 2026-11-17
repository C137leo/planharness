"""First-plan lifecycle using the existing validated planning service, never a replan."""
import datetime as dt
from pydantic import Field
from sqlalchemy import select
from app.agent.binding_contract import StrictModel, BoundTask
from app.agent.capabilities import Capability
from app.agent.business_snapshot import digest, business_state, build_snapshot
from app.agent.task_executor import validate_tasks, TaskFailure, resource_changes
from app.models import Plan, PlanDraft, CoachTaskExecution, new_id
from app.domain.coach import initial_profile_revision as profile_revision
from app.agent.initial_requirements import InitialRequirements


class CreateInitial(StrictModel):
    instruction: str = Field(min_length=1, max_length=4000)
    goal_contract: InitialRequirements | None = None


class InitialVersion(StrictModel):
    expected_revision: str = Field(min_length=64, max_length=64)


class ReviseInitial(CreateInitial, InitialVersion):
    pass


CAPABILITIES = {c.capability_id: c for c in [
    Capability('plan.create_initial_draft', 'initial_plan', '没有生效计划时，根据已保存档案生成首份长期训练和饮食草案。只预览，不改档案、不启用。instruction完整保留用户要求。', CreateInitial, ('profile',), True, 'never', 'initial_plan', execution_boundary='initial_plan'),
    Capability('plan.revise_initial_draft', 'initial_plan', '修改已展示的首份计划草案，expected_revision复制etag；instruction只写本轮修改，保留原约束和其他选择，重新预览不启用。', ReviseInitial, ('initial_plan_draft',), True, 'never', 'initial_plan', execution_boundary='initial_plan'),
    Capability('plan.confirm_initial_draft', 'initial_plan', '仅在用户明确确认已经展示的首份计划后启用，expected_revision复制草案etag。', InitialVersion, ('initial_plan_draft',), True, 'always', 'initial_plan', execution_boundary='initial_plan'),
    Capability('plan.cancel_initial_draft', 'initial_plan', '取消尚未启用的首份计划草案，不更改档案或记录。', InitialVersion, ('initial_plan_draft',), True, 'never', 'initial_plan', execution_boundary='initial_plan'),
]}

# The same domain draft service supports replacing an existing long-term plan.
CAPABILITIES.update({c.capability_id:c for c in [
    Capability('plan.create_replacement_draft','initial_plan','根据已保存档案重新生成完整长期训练与饮食计划。绑定profile，只预览；保留训练和进食事实，确认后新版本才生效，不是只改今天。',CreateInitial,('profile',),True,'never','initial_plan',execution_boundary='initial_plan'),
    Capability('plan.revise_replacement_draft','initial_plan','修改未应用的长期替换草案，保留其他目标，expected_revision使用etag。',ReviseInitial,('replacement_plan_draft',),True,'never','initial_plan',execution_boundary='initial_plan'),
    Capability('plan.confirm_replacement_draft','initial_plan','确认已展示的长期替换草案，创建新版本；原有事实保留。',InitialVersion,('replacement_plan_draft',),True,'always','initial_plan',execution_boundary='initial_plan'),
    Capability('plan.cancel_replacement_draft','initial_plan','取消未应用的长期替换草案，原计划保持。',InitialVersion,('replacement_plan_draft',),True,'never','initial_plan',execution_boundary='initial_plan'),
]})


def draft_revision(row):
    return digest({'id': row.id, 'status': row.status, 'payload': row.payload})


def execute(db, user, snapshot, tasks, *, confirmed=False):
    from app.domain import coach
    from app.agent.semantic_runtime import make_pending, pending_result
    objects = validate_tasks(tasks, snapshot)
    if len(tasks) != 1:
        raise TaskFailure('execution_boundary', '首份计划每轮只处理一个草案阶段。')
    task = tasks[0]
    replacing = '_replacement_draft' in task.capability_id
    current_plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == 'active'))
    if bool(current_plan) != replacing:
        raise TaskFailure('stale', '已有生效计划，请重新核对安排。')
    with db.begin_nested():
        before = business_state(db, user, snapshot.payload['dates'])
        if task.capability_id in {'plan.create_initial_draft', 'plan.revise_initial_draft','plan.create_replacement_draft','plan.revise_replacement_draft'}:
            instruction = task.arguments['instruction']
            parent_id = None
            if task.capability_id in {'plan.revise_initial_draft','plan.revise_replacement_draft'}:
                parent = coach.load_draft(db, user, task.resolution.selected_id)
                if bool(parent.plan_id) != replacing or parent.status != 'ready' or draft_revision(parent) != task.arguments['expected_revision']:
                    raise TaskFailure('stale', '要修改的首份计划预览已变化。')
                instruction = parent.instruction + '\n本轮修改（其他约束保留）：' + instruction
                parent_id = parent.id
            if (task.arguments.get('goal_contract') or {}).get('meal_substitutions') and not parent_id:
                raise TaskFailure('stale', '请选择当前待确认草案后替换具体餐食。')
            basis = profile_revision(db, user)
            row = coach.create_draft(db, user, instruction, parent_draft_id=parent_id,
                client=db.info.get('semantic_client'), interpret_instruction=False, commit=False,
                goal_contract=CreateInitial.model_validate({k:v for k,v in task.arguments.items() if k != 'expected_revision'}).goal_contract)
            row.payload = {**row.payload, 'meta': {**row.payload.get('meta', {}),
                            ('replacement_profile_revision' if replacing else 'initial_profile_revision'): basis}}
            message = '首份计划草案已生成，尚未启用。请核对训练和饮食安排后确认。' if row.status == 'ready' else row.question or '生成还需要补充资料。'
        else:
            row = coach.load_draft(db, user, task.resolution.selected_id)
            if bool(row.plan_id) != replacing or row.status != 'ready' or draft_revision(row) != task.arguments['expected_revision']:
                raise TaskFailure('stale', '首份计划预览已变化，请重新核对。')
            if task.capability_id in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'}:
                if not confirmed:
                    raise TaskFailure('confirmation', '请先确认已展示的计划草案。')
                plan, version = coach.confirm_draft(db, user, row.id, commit=False)
                message = f'首份训练与饮食计划已启用（第{version.version}版）。'
            else:
                row.status = 'rejected'
                message = '已取消首份计划草案，未启用任何安排。'
        db.flush()
        db.expire_all()
        after = business_state(db, user, snapshot.payload['dates'])
        if task.capability_id not in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'} and before != after:
            raise TaskFailure('verification', '预览意外改变业务数据，已回滚。')
        for key in ('profile', 'memories', 'facts'):
            if before[key] != after[key]:
                raise TaskFailure('verification', '计划操作改变了档案或记录，已回滚。')
        if any(sid not in after['sessions'] or value['items']!=after['sessions'][sid]['items'] for sid,value in before['sessions'].items()):
            raise TaskFailure('verification','长期计划不能删除或改写已有训练事实。')
        row = db.get(PlanDraft, row.id)
        journal = CoachTaskExecution(user_id=user.id, thread=snapshot.thread,
            request_key=db.info.get('coach_request_key') or new_id(), task_id=task.task_id,
            effective_date=dt.date.fromisoformat(snapshot.payload['dates'][0]), operation=task.capability_id,
            domain='initial_plan', scope='first_plan', goal=task.user_goal, tool='initial_plan',
            arguments=task.arguments, resource_changes=resource_changes(before, after), status='verified')
        db.add(journal)
        db.flush()
        complete = row.status in {'ready', 'confirmed', 'rejected'}
        if not complete:
            retained = db.scalar(select(PlanDraft).where(PlanDraft.user_id == user.id, PlanDraft.plan_id.is_(None), PlanDraft.status == 'ready'))
            if retained: message += '上一份可用预览已保留，尚未启用。'
        if replacing: message = message.replace('首份','长期替换').replace('尚未启用','尚未应用')
        result = {'overall_status': 'success' if complete else 'clarification_required', 'reply': message, 'plan_draft': coach.draft_payload(row),
            'task_results': [{'task_id': task.task_id, 'capability_id': task.capability_id, 'status': 'success' if complete else 'clarification_required',
                'message': message, 'execution_id': journal.id}], 'actions': []}
        if row.status == 'ready':
            fresh = build_snapshot(db, user, snapshot.thread, snapshot.payload['dates'])
            apply = BoundTask(task_id=new_id(), capability_id=('plan.confirm_replacement_draft' if replacing else 'plan.confirm_initial_draft'),
                user_goal='启用已展示的首份训练与饮食计划',
                resolution={'status': 'resolved', 'selected_id': row.id, 'basis': 'explicit'},
                arguments={'expected_revision': draft_revision(row)}, needs_confirmation=True)
            pending = make_pending(db, user, snapshot.thread, fresh, [apply], '确认后才启用；也可以继续修改或取消。')
            pending.result = {'plan_draft': result['plan_draft']}
            display = pending_result(pending)
            result.update(overall_status=display['overall_status'], pending_confirmation=display['pending_confirmation'], actions=display['actions'])
        return result


def refreshed_allergy_preview(db,user,snapshot):
    """Reissue only this thread's already-live preview, after safety repair."""
    from app.models import CoachPendingPlan
    ids=set(db.info.pop('allergy_preview_ids',[]))
    if not ids:return {}
    previous=db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,
        CoachPendingPlan.thread==snapshot.thread,CoachPendingPlan.status=='stale').order_by(CoachPendingPlan.created_at.desc())).all()
    selected=next((t['resolution']['selected_id'] for p in previous for t in p.tasks
        if t['capability_id'] in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'} and t['resolution'].get('selected_id') in ids),None)
    if not selected:return {}
    row=db.get(PlanDraft,selected)
    if row.status!='ready':return {}
    from app.agent.semantic_runtime import make_pending,pending_result
    from app.domain.coach import draft_payload
    fresh=build_snapshot(db,user,snapshot.thread,snapshot.payload['dates'])
    task=BoundTask(task_id=new_id(),capability_id='plan.confirm_replacement_draft' if row.plan_id else 'plan.confirm_initial_draft',
        user_goal='确认启用已按长期过敏限制修复的计划预览',
        resolution={'status':'resolved','selected_id':row.id,'basis':'explicit'},
        arguments={'expected_revision':draft_revision(row)},needs_confirmation=True)
    pending=make_pending(db,user,snapshot.thread,fresh,[task],'过敏食材已排除。草案尚未启用，请核对新版预览。')
    pending.result={'plan_draft':draft_payload(row)}
    value=pending_result(pending)
    return {'plan_draft':draft_payload(row),'pending_confirmation':value['pending_confirmation'],'actions':value['actions']}


def reconcile_pending_previews(db,user,thread):
    """Retire legacy authorizations whose older ready draft survived a revision."""
    from app.models import CoachPendingPlan
    for pending in db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,
            CoachPendingPlan.thread==thread,CoachPendingPlan.status=='pending')):
        for task in pending.tasks:
            if task['capability_id'] not in {'plan.confirm_initial_draft','plan.confirm_replacement_draft'}:continue
            row=db.get(PlanDraft,task['resolution'].get('selected_id'))
            latest=db.scalar(select(PlanDraft).where(PlanDraft.user_id==user.id,PlanDraft.plan_id==row.plan_id,
                PlanDraft.status.in_(['ready','confirmed','rejected','superseded'])).order_by(PlanDraft.created_at.desc())) if row and row.user_id==user.id else None
            if not row or row.status!='ready' or not latest or latest.id!=row.id:
                pending.status='superseded';pending.version+=1
                break
    db.flush()
