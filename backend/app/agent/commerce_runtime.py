"""Commerce execution boundary, outside local fitness savepoints and DB locks."""
import json
import hashlib
from sqlalchemy import select
from app.agent.capabilities import capability
from app.agent.binding_contract import BoundTask
from app.agent.business_snapshot import build_snapshot, business_state
from app.domain import commerce as service
from app.models import CheckoutDraft, CommerceObject, CoachPendingPlan

READ_OPERATIONS={'commerce.nutrition','commerce.search_stores','commerce.menu','commerce.product_detail','commerce.select_store'}


def can_advance(tasks,result):
    # A quote may need one more model step to bind an already requested take way
    # to newly observed provider codes. This never authorizes order submission.
    return bool(tasks) and (all(t.capability_id in READ_OPERATIONS for t in tasks) or
        all(t.capability_id in READ_OPERATIONS | {'commerce.prepare_order','commerce.revise_order'} for t in tasks)
        and (result.get('checkout') or {}).get('status')=='needs_selection')


def _service_call(db,user,snapshot,task,confirmed):
    p=service.provider(db);uid=user.id;thread=snapshot.thread;op=task.capability_id
    target=task.resolution.selected_id;args=capability(op).argument_model.model_validate(task.arguments).model_dump()
    if op=='commerce.nutrition':
        service.owned(db,uid,thread,p.account_key,target,'commerce_account');db.commit()
        with p.connect() as client:table=service.nutrition_table(db,uid,thread,p,client)
        db.commit()
        return {'status':'success','data':{'official_nutrition':table},'nutrition':table,'message':'已读取麦当劳官方营养表，共'+str(len(table['items']))+'项。'}
    if op=='commerce.search_stores':
        service.owned(db,uid,thread,p.account_key,target,'commerce_account');db.commit()
        search={'city':args['city'],'keyword':args['keyword'],'beType':1,'searchType':2}
        from app.domain import commerce_availability as availability
        with p.connect() as client:objects,context=availability.search(db,uid,thread,p,client,search,args['selection_policy'])
        if not any(x['open'] for x in objects):raise availability.switch_error(objects,context)
        root,_=availability.current_context(db,uid,thread,p)
        approved=root.payload.get('selected_store') or {}
        if context['selection_policy']=='nearest' and context['nearest_id']!=context['nearest_open_id'] and (approved.get('store_id')!=context['nearest_open_id'] or approved.get('search_revision')!=context['revision']):
            raise availability.switch_error(objects,context)
        return {'status':'success','data':{'stores':objects},'message':'查询到'+str(len(objects))+'家麦当劳门店。距离是官方接口返回值。'}
    if op=='commerce.select_store':
        from app.domain import commerce_availability as availability
        chosen=availability.choose(db,uid,thread,p,target,args['search_revision'])
        return {'status':'success','data':{'selected_store':chosen},'message':'已确认使用「'+chosen['name']+'」；继续准备原请求的报价，尚未创建订单。'}
    if op in {'commerce.menu','commerce.product_detail'}:
        kind='commerce_store' if op=='commerce.menu' else 'commerce_product'
        row=service.owned(db,uid,thread,p.account_key,target,kind);db.commit()
        with p.connect() as client:
            if op=='commerce.menu':
                from app.domain import commerce_availability as availability
                availability.check(db,uid,thread,p,row,client,manual=bool(db.info.get('commerce_manual_store')))
                data=client.call_read('query-meals',{'storeCode':row.external_key,'orderType':1,'beType':1})
                ids=service.save_menu(db,uid,thread,p,row,data)
                db.commit()
                if not ids:
                    raise availability.AvailabilityError('empty_menu','「'+row.payload['name']+'」当前没有返回可点餐品，请换一家餐厅或稍后再试。',store={'id':row.id,**row.payload})
                nutrition=None
                if 'list-nutrition-foods' in client.discover():
                    try:nutrition=service.nutrition_table(db,uid,thread,p,client)
                    except Exception:db.rollback()
                exact={x['name']:x for x in (nutrition or {}).get('items',[])}
                public=[{'id':id,**db.get(CommerceObject,id).payload,'nutrition':exact.get(db.get(CommerceObject,id).payload['name'])} for id in ids]
            else:
                store=service.owned(db,uid,thread,p.account_key,row.parent_key,'commerce_store')
                from app.domain import commerce_availability as availability
                availability.check(db,uid,thread,p,store,client)
                detail=client.call_read('query-meal-detail',{'storeCode':store.external_key,'orderType':1,'beType':1,'code':row.external_key})
                service.save_detail(db,uid,thread,p,row,detail)
                public=[{'id':x.id,**x.payload} for x in db.scalars(select(CommerceObject).where(CommerceObject.parent_key==row.id,CommerceObject.kind=='commerce_option')) if x.payload.get('active',True)]
        db.commit()
        if op=='commerce.product_detail':
            summary=row.payload['detail_summary']
            product={'id':row.id,**{k:v for k,v in row.payload.items() if k!='detail'},
                     'menu_name':row.payload['name'],'name':summary['name']}
            message='已读取「'+product['name']+'」的商品详情。'
            message+=('含 '+str(len(summary['round_groups']))+' 组套餐选项和 '+str(len(summary['modification_groups']))+' 组特制选项；当前商品已保留。' if summary['option_group_count'] else '官方未返回额外可选项，可沿用当前商品准备报价预览。')
            return {'status':'success','data':{'items':public,'store_id':store.id,'store':store.payload,
                    'product':product,'detail':summary},'message':message}
        return {'status':'success','data':{'items':public,'store_id':row.id,'store':row.payload},'nutrition':nutrition,
                'message':'已读取真实菜单，共'+str(len(public))+'项。'}
    if op=='commerce.prepare_order':
        row=service.quote_preview(db,uid,thread,p,target,args['items'],args['take_way_code'],
              key=hashlib.sha256(((db.info.get('coach_request_key') or 'request')+':'+task.task_id).encode()).hexdigest(),take_way_preference=args['take_way_preference'])
        db.commit();return {'status':'success','checkout':service.public_draft(row),'message':'订单尚未提交。\n'+service.preview_text(row)}
    if op=='commerce.revise_order':
        row=service.draft_owned(db,uid,thread,p.account_key,target)
        lines=args['items'] if args['items'] is not None else [x['selection'] for x in row.cart]
        way=args['take_way_code'] if args['take_way_code'] is not None else '' if args['take_way_preference'] else row.take_way_code
        row=service.quote_preview(db,uid,thread,p,row.store_id,lines,way,draft_id=row.id,version=args['version'],take_way_preference=args['take_way_preference'])
        db.commit();return {'status':'success','checkout':service.public_draft(row),'message':'已更新订单预览，尚未提交。\n'+service.preview_text(row)}
    if op=='commerce.submit_order':return service.submit(db,uid,thread,p,target,args['version'],confirmed=confirmed,existing_order_decision=args['existing_order_decision'])
    if op=='commerce.cancel_order':return service.cancel_existing_order(db,uid,thread,p,target,args['version'],args['existing_checkout_id'],args['decision_fingerprint'],confirmed=confirmed)
    if op=='commerce.review_existing_orders':return service.refresh_conflict(db,uid,thread,p,target,args['version'])
    if op=='commerce.query_order':return service.query_checkout(db,uid,thread,p,target)
    if op=='commerce.cancel_checkout':
        row=service.cancel_draft(db,uid,thread,p,target,args['version']);db.commit()
        return {'status':'success','checkout':service.public_draft(row),'message':'已取消未提交订单草案，没有创建订单。'}
    raise service.CheckoutError('不支持该商务能力。')


