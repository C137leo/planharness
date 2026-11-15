"""Day revisions: isolated domain execution, saved confirmation, truthful results."""
import copy
import datetime as dt
import json
from app.agent.binding_contract import BoundTask, Resolution
from app.agent.business_snapshot import build_snapshot, business_state
from app.agent.capabilities import capability
from app.agent.task_executor import validate_tasks, requires_confirmation, resource_changes, TaskFailure
from app.domain import day_revision as service
from app.domain.day_plan import _estimated_minutes
from app.models import CoachTaskExecution, DayPlanRevision, new_id


def describe(draft):
    labels={'awaiting_confirmation':'草案已生成，尚未应用','applied':'草案已应用并重读验证','cancelled':'草案已取消','expired':'草案已过期','conflicted':'数据已变化，草案需重新生成'}
    lines=[labels.get(draft['status'],draft['status'])+'。']
    actions=draft['target_plan']['actions']
    if draft.get('workout_policy')=='replace_remaining':
        lines.append('新目标进度从零开始；重排前已有组数单独保留，不算作新目标已完成。')
    lines.append('目标训练约 '+str(sum(_estimated_minutes(a) for a in actions))+' 分钟：'+
                 '；'.join(str(a.get('name') or a['action_ref'])+' '+str(a.get('sets') or 1)+'组' for a in actions))
    for meal in draft['target_plan']['meals']:
        if meal['slot'] in draft['scope']:
            lines.append(meal['title']+'：'+'、'.join(i['name']+' '+str(i['grams'])+'g' for i in meal['items']))
    if draft['status']=='awaiting_confirmation':lines.append('请查看差异；确认后才应用。你可以继续修改或取消。')
    return '\n'.join(lines)


