"""Verified local lifecycle tasks; no natural-language interpretation here."""
import copy
import datetime as dt
from app.agent.capabilities import capability
from app.agent.business_snapshot import business_state
from app.agent.task_executor import validate_tasks,requires_confirmation,execute_tasks,resource_changes
from app.domain import workout_records as records
from app.models import CoachTaskExecution,new_id


def execute_mixed(db,user,snapshot,tasks,confirmed=False):
    objects=validate_tasks(tasks,snapshot);results=[];success=set();latest=None;revisions={}
    for task in tasks:
        cap=capability(task.capability_id)
        if not set(task.dependencies)<=success:
            results.append({'task_id':task.task_id,'capability_id':task.capability_id,'status':'blocked','message':'前置任务未完成。'});continue
        if cap.execution_boundary!='workout_lifecycle':
            one=execute_tasks(db,user,snapshot,[task.model_copy(update={'dependencies':[]})],confirmed=confirmed)[0]
            results.append(one)
            if one['status']=='success':success.add(task.task_id)
            continue
        base={'task_id':task.task_id,'capability_id':task.capability_id,'user_goal':task.user_goal,'tool':'workout_lifecycle',
            'tool_arguments':{'target_id':task.resolution.selected_id,**task.arguments}}
        try:
            if requires_confirmation(task,objects) and not confirmed:raise ValueError('训练记录任务尚未确认。')
            if not task.parameters_resolved:raise ValueError('记录参数尚未明确。')
            target=objects[task.resolution.selected_id];sid=target['parent_id'] if target['type']=='session_item' else target['id']
            with db.begin_nested():
                before=business_state(db,user,snapshot.payload['dates']);prior=records.read(db,user,sid)
                args=cap.argument_model.model_validate(task.arguments)
                prior_revision=prior['record_revision']
                if hasattr(args,'expected_revision') and sid in revisions:
                    original,last=revisions[sid]
                    if args.expected_revision==original and prior_revision==last:
                        args=args.model_copy(update={'expected_revision':last})
                initial_revision=revisions.get(sid,(prior_revision,prior_revision))[0]
                if task.capability_id=='workout.finish_session':data=records.finish(db,user,sid,args)
                elif task.capability_id=='workout.correct_record':data=records.correct(db,user,sid,target['id'],args)
                elif task.capability_id=='workout.record_progress':data=records.progress(db,user,sid,target['id'],args)
                else:data=records.read(db,user,sid)
                db.flush();db.expire_all();after=business_state(db,user,snapshot.payload['dates'])
                protected=copy.deepcopy(before);protected['sessions'][sid]=copy.deepcopy(after['sessions'][sid])
                if after!=protected:raise ValueError('修改了训练记录以外的资源，已回滚。')
                if not cap.mutates_state and before!=after:raise ValueError('查询意外修改状态。')
                changes=resource_changes(before,after)
                if prior['logs']!=data['logs']:changes.append({'kind':'workout_logs','key':sid,'before':prior['logs'],'after':data['logs']})
                eid=None
                if cap.mutates_state:
                    journal=CoachTaskExecution(user_id=user.id,thread=snapshot.thread,request_key=db.info.get('coach_request_key') or new_id(),
                        task_id=task.task_id,effective_date=dt.date.fromisoformat(target['date']),operation=task.capability_id,
                        domain='workout_lifecycle',scope=target['type'],goal=task.user_goal,tool=cap.executor,
                        arguments=base['tool_arguments'],resource_changes=changes,status='verified')
                    db.add(journal);db.flush();eid=journal.id
                label='已读取训练记录。' if not cap.mutates_state else '已结束训练并保存确认记录。' if task.capability_id=='workout.finish_session' else '已更新训练进度。' if task.capability_id=='workout.record_progress' else '已修订实际训练记录并保留审计。'
                count=sum(int(i.get('completed_sets') or 0) for i in data['session']['items'])
                message=label+'\n已记录 '+str(count)+' 组；'+str(len(data['unrecorded_items']))+' 个条目存在未记录或未完成部分。'
                if not cap.mutates_state:
                    if data['record_scope']=='prior_cycle':
                        message+='\n以上是重排前记录，单独保留，不计入当前新方案进度：'+ '；'.join(i['name']+' '+str(i.get('completed_sets') or 0)+'组' for i in data['session']['items'] if int(i.get('completed_sets') or 0)>0)+'。'
                    progress=data['current_day_progress']
                    message+='\n当前有效目标已记录 '+str(progress['done_sets'])+'/'+str(progress['total_sets'])+' 组，还差 '+str(progress['total_sets']-progress['done_sets'])+' 组。'
                if task.capability_id=='workout.finish_session' and args.completion_basis=='confirm_current_plan':
                    message+='本次按你确认的今日安排保存，计划默认值已注明来源；未提供的重量仍为空。'
                else:message+='未提供的重量和其他未确认数据保持未知。'
                one={**base,'status':'success','message':message,'data':data,'execution_id':eid,
                    'actual_changes':changes,'unrelated_changes':[],'state_history':['bound','validated','executed','reloaded','verified']}
                latest=data
                revisions[sid]=(initial_revision,data['record_revision'])
            success.add(task.task_id);results.append(one)
        except Exception as exc:
            results.append({**base,'status':'failed','message':str(exc)[:300],'actual_changes':[],'unrelated_changes':[]})
    n=sum(r['status']=='success' for r in results)
    result={'overall_status':'success' if n==len(results) else 'partial_success' if n else 'failed',
        'task_plan':[t.model_dump() for t in tasks],'task_results':results,'reply':'\n'.join(r['message'] for r in results),'actions':[]}
    if latest:result['workout_records']=latest
    return result