def attach_checkout(db,user,thread,result,checkout,dates, *, offer_submission=True):
    from app.agent.semantic_runtime import make_pending, pending_payload
    result['checkout']=checkout
    status=checkout['status']
    for saved in db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,CoachPendingPlan.thread==thread,CoachPendingPlan.status=='pending')):
        if any(t['capability_id'] in {'commerce.submit_order','commerce.revise_order'} and t['resolution'].get('selected_id')==checkout['id']
               and t['arguments'].get('version')!=checkout['version'] for t in saved.tasks):saved.status='cancelled'
    conflict=result.get('order_conflict')
    if conflict:
        result['actions']=[{'type':'checkout_existing_query','label':'刷新旧订单状态','checkout_id':checkout['id'],'version':checkout['version']}]
        roots=db.scalars(select(CommerceObject).where(CommerceObject.user_id==user.id,CommerceObject.account_key==service.provider(db).account_key,CommerceObject.kind=='commerce_account'))
        cancel_available=service.provider(db).allow_orders and any('cancel-order' in root.payload.get('tools',[]) for root in roots)
        for old in conflict['orders']:
            if old['can_cancel'] and cancel_available:
                result['actions'].append({'type':'checkout_existing_cancel','label':'取消旧单','checkout_id':checkout['id'],
                    'version':checkout['version'],'existing_checkout_id':old['checkout_id'],'decision_fingerprint':conflict['fingerprint']})
        if conflict['can_create_separately']:
            result['actions'].append({'type':'checkout_place_new','label':'保留旧单，确认另下一单','checkout_id':checkout['id'],
                'version':checkout['version'],'decision_fingerprint':conflict['fingerprint']})
        result['overall_status']='existing_order'
        return
    ways=[{'type':'checkout_way','label':w['title'],'checkout_id':checkout['id'],'version':checkout['version'],
           'take_way_code':w['code']} for w in checkout['quote']['take_ways']]
    if status=='needs_selection':
        result['actions']=ways
        result['reply']+='\n请选择取餐方式后核对并确认。'
    elif status=='awaiting_confirmation':
        if not offer_submission:
            result['actions'] = []
            return
        if not service.provider(db).allow_orders:
            result['actions']=[]
            result['reply']+='\n当前配置关闭订单提交，此预览不能下单。'
            result['overall_status']='proposed'
            return
        snapshot=build_snapshot(db,user,thread,dates)
        task=BoundTask(task_id='checkout:'+checkout['id'],capability_id='commerce.submit_order',
                      user_goal='按此订单预览创建麦当劳待支付订单',arguments={'version':checkout['version']},
                      resolution={'status':'resolved','selected_id':checkout['id'],'candidate_ids':[checkout['id']],'basis':'explicit'})
        # One pending transaction per draft version. A stale preview has no authority.
        existing=None
        for row in db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,CoachPendingPlan.thread==thread,CoachPendingPlan.status=='pending')):
            if any(t.get('capability_id')=='commerce.submit_order' and t['resolution'].get('selected_id')==checkout['id'] for t in row.tasks):
                if row.tasks==[task.model_dump()]:existing=row
                else:row.status='cancelled'
        pending=existing or make_pending(db,user,thread,snapshot,[task],result['reply'])
        pending.result={**(pending.result or {}),'checkout':checkout}
        result['pending_confirmation']=pending_payload(pending)
        result['actions']=[*ways,{'type':'confirm_tasks','label':'确认创建待支付订单','pending_id':pending.id,'version':pending.version},
                           {'type':'cancel_tasks','label':'取消订单草案','pending_id':pending.id,'version':pending.version}]
        result['overall_status']='confirmation_required'
    elif status in {'unknown','submitting','verifying','submitted'}:
        result['actions']=[{'type':'checkout_query','label':'查询订单状态','checkout_id':checkout['id'],'version':checkout['version']}]