def execute_revision(db,user,snapshot,tasks,confirmed=False):
    objects=validate_tasks(tasks,snapshot)
    if len(tasks)!=1 or capability(tasks[0].capability_id).execution_boundary!='day_revision':
        raise TaskFailure('execution_boundary','整日草案每轮只执行一个生命周期任务；其它修改请单独规划。')
    task=tasks[0];cap=capability(task.capability_id);target=objects[task.resolution.selected_id]
    if task.capability_id in {'day_plan.request_replan','day_plan.confirm_replan'}:
        return execute_replan(db,user,snapshot,task,target,confirmed)
    base={'task_id':task.task_id,'capability_id':task.capability_id,'user_goal':task.user_goal,
          'tool':'day_revision','tool_arguments':{'target_id':target['id'],**task.arguments}}
    data=None
    try:
        if not task.parameters_resolved:raise ValueError('草案参数尚未明确。')
        if requires_confirmation(task,objects) and not confirmed:raise ValueError('必须先确认保存的草案版本。')
        args=cap.argument_model.model_validate(task.arguments)
        if task.capability_id=='day_plan.create_revision' and args.workout_policy=='replace_remaining':
            recovered=recover_replan_consent(db,user,snapshot,task,target,args)
            if recovered is not None:return recovered
        with db.begin_nested():
            before=business_state(db,user,snapshot.payload['dates'])
            if task.capability_id=='day_plan.create_revision':
                data=service.create(db,user,dt.date.fromisoformat(target['date']),scope=args.scope,
                    proposal=args.proposal.model_dump(exclude_unset=True),constraints=args.constraints.model_dump(),
                    expected_base_revision=args.expected_base_revision,workout_policy=args.workout_policy,
                    replan_authorization_id=args.replan_authorization_id)
            elif task.capability_id=='day_plan.preview_revision':data=service.preview(db,user,target['id'])
            elif task.capability_id=='day_plan.update_revision':
                data=service.update_draft(db,user,target['id'],expected=args.expected_revision,
                    proposal=args.proposal.model_dump(exclude_unset=True),constraints=args.constraints.model_dump(exclude_unset=True) if args.constraints is not None else None)
            elif task.capability_id=='day_plan.extend_revision':
                data=service.extend_draft(db,user,target['id'],expected=args.expected_revision,additional_scope=args.additional_scope,
                    proposal=args.proposal.model_dump(exclude_unset=True),constraints=args.constraints.model_dump(exclude_unset=True) if args.constraints is not None else None)
            elif task.capability_id=='day_plan.cancel_revision':data=service.cancel(db,user,target['id'],expected=args.expected_revision)
            else:data=service.apply(db,user,target['id'],confirmed_etag=args.expected_revision)
            db.flush();db.expire_all()
            after=business_state(db,user,snapshot.payload['dates'])
            if task.capability_id!='day_plan.apply_revision' and before!=after:
                raise ValueError('草案操作意外修改当天业务数据，已回滚。')
            protected=copy.deepcopy(before)
            if task.capability_id=='day_plan.apply_revision':
                protected['days'][target['date']]=copy.deepcopy(after['days'][target['date']])
                for sid,value in after['sessions'].items():
                    if value['date']==target['date']:protected['sessions'][sid]=copy.deepcopy(value)
                date = target['date']
                # Replacement archives real sets from superseded sessions. The
                # domain service verifies those derived logs against the old set
                # facts; hand-entered logs and every other fact stay immutable.
                if data.get('workout_policy') == 'replace_remaining':
                    archived = {sid for sid, value in after['sessions'].items()
                                if value['date'] == date and value['status'] == 'superseded'}
                    old_logs = before['facts'][date]['workout_logs']
                    new_logs = after['facts'][date]['workout_logs']
                    if [r for r in old_logs if r['session_id'] not in archived] != [r for r in new_logs if r['session_id'] not in archived]:
                        raise ValueError('重排改变了其他训练或手工运动记录，已回滚。')
                    protected['facts'][date]['workout_logs'] = copy.deepcopy(new_logs)
                if after!=protected:raise ValueError('草案提交修改了日期范围以外的资源，已回滚。')
            changes=resource_changes(before,after)
            journal=CoachTaskExecution(user_id=user.id,thread=snapshot.thread,
                request_key=db.info.get('coach_request_key') or new_id(),task_id=task.task_id,
                effective_date=dt.date.fromisoformat(target['date']),operation=task.capability_id,domain='day_revision',
                scope=','.join(data['scope']),goal=task.user_goal,tool='day_revision',arguments=base['tool_arguments'],
                resource_changes=changes,status='verified')
            db.add(journal);db.flush()
            one={**base,'status':'success','message':describe(data),'data':data,'execution_id':journal.id,
                'actual_changes':changes,'unrelated_changes':[],'state_history':['bound','validated','executed','reloaded','verified']}
        result={'overall_status':'success','reply':one['message'],'task_plan':[task.model_dump()],
                'task_results':[one],'day_revision':data,'actions':[]}
        if data['status']=='awaiting_confirmation':
            from app.agent.semantic_runtime import make_pending, pending_result
            fresh=build_snapshot(db,user,snapshot.thread,snapshot.payload['dates'],day_revision=True,
                                 exercise_catalog=snapshot.payload['exercise_catalog'])
            apply=BoundTask(task_id='apply-day-revision',capability_id='day_plan.apply_revision',
                user_goal='按已展示的差异应用这份整日草案',
                resolution=Resolution(status='resolved',selected_id=data['revision_id'],candidate_ids=[data['revision_id']],basis='explicit'),
                arguments={'expected_revision':data['etag']},needs_confirmation=True)
            pending=make_pending(db,user,snapshot.thread,fresh,[apply],'确认应用这份草案吗？')
            pending.result={'day_revision':copy.deepcopy(data)}
            p=pending_result(pending)
            result.update(overall_status=p['overall_status'],pending_confirmation=p['pending_confirmation'],actions=p['actions'])
        return result
    except Exception as exc:
        if target['type']=='day_revision':
            row=db.get(DayPlanRevision,target['id'])
            if row and getattr(exc,'code',None) in {'conflict','expired'}:
                row.status='conflicted' if exc.code=='conflict' else 'expired'
            if row:data=service.payload(row)
        return {'overall_status':'failed','reply':'草案未能执行：'+str(exc)[:300],
                'task_plan':[task.model_dump()],'day_revision':data,'actions':[],
                'task_results':[{**base,'status':'failed','message':str(exc)[:300],'actual_changes':[],'unrelated_changes':[]}]}


def replan_question(data):
    records='；'.join(i['name']+' '+str(i['completed_sets'])+'组' for i in data['recorded']) or '尚无逐项完成记录'
    title='是否放弃原方案的未完成部分，重新制定今天的新目标？'
    remaining='原安排仍有 '+str(data['remaining_sets'])+' 组未达到目标。'
    if data.get('total_sets')==0:
        title='是否重新制定今天的新训练目标？';remaining='今天暂无有效训练安排。'
    elif data['remaining_sets']==0:
        title='是否保留已有记录，另行制定今天的新训练目标？';remaining='当前已有记录已达到原目标。'
    return (title+'\n当前系统已记录：'+records+
            '。\n'+remaining+'已有记录保留并单独展示，不计入新方案进度。'
            '\n确认后先生成新方案预览；最终应用新方案前，原安排保持不变。若系统记录与你的实际情况不符，可先核对或更正。')