def execute_mixed(db,user,snapshot,tasks,confirmed):
    from app.agent.task_executor import execute_tasks, requires_confirmation, validate_tasks
    from app.agent.semantic_runtime import make_pending, pending_result
    objects=validate_tasks(tasks,snapshot)
    results=[];success=set();last_checkout=None;last_order=None;catalog=None;nutrition=None;credential_required=False;order_conflict=None;recovery=None
    for task in tasks:
        cap=capability(task.capability_id)
        base={'task_id':task.task_id,'capability_id':task.capability_id,'user_goal':task.user_goal,
              'tool':cap.executor,'tool_arguments':{'target_id':task.resolution.selected_id,**task.arguments}}
        if not set(task.dependencies)<=success:
            results.append({**base,'status':'blocked','message':'前置任务尚未成功。'});continue
        if requires_confirmation(task,objects) and not confirmed:
            raise service.CheckoutError('任务尚未确认。')
        if cap.execution_boundary!='commerce':
            if cap.execution_boundary=='workout_lifecycle':
                from app.agent.workout_runtime import execute_mixed as execute_workout
                one=execute_workout(db,user,snapshot,[task.model_copy(update={'dependencies':[]})],confirmed)['task_results'][0]
            else:
                one=execute_tasks(db,user,snapshot,[task.model_copy(update={'dependencies':[]})],confirmed=confirmed)[0]
            db.commit();results.append(one)
            if one['status']=='success':success.add(task.task_id)
            continue
        before=business_state(db,user,snapshot.payload['dates']);db.commit()
        try:
            data=_service_call(db,user,snapshot,task,confirmed)
            if business_state(db,user,snapshot.payload['dates'])!=before:
                raise service.CheckoutError('商务操作意外改变了训练或饮食状态。')
            status=data['status'];message=data['message']
            one={**base,'status':status,'message':message,'data':data.get('data') or data.get('order'),
                 'checkout':data.get('checkout'),'actual_changes':[],'unrelated_changes':[]}
            if data.get('checkout'):last_checkout=data['checkout']
            if data.get('order'):last_order=data['order']
            if data.get('order_conflict'):order_conflict=data['order_conflict']
            if data.get('nutrition'):nutrition=data['nutrition']
            if task.capability_id in READ_OPERATIONS and task.capability_id not in {'commerce.nutrition','commerce.select_store'}:
                payload=data.get('data') or {}
                catalog={'kind':'stores' if task.capability_id=='commerce.search_stores' else 'products' if task.capability_id=='commerce.menu' else 'detail',
                         'items':payload.get('stores') or payload.get('items') or [],'store_id':payload.get('store_id'),'store':payload.get('store'),'product':payload.get('product'),'detail':payload.get('detail')}
            if status=='success':success.add(task.task_id)
            results.append(one)
        except Exception as exc:
            from app.food.mcd_transport import McpAuthenticationError,McpHttpError,McpBusinessError
            from app.domain.commerce_availability import AvailabilityError
            credential_required=credential_required or isinstance(exc,(McpAuthenticationError,service.CommerceCredentialError))
            db.rollback()
            message='本次麦当劳操作未能完成，请刷新门店或菜单后再试；没有自动重试下单。'
            if isinstance(exc,AvailabilityError):
                recovery=availability_result(db,user,snapshot,task,exc)
                message=recovery['reply']
            elif isinstance(exc,McpBusinessError):
                recovery=business_rejection_result(db,user,snapshot,task,exc)
                message=recovery['reply']
            elif isinstance(exc,service.CheckoutError):message=str(exc)[:220]
            elif isinstance(exc,(McpAuthenticationError,service.CommerceCredentialError)):message='麦当劳账号连接已失效，请在Token设置重新连接后继续。'
            if isinstance(exc,McpHttpError) and exc.status_code in {502,503,504}:
                message='麦当劳服务暂时未能返回'+('门店菜单' if task.capability_id=='commerce.menu' else '查询结果')+'，请稍后重试。已保留本轮点餐要求，没有创建订单。'
            results.append({**base,'status':'failed','message':message,'actual_changes':[],'error_code':str(exc)[:80],'provider_code':getattr(exc,'provider_code',None),'provider_message':getattr(exc,'provider_message','')})
    statuses=[r['status'] for r in results]
    overall='unknown' if any(x in {'unknown','submitting','verifying'} for x in statuses) else 'success' if all(x=='success' for x in statuses) else 'partial_success' if 'success' in statuses else 'failed'
    result={'overall_status':overall,'task_plan':[t.model_dump() for t in tasks],'task_results':results,
            'reply':'\n'.join(r['message'] for r in results),'actions':[],'changes':[r['message'] for r in results if r.get('execution_id')]}
    if credential_required:result['commerce_connection']={'status':'token_required','guide_url':'https://open.mcd.cn/mcp'}
    if nutrition:result['commerce_nutrition']=nutrition
    workout=next((r['data'] for r in reversed(results) if r.get('tool')=='workout_lifecycle' and r.get('status')=='success'),None)
    if workout:result['workout_records']=workout
    if last_order:result['order']=last_order
    if order_conflict:result['order_conflict']=order_conflict
    if catalog:result['commerce_catalog']=catalog
    if recovery:result.update(recovery)
    if last_checkout and not recovery:attach_checkout(db,user,snapshot.thread,result,last_checkout,snapshot.payload['dates'],
        offer_submission=any(t.capability_id in {'commerce.prepare_order','commerce.revise_order','commerce.submit_order'} for t in tasks))
    return result