def execute_replan(db,user,snapshot,task,target,confirmed,*,goal_text=None):
    from app.domain import workout_replan as replans
    from app.agent.semantic_runtime import make_pending,pending_result
    cap=capability(task.capability_id);args=cap.argument_model.model_validate(task.arguments)
    try:
        if requires_confirmation(task,{o['id']:o for o in snapshot.payload['objects']}) and not confirmed:
            raise ValueError('请先确认已展示的重排选择。')
        before=business_state(db,user,snapshot.payload['dates'])
        if task.capability_id=='day_plan.request_replan':
            data=replans.request(db,user,dt.date.fromisoformat(target['date']),scope=args.scope,
                expected_base_revision=args.expected_base_revision,goal_text=goal_text or db.info.get('coach_user_text') or task.user_goal)
        else:data=replans.approve(db,user,target['id'],args.expected_revision)
        if business_state(db,user,snapshot.payload['dates'])!=before:raise ValueError('重排确认不应改变实际安排或记录。')
        journal=CoachTaskExecution(user_id=user.id,thread=snapshot.thread,request_key=db.info.get('coach_request_key') or new_id(),
            task_id=task.task_id,effective_date=dt.date.fromisoformat(data['date']),operation=task.capability_id,
            domain='day_revision',scope='workout',goal=task.user_goal,tool='day_revision',arguments={'target_id':target['id'],**task.arguments},resource_changes=[],status='verified')
        db.add(journal);db.flush()
        message=replan_question(data) if data['status']=='pending' else '已确认保留已有记录，接下来重新制定目标并展示预览；原安排尚未改变。'
        result={'overall_status':'success','reply':message,'workout_replan':data,'actions':[],
            'task_plan':[task.model_dump()],'task_results':[{'task_id':task.task_id,'capability_id':task.capability_id,
                'status':'success','tool':'day_revision','tool_arguments':{'target_id':target['id'],**task.arguments},
                'message':message,'execution_id':journal.id,'actual_changes':[],'unrelated_changes':[]}]}
        if data['status']=='pending':
            fresh=build_snapshot(db,user,snapshot.thread,snapshot.payload['dates'],day_revision=True)
            confirm=BoundTask(task_id='confirm-workout-replan',capability_id='day_plan.confirm_replan',user_goal='确认保留实际记录并重新制定新训练目标',
                resolution=Resolution(status='resolved',selected_id=data['authorization_id'],candidate_ids=[data['authorization_id']],basis='explicit'),
                arguments={'expected_revision':data['etag']},needs_confirmation=True)
            saved=make_pending(db,user,snapshot.thread,fresh,[confirm],'')
            saved.result={'workout_replan':copy.deepcopy(data)}
            p=pending_result(saved);result.update(overall_status=p['overall_status'],pending_confirmation=p['pending_confirmation'],actions=p['actions'])
        return result
    except Exception as exc:
        if task.capability_id=='day_plan.confirm_replan' and getattr(exc,'code',None) in {'expired','conflict'}:
            row=replans.owned(db,user,target['id'])
            return request_consent_for_goal(db,user,snapshot,row.date,row.scope,row.goal_text,
                '此前确认已过期或安排发生变化，已重新核对，请确认本次最新安排。')
        return {'overall_status':'failed','reply':'重排尚未继续：'+str(exc),'actions':[],'task_results':[]}


def request_consent_for_goal(db,user,snapshot,day,scope,goal_text,reason):
    """Recover a missing workflow stage, never approve it or reuse its proposal."""
    fresh=build_snapshot(db,user,snapshot.thread,[str(day)],day_revision=True,
                         exercise_catalog=snapshot.payload.get('exercise_catalog',False))
    target=next(o for o in fresh.payload['objects'] if o['type']=='day' and o['date']==str(day))
    request=BoundTask(task_id='request-fresh-replan-consent',capability_id='day_plan.request_replan',
        user_goal='核对当前安排，确认保留真实记录并重新制定目标',
        resolution=Resolution(status='resolved',selected_id=target['id'],candidate_ids=[target['id']],basis='explicit'),
        arguments={'scope':scope,'expected_base_revision':target['revision_base_revision']})
    result=execute_replan(db,user,fresh,request,target,False,goal_text=goal_text)
    result['reply']=reason+'\n'+result['reply']
    return result