def continue_plan(db,user,text,snapshot,recent,date,client,trace,result):
    from app.agent import semantic_planner
    from app.agent.semantic_runtime import execute_plan, make_pending, pending_result
    from app.agent.task_executor import validate_tasks, requires_confirmation
    accumulated=list(result.get('task_results',[]));seen=set()
    for _ in range(4):
        snapshot=build_snapshot(db,user,snapshot.thread,snapshot.payload['dates']);db.commit()
        observations={'role':'agent','text':'本轮已经真实执行的工具结果；仅继续未完成目标，不得重复已执行操作。','task_results':accumulated}
        plan=semantic_planner.plan(client,text,snapshot,recent+[observations],date,date,trace)
        if plan.mode=='load_context':
            if plan.context_request and plan.context_request.commerce:
                service.load_context(db,user.id,snapshot.thread,service.provider(db));continue
            break
        if plan.snapshot_id!=snapshot.id:raise service.CheckoutError('规划使用了错误的商务快照。')
        objects=validate_tasks(plan.tasks,snapshot)
        if plan.mode in {'noop','propose'} or not plan.tasks:
            status='proposed' if plan.mode=='propose' else 'clarification_required' if plan.question or plan.unfulfilled_goals else 'success'
            result={**result,'overall_status':status,
                    'reply':plan.answer or plan.question or result['reply'],'actions':[],'task_results':[]}
        elif any(t.resolution.status!='resolved' or requires_confirmation(t,objects) for t in plan.tasks):
            previous=result
            result=pending_result(make_pending(db,user,snapshot.thread,snapshot,plan.tasks,plan.question))
            # Preserve the real selected cart when a further selection is asked.
            # It is reference/UI state, never submit authorization for this step.
            if previous.get('checkout') and any(t.capability_id in {'commerce.revise_order','commerce.submit_order'} and t.resolution.selected_id==previous['checkout']['id'] for t in plan.tasks):
                result['checkout']=previous['checkout']
                result['actions']=[*[a for a in previous.get('actions',[]) if a['type']=='checkout_way'],*result['actions']]
                saved=db.get(CoachPendingPlan,result['pending_confirmation']['pending_id'])
                saved.result={**(saved.result or {}),'checkout':previous['checkout']}
            save_read_continuation(db,result,plan.tasks,text,plan.continue_after_tools or bool(plan.unfulfilled_goals))
        else:
            for t in plan.tasks:
                signature=json.dumps([t.capability_id,t.resolution.selected_id,t.arguments],sort_keys=True)
                if signature in seen:raise service.CheckoutError('工具推进重复，已停止，避免重复操作。')
                seen.add(signature)
            result=execute_plan(db,user,snapshot,plan.tasks)
        accumulated.extend(result.get('task_results',[]));result['task_results']=list(accumulated)
        if result.get('overall_status')=='failed' and any(r['status']=='success' for r in accumulated):result['overall_status']='partial_success'
        result.pop('unfulfilled_goals',None)
        if plan.unfulfilled_goals:
            from app.agent.reply_evidence import remaining_commerce_goals
            plan.unfulfilled_goals = remaining_commerce_goals(client,plan.unfulfilled_goals,result,trace)
        if plan.unfulfilled_goals:
            result['unfulfilled_goals']=plan.unfulfilled_goals;result['reply']+='\n尚未完成：'+'；'.join(plan.unfulfilled_goals)
        if not (plan.continue_after_tools and can_advance(plan.tasks,result)
                and all(r['status']=='success' for r in accumulated[-len(plan.tasks):])):
            return result
    result['reply']+='\n本轮工具推进已到上限，请继续指定下一步。'
    return result