def recover_replan_consent(db,user,snapshot,task,target,args):
    from app.domain import workout_replan
    day=dt.date.fromisoformat(target['date']);base=service.digest(service.context(db,user,day))
    status='missing'
    if args.replan_authorization_id:
        row=workout_replan.owned(db,user,args.replan_authorization_id)
        if row.date!=day or 'workout' not in row.scope:
            raise service.RevisionError('scope','重排确认的日期或范围不匹配，需要重新核对。')
        status=workout_replan.effective_status(row,base)
        if status=='approved':return None
    reasons={'missing':'尚未保存可执行的重排确认，先核对当前安排。',
             'expired':'此前重排确认已过期，已按当前安排重新发起确认。',
             'stale':'安排或实际记录已经变化，已重新核对并发起确认。'}
    goal=json.dumps({'user_expression':db.info.get('coach_user_text') or '',
        'understood_goal':task.user_goal,'scope':args.scope,'constraints':args.constraints.model_dump(),
        'unapplied_proposal_reference':args.proposal.model_dump(exclude_unset=True)},ensure_ascii=False)
    result=request_consent_for_goal(db,user,snapshot,day,args.scope,goal,
        reasons.get(status,'此前重排选择尚未批准或已经结束，需要确认本次新目标。'))
    result.setdefault('task_results',[]).insert(0,{'task_id':task.task_id,'capability_id':task.capability_id,
        'user_goal':task.user_goal,'status':'blocked','tool':'day_revision',
        'tool_arguments':{'target_id':target['id'],**task.arguments},
        'message':'未生成草案，已转入真实的重排确认流程。','reason':status,
        'actual_changes':[],'unrelated_changes':[],'state_history':['bound','awaiting_replan_consent']})
    return result


def resume_replan(db,user,snapshot,result,client,day,trace):
    data=result.get('workout_replan')
    if not data or data['status']!='approved' or client is None:return None
    from app.agent import semantic_planner
    from app.agent.semantic_runtime import execute_plan
    try:
        fresh=build_snapshot(db,user,snapshot.thread,[data['date']],day_revision=True)
        from app.agent.coach_agent import recent_messages
        recent=recent_messages(db,user,snapshot.thread,limit=8)+[
            {'role':'agent','approved_replan':data,'confirmation_text':db.info.get('coach_user_text') or '',
             'text':'本次保存的重排选择已经确认。结合原会话中的目标、数量和限制生成新目标预览；不要执行历史失败任务，也不再请求同一授权。'}]
        for _ in range(2):
            db.commit()  # no writer lock during the model call
            from app.domain.timeutil import user_today
            from app.models import Profile
            parsed=semantic_planner.plan(client,data['goal_text'],fresh,recent,day,user_today(db.get(Profile,user.id)),trace)
            if parsed.mode!='load_context':break
            fresh=build_snapshot(db,user,snapshot.thread,[data['date']],day_revision=True,exercise_catalog=True)
        if parsed.mode in {'propose','noop'}:
            return {'overall_status':'clarification_required','reply':parsed.question or parsed.answer or '请告诉我新方案的训练时长与限制。','workout_replan':data,'actions':[],'task_results':result['task_results']}
        if len(parsed.tasks)!=1 or parsed.tasks[0].capability_id!='day_plan.create_revision':raise ValueError('确认重排后只能生成新目标草案。')
        task=parsed.tasks[0]
        if task.arguments.get('workout_policy')!='replace_remaining' or task.arguments.get('replan_authorization_id')!=data['authorization_id']:
            raise ValueError('新目标必须绑定本次已确认的重排授权。')
        next_result=execute_plan(db,user,fresh,parsed.tasks)
        next_result['workout_replan']=data
        next_result['task_results']=result['task_results']+next_result.get('task_results',[])
        return next_result
    except Exception as exc:
        return {'overall_status':'clarification_required','reply':'已保留本次重排确认，但新目标尚未生成：'+str(exc)[:200]+' 原安排未改变，可继续调整方案。',
                'workout_replan':data,'actions':[],'task_results':result['task_results']}