def structured_action(db,user,thread,day,command):
    p=service.provider(db);row=service.draft_owned(db,user.id,thread,p.account_key,command.checkout_id)
    if command.action in {'query_existing','cancel_existing','place_new'}:
        if command.action=='query_existing':data=service.refresh_conflict(db,user.id,thread,p,row.id,command.version)
        elif command.action=='place_new':
            saved=row.quote.get('order_conflict') or {}
            if not command.decision_fingerprint or saved.get('fingerprint')!=command.decision_fingerprint:
                raise service.CheckoutError('旧订单提示或预览已变化，请先刷新核对。')
            data=service.submit(db,user.id,thread,p,row.id,command.version,confirmed=True,existing_order_decision=command.decision_fingerprint)
        else:
            from app.agent.semantic_runtime import make_pending,pending_result
            saved=row.quote.get('order_conflict') or {}
            if row.version!=command.version or saved.get('fingerprint')!=command.decision_fingerprint or command.existing_checkout_id not in {x['checkout_id'] for x in saved.get('orders',[])}:
                raise service.CheckoutError('旧订单选择已变化，请刷新核对。')
            snapshot=build_snapshot(db,user,thread,[str(day)])
            task=BoundTask(task_id='cancel-existing:'+row.id,capability_id='commerce.cancel_order',user_goal='取消指定未支付旧订单，保留新预览但不自动下单',
                arguments={'version':row.version,'existing_checkout_id':command.existing_checkout_id,'decision_fingerprint':command.decision_fingerprint},
                resolution={'status':'resolved','selected_id':row.id,'candidate_ids':[row.id],'basis':'explicit'})
            pending=make_pending(db,user,thread,snapshot,[task],'仅取消旧订单，不会自动创建新订单。')
            pending.result={'checkout':service.public_draft(row)}
            result=pending_result(pending)
            result['checkout']=service.public_draft(row)
            return result
        result={'overall_status':data['status'],'reply':data['message'],'actions':[],'task_results':[]}
        if data.get('order'):result['order']=data['order']
        if data.get('order_conflict'):result['order_conflict']=data['order_conflict']
        attach_checkout(db,user,thread,result,data['checkout'],[str(day)])
        return result
    if command.action=='query':data=service.query_checkout(db,user.id,thread,p,row.id)
    elif command.action=='cancel':
        row=service.cancel_draft(db,user.id,thread,p,row.id,command.version);db.commit()
        for pending in db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,CoachPendingPlan.thread==thread,CoachPendingPlan.status=='pending')):
            if any(t.get('capability_id')=='commerce.submit_order' and t['resolution'].get('selected_id')==row.id for t in pending.tasks):pending.status='cancelled'
        db.commit()
        data={'checkout':service.public_draft(row),'message':'已取消订单草案。','status':'success'}
    else:
        row=service.quote_preview(db,user.id,thread,p,row.store_id,[x['selection'] for x in row.cart],command.take_way_code,
                                  draft_id=row.id,version=command.version)
        db.commit();data={'checkout':service.public_draft(row),'message':service.preview_text(row),'status':'success'}
    result={'overall_status':data['status'],'reply':data['message'],'actions':[],'task_results':[]}
    if data.get('order'):result['order']=data['order']
    attach_checkout(db,user,thread,result,data['checkout'],[str(day)], offer_submission=command.action!='query')
    return result


def catalog_action(db,user,thread,day,command):
    from app.agent.binding_contract import BoundTask
    p=service.provider(db)
    editing=None
    if command.action=='product_detail' and command.checkout_id:
        editing=service.draft_owned(db,user.id,thread,p.account_key,command.checkout_id)
        product=service.owned(db,user.id,thread,p.account_key,command.target_id,'commerce_product')
        if editing.version!=command.version or editing.status not in {'awaiting_confirmation','needs_selection'} or product.parent_key!=editing.store_id:
            raise service.CheckoutError('商品不属于当前有效订单的门店或版本。')
    if command.action=='checkout_menu':
        row=service.draft_owned(db,user.id,thread,p.account_key,command.target_id)
        if row.version!=command.version or row.status not in {'awaiting_confirmation','needs_selection'}:
            raise service.CheckoutError('订单预览已变化，请使用最新版本重新选餐。')
        snapshot=build_snapshot(db,user,thread,[str(day)])
        task=BoundTask(task_id='manual:checkout-menu',capability_id='commerce.menu',user_goal='查看当前订单门店菜单，订单尚未修改',arguments={},
            resolution={'status':'resolved','selected_id':row.store_id,'candidate_ids':[row.store_id],'basis':'explicit'})
        result=execute_mixed(db,user,snapshot,[task],False)
        attach_checkout(db,user,thread,result,service.public_draft(row),[str(day)])
        result['commerce_view']='edit_menu';return result
    if command.action=='connect':
        service.load_context(db,user.id,thread,p)
        return {'overall_status':'success','reply':'麦当劳已连接。可以查询门店、营养或继续点餐。','actions':[],
            'commerce_connection':{'status':'ready','guide_url':'https://open.mcd.cn/mcp'}}
    if command.action=='search_stores' and (not command.city.strip() or not command.keyword.strip()):
        raise service.CheckoutError('请填写城市和门店或地标。')
    snapshot=build_snapshot(db,user,thread,[str(day)])
    target=command.target_id
    if command.action in {'nutrition','search_stores'}:
        target=next(o['id'] for o in snapshot.payload['objects'] if o['type']=='commerce_account')
    args={}
    if command.action=='search_stores':args={'city':command.city,'keyword':command.keyword}
    if command.action=='prepare_order':args={'items':command.items,'take_way_code':command.take_way_code}
    if command.action=='revise_order':
        if command.version is None:raise service.CheckoutError('重新选餐必须提供订单版本。')
        args={'items':command.items,'version':command.version}
    task=BoundTask(task_id='manual:'+command.action,capability_id='commerce.'+command.action,user_goal='用户在麦当劳模块选择的操作',
        arguments=args,resolution={'status':'resolved','selected_id':target,'candidate_ids':[target],'basis':'explicit'})
    db.commit()
    db.info['commerce_manual_store']=command.action=='menu'
    try:result=execute_mixed(db,user,snapshot,[task],False)
    finally:db.info.pop('commerce_manual_store',None)
    if editing:
        attach_checkout(db,user,thread,result,service.public_draft(editing),[str(day)])
        result['commerce_view']='edit_menu'
    return result


def save_read_continuation(db,result,tasks,goal_text,requested):
    id=(result.get('pending_confirmation') or {}).get('pending_id')
    if not id or not requested or not tasks or not all(t.capability_id in READ_OPERATIONS for t in tasks):return
    pending=db.get(CoachPendingPlan,id)
    if pending and pending.tasks==[t.model_dump() for t in tasks]:
        pending.result={**(pending.result or {}),'read_continuation':{'goal_text':goal_text,'snapshot_id':pending.snapshot_id}}


def resume_read_pending(db,user,pending,snapshot,tasks,result,client,day,trace,confirmation_text):
    from app.agent.coach_agent import recent_messages
    if not tasks or not all(t.capability_id in READ_OPERATIONS for t in tasks) or result['overall_status']!='success':return None
    context=(pending.result or {}).get('read_continuation') or {}
    if not context and not (pending.result or {}).get('unfulfilled_goals'):return None
    goal=context.get('goal_text') or pending.question
    recent=recent_messages(db,user,snapshot.thread,limit=8)
    recent.append({'role':'user','text':confirmation_text,'original_requested_cart':context.get('requested_cart'), 'previous_checkout_id':context.get('previous_checkout_id'),'confirmed_read_tasks':[t.model_dump() for t in tasks],
                   'authorization':'只批准已保存的读取或换店选择；后续生成报价不构成提交订单授权。'})
    original=result
    try:
        return continue_plan(db,user,goal,snapshot,recent,day,client,trace,result)
    except Exception as exc:
        trace.append({'stage':'commerce_read_continuation','status':'failed','error_type':type(exc).__name__})
        return {**original,'overall_status':'partial_success','reply':original['reply']+'\n所选商品已保留；后续报价暂未完成，可从商品卡片继续查看预览。',
                'unfulfilled_goals':['继续处理已确认读取后的剩余目标。']}


def availability_result(db,user,snapshot,task,issue):
    from app.agent.semantic_runtime import make_pending,pending_result
    result={'overall_status':'unavailable','reply':str(issue),'actions':[],
            'commerce_availability':{'code':issue.code,'message':str(issue),'store':issue.store,'product':issue.product,'choice':issue.choice}}
    if issue.stores:result['commerce_catalog']={'kind':'stores','items':issue.stores}
    elif issue.store:result['actions']=[{'type':'commerce_recovery_menu','label':'查看这家店的最新菜单','target_id':issue.store['id']}]
    if issue.choice:
        current=build_snapshot(db,user,snapshot.thread,snapshot.payload['dates'])
        target=issue.choice['store_id']
        selection=BoundTask(task_id='select-store:'+task.task_id[:45],capability_id='commerce.select_store',
            user_goal='确认改用「'+issue.choice['store']['name']+'」继续原点餐请求',
            arguments={'search_revision':issue.choice['search_revision']},
            resolution={'status':'resolved','selected_id':target,'candidate_ids':[target],'basis':'explicit'})
        pending=make_pending(db,user,snapshot.thread,current,[selection],str(issue))
        context={'goal_text':db.info.get('coach_user_text') or task.user_goal,'snapshot_id':current.id}
        if task.capability_id in {'commerce.submit_order','commerce.revise_order'}:
            draft=service.draft_owned(db,user.id,snapshot.thread,service.provider(db).account_key,task.resolution.selected_id)
            context.update(goal_text='为原先已预览的餐品，在刚确认的新门店准备新的报价预览；不要提交订单。',
                requested_cart=[{'name':x['name'],'quantity':x['quantity']} for x in draft.cart],previous_checkout_id=draft.id)
        pending.result={'read_continuation':context}
        result.update(pending_result(pending));result['reply']=str(issue)
    return result


def business_rejection_result(db,user,snapshot,task,exc):
    from app.domain.commerce_availability import AvailabilityError
    p=service.provider(db);target=task.resolution.selected_id;store=None;products=[]
    if task.capability_id in {'commerce.prepare_order','commerce.menu'}:
        row=service.owned(db,user.id,snapshot.thread,p.account_key,target,'commerce_store');store={'id':row.id,**row.payload}
    elif task.capability_id=='commerce.product_detail':
        product=service.owned(db,user.id,snapshot.thread,p.account_key,target,'commerce_product')
        row=service.owned(db,user.id,snapshot.thread,p.account_key,product.parent_key,'commerce_store');store={'id':row.id,**row.payload}
    elif task.capability_id in {'commerce.submit_order','commerce.revise_order'}:
        draft=service.draft_owned(db,user.id,snapshot.thread,p.account_key,target)
        row=service.owned(db,user.id,snapshot.thread,p.account_key,draft.store_id,'commerce_store');store={'id':row.id,**row.payload}
    if store:
        # A business rejection may reflect a just-closed restaurant. Re-read facts,
        # never infer its cause by parsing provider prose or retry the failed write.
        try:
            from app.domain import commerce_availability as availability
            with p.connect() as client:availability.check(db,user.id,snapshot.thread,p,row,client)
        except AvailabilityError as issue:return availability_result(db,user,snapshot,task,issue)
        except Exception:db.rollback()
    stage={'commerce.prepare_order':'准备报价','commerce.revise_order':'更新报价','commerce.menu':'读取菜单','commerce.product_detail':'读取商品详情','commerce.submit_order':'创建订单'}.get(task.capability_id,'完成操作')
    name='「'+store['name']+'」' if store else '麦当劳服务'
    message=name+'未能'+stage+'。'+('官方说明：'+exc.provider_message+'。' if exc.provider_message else '官方未提供具体原因，暂时无法确定是否营业或有库存。')
    message+='请刷新该店菜单或重新选择餐品/门店；本次没有创建订单。'
    result={'overall_status':'unavailable','reply':message,'actions':[],
        'commerce_availability':{'code':'provider_business_rejected','provider_code':exc.provider_code,'message':message,'store':store}}
    if store:result['actions']=[{'type':'commerce_recovery_menu','label':'刷新这家店的菜单','target_id':store['id']}]
    return result
