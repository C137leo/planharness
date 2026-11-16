"""Typed day proposals and atomic application. No natural-language decisions here.

Chat and Diff use the same saved confirmation/version contract as this service.
"""
import copy
import datetime as dt
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from app.models import DayPlanRevision, MealLog, WorkoutLog, WorkoutSession, Profile, new_id
from app.domain.day_plan import effective_day, save_override, _estimated_minutes, allowed_equipment, _norm_equipment
from app.domain.timeutil import as_utc, day_end_utc
from app.domain.catalog import all_exercise_refs, name_of
from app.domain.nutrition_catalog import FOOD_MACROS_PER_100G, FOOD_TAGS
from app.domain.workout_records import lock


class RevisionError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ActionTarget(Strict):
    id: str | None = Field(default=None, description='保留或调整当天既有动作时，逐字复制该动作的真实日期对象ID；省略表示新增目录动作。')
    action_ref: str | None = Field(default=None, description='新增/替换仅允许目录action_ref；已有领域生成动作不在目录时，必须保留其真实id，不能当作新增。')
    slot: Literal['warmup', 'strength', 'cardio', 'mobility'] | None = None
    sets: int | None = Field(default=None, ge=1, le=10)
    reps: int | None = Field(default=None, ge=1, le=100)
    duration_min: int | None = Field(default=None, ge=1, le=180)

    @model_validator(mode='after')
    def require_identity_or_catalog(self):
        if self.id is None and self.action_ref not in all_exercise_refs():
            raise ValueError('新增动作必须给真实目录action_ref；保留快照中已有的非目录动作时，请复制该动作的真实日期id。不得猜测新引用或省略既有对象身份。')
        return self


class FoodTarget(Strict):
    ingredient_key: Literal[tuple(FOOD_MACROS_PER_100G)] = Field(description='精确食材目录键（中文名称枚举），不是 ingredient 对象的 UUID。')
    grams: float = Field(gt=0, le=1000)


class MealTarget(Strict):
    slot: Literal['breakfast', 'lunch', 'dinner', 'snack']
    items: list[FoodTarget] = Field(max_length=20)


class TargetProposal(Strict):
    # A present section is a complete desired state, never a sequence of local tools.
    actions: list[ActionTarget] | None = Field(default=None, max_length=30)
    meals: list[MealTarget] | None = Field(default=None, max_length=4)


class MealIngredientRule(Strict):
    slot: Literal['breakfast','lunch','dinner','snack']
    ingredient_options: list[Literal[tuple(FOOD_MACROS_PER_100G)]] = Field(min_length=1,max_length=10, description='同一食材要求允许的真实目录选项，只能选择本枚举实际提供的键。')
    requirement: Literal['include','exclude'] = 'include'
    grams: float | None = Field(default=None,gt=0,le=1000)


class Constraints(Strict):
    max_workout_minutes: int | None = Field(default=None, ge=1, le=240)
    min_workout_minutes: int | None = Field(default=None,ge=1,le=240)
    target_workout_minutes: int | None = Field(default=None,ge=1,le=240,description='用户要求练到/调整为的整场目标时长，不能只保存为上限。')
    workout_time_tolerance_min: int = Field(default=0,ge=0,le=30,description='目标时长的允许偏差；明确目标默认0，只有约/左右的请求才给合理偏差。')
    max_workout_actions: int | None = Field(default=None, ge=1, le=30, description='正式动作条目数量上限，不是组数。新目标未指定数量时默认3；用户明确要求更多时可给对应数量。')
    include_warmup_in_action_count: bool = Field(default=False, description='仅用户明确将热身计入动作总数时为true；默认数量上限不计热身。')
    excluded_ingredients: list[str] = Field(default_factory=list, max_length=30)
    meal_ingredient_rules: list[MealIngredientRule] = Field(default_factory=list,max_length=30)

    @model_validator(mode='after')
    def compatible_time_bounds(self):
        if self.min_workout_minutes is not None and self.max_workout_minutes is not None and self.min_workout_minutes>self.max_workout_minutes:
            raise ValueError('训练时长下限不能大于上限。')
        if self.target_workout_minutes is not None:
            lo=self.target_workout_minutes-self.workout_time_tolerance_min;hi=self.target_workout_minutes+self.workout_time_tolerance_min
            if (self.max_workout_minutes is not None and self.max_workout_minutes<lo) or (self.min_workout_minutes is not None and self.min_workout_minutes>hi):
                raise ValueError('目标时长与已有上下限冲突；用户修改目标时须同步更新或清除过时边界。')
        return self


def planning_constraints(constraints, *, replacement=False):
    """Apply the new-cycle product default, never retrofit saved/old plans."""
    if replacement and constraints.max_workout_actions is None:
        return constraints.model_copy(update={'max_workout_actions':3})
    return constraints


SCOPE = {'workout', 'breakfast', 'lunch', 'dinner', 'snack'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def context(db, user, day):
    """Read-only stable IDs: preparing a draft never materializes a daily override."""
    value = effective_day(db, user, day, fallback_next=False)
    dated = {k: copy.deepcopy(value[k]) for k in ('plan_id','plan_version','adjustment_id','actions','meals','constraints')}
    dated['date'] = day.isoformat()
    dated['workout_cycle_id']=value.get('workout_cycle_id')
    for n, action in enumerate(dated['actions']):
        try:UUID(str(action.get('id')))
        except (ValueError,TypeError,AttributeError):
            action['source_object_id']=action.get('id')
            action['id']=str(uuid5(NAMESPACE_URL,f'{user.id}/{day}/action/{n}/{action.get("id")}'))
    for meal in dated['meals']:
        for n, item in enumerate(meal['items']):
            item.setdefault('id', str(uuid5(NAMESPACE_URL, f'{user.id}/{day}/{meal["id"]}/{n}')))
    profile = db.get(Profile, user.id)
    facts = {}
    for label, model, fields in [
        ('sessions', WorkoutSession, ('id','status','items','agent_log','actual_minutes','note','difficulty','fatigue','started_at')),
        ('meal_logs', MealLog, ('id','slot','title','kcal','protein_g','note')),
        ('workout_logs', WorkoutLog, ('id','session_id','session_item_id','action_ref','sets','duration_min','details')),
    ]:
        facts[label] = [{k: as_utc(getattr(row,k)).isoformat() if isinstance(getattr(row,k),dt.datetime) else copy.deepcopy(getattr(row,k)) for k in fields}
            for row in db.scalars(select(model).where(model.user_id==user.id,model.date==day).order_by(model.id))]
    preferences = {k:copy.deepcopy(getattr(profile,k)) for k in ('goal','equipment','allergies','dislikes','diet_preferences')} if profile else {}
    return {'day':dated, 'facts':facts, 'profile':preferences,
            'allowed_equipment':sorted(allowed_equipment(profile,dated['constraints']))}


def structured_diff(before, after):
    changes = []
    for domain, key in [('workout','actions'),('nutrition','meals')]:
        old = {x['id']: x for x in before[key]}
        new = {x['id']: x for x in after[key]}
        for ident in list(old) + [i for i in new if i not in old]:
            a, b = old.get(ident), new.get(ident)
            kind = 'added' if a is None else 'removed' if b is None else 'unchanged' if a==b else 'replaced' if a.get('action_ref')!=b.get('action_ref') else 'updated'
            changes.append({'domain':domain,'object_id':ident,'kind':kind,'before':a,'after':b})
        if list(old)!=list(new):
            changes.append({'domain':domain,'kind':'order','object_id':None,'before':list(old),'after':list(new)})
    return changes


def _target(base, proposal, scope):
    result = copy.deepcopy(base['day'])
    if proposal.actions is not None:
        if 'workout' not in scope:raise RevisionError('scope','草案没有训练修改权限。')
        old = {x['id']:x for x in result['actions']}
        actions = []
        for proposed in proposal.actions:
            if proposed.id and proposed.id not in old:raise RevisionError('ownership','动作不属于该日期。')
            item = copy.deepcopy(old[proposed.id]) if proposed.id else {'id':new_id(),'meta':{}}
            values = proposed.model_dump(exclude_unset=True,exclude={'id'})
            if any(key in values and values[key] is None for key in ('action_ref','slot')):
                raise RevisionError('schema','动作引用与类型不能清空。')
            ref = values.get('action_ref') or item.get('action_ref')
            # Existing dated objects (e.g. domain-generated warmup) remain valid
            # even when they are not offered as new catalogue exercises.
            if ref != item.get('action_ref') and ref not in all_exercise_refs():
                raise RevisionError('catalog','新增或替换动作必须来自真实目录。')
            if not proposed.id and not proposed.slot:raise RevisionError('schema','新增动作需要明确类型。')
            if ref != item.get('action_ref'):
                # Catalog supplies object metadata; the planner cannot invent equipment/names.
                from app.domain.day_plan import _action_equipment, _action_muscle, _action_mode
                item.update(action_ref=ref,name=name_of(ref),meta={'name_zh':name_of(ref)},
                    equipment=_action_equipment(ref),primary_muscle=_action_muscle(ref),movement_mode=_action_mode(ref))
            item.update(values)
            item.setdefault('slot_key',f'revision:{item["id"]}')
            item.setdefault('day_index',dt.date.fromisoformat(result['date']).isoweekday())
            item.setdefault('seq',len(actions)+1)
            actions.append(item)
        if len({a['id'] for a in actions}) != len(actions):raise RevisionError('duplicate','草案动作ID重复。')
        result['actions'] = actions
    if proposal.meals is not None:
        old = {m['slot']:m for m in result['meals']}
        slots = [m.slot for m in proposal.meals]
        if len(set(slots)) != len(slots):raise RevisionError('duplicate','草案餐次重复。')
        # Only specified, authorized meal slots are replaced; other meals are preserved.
        for proposed in proposal.meals:
            if proposed.slot not in scope:raise RevisionError('scope','餐次不在草案权限内。')
            meal = copy.deepcopy(old.get(proposed.slot) or {'id':new_id(),'slot':proposed.slot,'title':{'breakfast':'早餐','lunch':'午餐','dinner':'晚餐','snack':'加餐'}[proposed.slot]})
            items = []
            for food in proposed.items:
                macros = FOOD_MACROS_PER_100G.get(food.ingredient_key)
                if macros is None:raise RevisionError('nutrition_source','食材缺少可靠营养来源。')
                existing = [i for i in meal.get('items',[]) if i.get('name')==food.ingredient_key]
                item = copy.deepcopy(existing[0]) if len(existing)==1 else {'id':new_id()}
                item.update(name=food.ingredient_key,ingredient_key=food.ingredient_key,
                            grams=food.grams,nutrition_source='internal_catalog_per_100g')
                item.update({key:round(macros[n]*food.grams/100,2) for n,key in enumerate(('kcal','protein','fat','carb'))})
                items.append(item)
            # Catalog identity is exact, no language/substring matching.
            from app.domain.meal_items import merge_food_items
            meal['items'] = merge_food_items(items)
            meal.update({out:round(sum(i[key] for i in meal['items']),2) for out,key in [('kcal','kcal'),('protein_g','protein'),('fat_g','fat'),('carb_g','carb')]})
            old[proposed.slot] = meal
        order = [m['slot'] for m in result['meals']] + [s for s in slots if s not in {m['slot'] for m in result['meals']}]
        result['meals'] = [old[s] for s in order]
    return result


def validate(base, target, scope, constraints, *, allow_workout_replacement=False):
    before = base['day']
    if 'workout' not in scope and target['actions']!=before['actions']:raise RevisionError('scope','训练超出授权范围。')
    old_meals = {m['slot']:m for m in before['meals']};new_meals = {m['slot']:m for m in target['meals']}
    protected_meals = {m['slot'] for m in base['facts']['meal_logs']}
    for slot in set(old_meals)|set(new_meals):
        if (slot not in scope or slot in protected_meals) and old_meals.get(slot)!=new_meals.get(slot):
            raise RevisionError('completed_meal' if slot in protected_meals else 'scope','已记录或范围外的餐次不能被重排。')
    old = {a['id']:a for a in before['actions']};new = {a['id']:a for a in target['actions']}
    from app.domain.safety_rules import is_risky_for
    for action in target['actions']:
        if action==old.get(action['id']):continue
        if _norm_equipment(action.get('equipment')) not in base['allowed_equipment']:
            raise RevisionError('equipment','动作「'+str(action.get('name') or action['action_ref'])+'」所需器械不可用。',
                {'action_ref':action['action_ref'],'required_equipment':_norm_equipment(action.get('equipment')),
                 'allowed_equipment':sorted(base['allowed_equipment'])})
        if any(is_risky_for(action['action_ref'],region) for region in before['constraints'].get('injured_regions',[])):
            raise RevisionError('injury','草案动作不满足现有伤病约束。')
    for session in base['facts']['sessions'] if target['actions']!=before['actions'] and not allow_workout_replacement else []:
        for item in session['items'] or []:
            if int(item.get('completed_sets') or 0)<=0:continue
            matches = [a for a in old.values() if item.get('source_action_id') in {a['id'],a.get('source_object_id')} and item.get('action_ref')==a.get('action_ref')]
            if not matches:
                matches = [a for a in old.values() if a['action_ref']==item['action_ref']]
            if len(matches)!=1:raise RevisionError('completed_binding','无法可靠关联已完成动作，需要先核对。')
            a=matches[0]
            # Position is allowed to change; completed prescription is protected.
            keys=('id','action_ref','slot','sets','reps','duration_min')
            if any(a.get(k)!=(new.get(a['id']) or {}).get(k) for k in keys):raise RevisionError('completed_workout','已发生训练不能被草案删除或改写。')
    for log in base['facts']['workout_logs'] if target['actions']!=before['actions'] and not allow_workout_replacement else []:
        if log['session_item_id'] or not (log.get('sets') or log.get('duration_min')):continue
        matches=[a for a in old.values() if a['action_ref']==log['action_ref']]
        if len(matches)>1:raise RevisionError('completed_binding','旧日志对应多个计划动作，需要先核对。')
        if matches:
            a=matches[0]
            if any(a.get(k)!=(new.get(a['id']) or {}).get(k) for k in ('id','action_ref','slot','sets','reps','duration_min')):
                raise RevisionError('completed_workout','已完成的旧训练日志不能被整日草案覆盖。')
    minutes=sum(_estimated_minutes(a) for a in target['actions'])
    if constraints.max_workout_minutes is not None and minutes>constraints.max_workout_minutes:
        raise RevisionError('time_budget','整场训练估计时长超过限制（包含热身与已完成部分）。')
    if constraints.min_workout_minutes is not None and minutes<constraints.min_workout_minutes:
        raise RevisionError('time_minimum','整场训练估计时长未达到要求的下限。',{'actual_minutes':minutes,'minimum_minutes':constraints.min_workout_minutes})
    if constraints.target_workout_minutes is not None and abs(minutes-constraints.target_workout_minutes)>constraints.workout_time_tolerance_min:
        raise RevisionError('time_target','动作时长合计没有达到用户的目标时长，不能只修改时长上限。',
            {'actual_minutes':minutes,'target_minutes':constraints.target_workout_minutes,'tolerance_minutes':constraints.workout_time_tolerance_min})
    count=sum(1 for a in target['actions'] if constraints.include_warmup_in_action_count or a['slot']!='warmup')
    if 'workout' in scope and constraints.max_workout_actions is not None and count>constraints.max_workout_actions:
        raise RevisionError('action_count','目标动作数量超过限制，请精简动作而非减少组数。',
            {'actual_actions':count,'max_workout_actions':constraints.max_workout_actions,
             'include_warmup':constraints.include_warmup_in_action_count})
    if 'workout' in scope and target['actions'] and target['actions'][0]['slot']!='warmup':
        raise RevisionError('warmup','训练安排必须以热身开始。')
    from app.domain.allergens import resolve_tags
    terms = list(constraints.excluded_ingredients) + list(base['profile'].get('allergies') or []) + list(base['profile'].get('diet_preferences') or [])
    tags, _ = resolve_tags(terms)
    excluded=set(terms) | tags
    if 'seafood' in excluded: excluded.add('fish')
    for meal in target['meals']:
        if meal['slot'] not in scope:continue
        if meal['slot']=='snack' and 'snack' not in old_meals:
            raise RevisionError('meal_scope','不能自动新增加餐；需要用户明确要求并单独核对。')
        if any(i.get('ingredient_key',i.get('name')) in excluded or FOOD_TAGS.get(i.get('ingredient_key',i.get('name')),set()) & excluded for i in meal['items']):raise RevisionError('ingredient_constraint','草案包含明确排除的食材。')
    meals={m['slot']:m for m in target['meals']}
    for rule in constraints.meal_ingredient_rules:
        items=[i for i in meals.get(rule.slot,{}).get('items',[]) if i.get('ingredient_key',i.get('name')) in rule.ingredient_options]
        if (rule.requirement=='include' and not items) or (rule.requirement=='exclude' and items):
            raise RevisionError('meal_goal','餐次没有满足已保存的食材要求。',rule.model_dump())
        if rule.requirement=='include' and rule.grams is not None and abs(sum(i['grams'] for i in items)-rule.grams)>0.01:
            raise RevisionError('meal_quantity','餐次没有满足已指定的食材克数。',rule.model_dump())


def etag(row):
    return digest({'id':row.id,'version':row.version,'base':row.base_revision,'scope':row.scope,'target':row.target_plan,'constraints':row.constraints})


def payload(row):
    return {'revision_id':row.id,'version':row.version,'etag':etag(row),'date':str(row.date),'status':row.status,
            'scope':row.scope,'constraints':row.constraints,'target_plan':row.target_plan,'changes':row.changes,
            'expires_at':as_utc(row.expires_at).isoformat(),'result':row.result,
            'workout_minutes_before':sum(_estimated_minutes(a) for a in row.base_state['day']['actions']),
            'workout_minutes_after':sum(_estimated_minutes(a) for a in row.target_plan['actions']),
            'workout_policy':row.target_plan.get('workout_policy','preserve_current'),
            'previous_training':[{'name':i.get('name') or i['action_ref'],'completed_sets':i.get('completed_sets') or 0}
                for s in row.base_state['facts']['sessions'] for i in s['items'] or [] if int(i.get('completed_sets') or 0)>0]}


def _owned(db,user,ident):
    row=db.get(DayPlanRevision,ident)
    if row is None or row.user_id!=user.id:raise RevisionError('ownership','草案不存在或不属于当前用户。')
    return row


def _ready(row,expected):
    if row.status!='awaiting_confirmation':raise RevisionError('state','该草案已结束，不能再次修改或提交。')
    if as_utc(row.expires_at)<=dt.datetime.now(dt.timezone.utc):
        row.status='expired'
        raise RevisionError('expired','草案已过期。')
    if etag(row)!=expected:raise RevisionError('revision','草案版本已变化，请重新预览。')


def create(db,user,day,*,scope,proposal,expected_base_revision,constraints=None,workout_policy='preserve_current',replan_authorization_id=None):
    if not scope or len(set(scope))!=len(scope) or not set(scope)<=SCOPE:raise RevisionError('scope','需要明确且有效的修改范围。')
    lock(db,user)
    base=context(db,user,day)
    if digest(base)!=expected_base_revision:raise RevisionError('conflict','生成草案期间数据已变化，需要重新读取上下文。')
    if not base['day']['plan_id']:raise RevisionError('plan','需要已有计划。')
    rule=planning_constraints(Constraints.model_validate(constraints or {}),replacement=workout_policy=='replace_remaining')
    target=_target(base,TargetProposal.model_validate(proposal),scope)
    replacement=workout_policy=='replace_remaining'
    if workout_policy not in {'preserve_current','replace_remaining'}:raise RevisionError('schema','未知的训练重排方式。')
    if replacement:
        from app.domain.workout_replan import authorized
        if 'workout' not in scope or not replan_authorization_id:raise RevisionError('replan_consent','请先确认放弃原方案的未完成部分。')
        authorized(db,user,replan_authorization_id,day,['workout'],digest(base))
        for action in target['actions']:
            action['previous_action_id']=action['id']
            action['id']=str(uuid5(NAMESPACE_URL,f'workout-cycle/{replan_authorization_id}/{action["id"]}'))
            action['workout_cycle_id']=replan_authorization_id
        target.update(workout_policy=workout_policy,replan_authorization_id=replan_authorization_id,workout_cycle_id=replan_authorization_id)
    validate(base,target,scope,rule,allow_workout_replacement=replacement)
    expires=min(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=30),day_end_utc(day,db.get(Profile,user.id)))
    if expires<=dt.datetime.now(dt.timezone.utc):raise RevisionError('expired','不能创建已过期日期的草案。')
    row=DayPlanRevision(user_id=user.id,date=day,scope=scope,base_state=base,base_revision=digest(base),
        target_plan=target,constraints=rule.model_dump(),changes=structured_diff(base['day'],target),expires_at=expires)
    db.add(row);db.flush()
    return payload(row)


def preview(db,user,ident):
    row=_owned(db,user,ident)
    if row.status=='awaiting_confirmation' and as_utc(row.expires_at)<=dt.datetime.now(dt.timezone.utc):row.status='expired'
    return payload(row)


def update_draft(db,user,ident,*,expected,proposal,constraints=None):
    return _edit_draft(db,user,ident,expected=expected,proposal=proposal,constraints=constraints)


def extend_draft(db,user,ident,*,expected,additional_scope,proposal,constraints=None):
    # This operation edits a preview only. It never widens the permission to
    # abandon training or applies the newly requested meal without confirmation.
    if not additional_scope or len(set(additional_scope))!=len(additional_scope) or not set(additional_scope)<=SCOPE-{'workout'}:
        raise RevisionError('scope','扩展草案需明确新增餐次，不能以此授权放弃训练。')
    return _edit_draft(db,user,ident,expected=expected,proposal=proposal,constraints=constraints,additional_scope=additional_scope)


def _edit_draft(db,user,ident,*,expected,proposal,constraints=None,additional_scope=()):
    lock(db,user);row=_owned(db,user,ident);_ready(row,expected)
    if digest(context(db,user,row.date))!=row.base_revision:raise RevisionError('conflict','当天计划或实际记录已变化，需要新草案。')
    target,rules,scope=preview_edit(row.base_state,payload(row),TargetProposal.model_validate(proposal),constraints,additional_scope)
    replacement=target.get('workout_policy')=='replace_remaining'
    if replacement:
        from app.domain.workout_replan import authorized
        authorized(db,user,target['replan_authorization_id'],row.date,['workout'],row.base_revision)
    row.revision_history=[*(row.revision_history or []),{'version':row.version,'scope':list(row.scope),'target_plan':row.target_plan,'constraints':row.constraints,'etag':etag(row)}]
    row.target_plan=target;row.constraints=rules.model_dump();row.scope=scope;row.version+=1
    row.changes=structured_diff(row.base_state['day'],target);db.flush()
    return payload(row)


def preview_edit(base,draft,proposal,constraints=None,additional_scope=()):
    """Shared read-only preflight and execution contract, no draft/DB mutations."""
    if additional_scope and (not set(additional_scope)<=SCOPE-{'workout'} or len(set(additional_scope))!=len(additional_scope)):
        raise RevisionError('scope','只能显式扩展新增餐次。')
    if set(additional_scope)&set(draft['scope']):raise RevisionError('scope','新增餐次已在草案范围内，请使用普通修改。')
    scope=[*draft['scope'],*additional_scope]
    if not set(additional_scope)<={m.slot for m in proposal.meals or []}:raise RevisionError('scope','新增餐次必须有完整餐食方案。')
    rules=Constraints.model_validate({**draft['constraints'],**(constraints or {})})
    prior={**copy.deepcopy(base),'day':copy.deepcopy(draft['target_plan'])}
    target=_target(prior,proposal,scope)
    replacement=target.get('workout_policy')=='replace_remaining'
    if replacement:
        for action in target['actions']:action['workout_cycle_id']=target['replan_authorization_id']
    validate(base,target,scope,rules,allow_workout_replacement=replacement)
    return target,rules,scope


def cancel(db,user,ident,*,expected):
    lock(db,user);row=_owned(db,user,ident)
    if row.status=='cancelled' and etag(row)==expected:return payload(row)
    _ready(row,expected);row.status='cancelled';db.flush()
    return payload(row)


def apply(db,user,ident,*,confirmed_etag):
    """Caller must supply the saved confirmation's etag, then commit its transaction."""
    lock(db,user);row=_owned(db,user,ident)
    if row.status=='applied' and etag(row)==confirmed_etag:return payload(row)
    _ready(row,confirmed_etag)
    if digest(context(db,user,row.date))!=row.base_revision:
        row.status='conflicted'
        raise RevisionError('conflict','当天计划或完成记录已变化，旧确认不能提交。')
    replacement=row.target_plan.get('workout_policy')=='replace_remaining'
    authorization=None
    if replacement:
        from app.domain.workout_replan import authorized
        authorization=authorized(db,user,row.target_plan['replan_authorization_id'],row.date,['workout'],row.base_revision)
    validate(row.base_state,row.target_plan,row.scope,Constraints.model_validate(row.constraints),allow_workout_replacement=replacement)
    with db.begin_nested():
        target=copy.deepcopy(row.target_plan)
        if authorization:
            from app.domain.workout_replan import archive_sessions
            archive_sessions(db,user,row.date)
            authorization.status='applied';authorization.applied_at=dt.datetime.now(dt.timezone.utc)
            db.flush()
        result=save_override(db,user,row.date,actions=target['actions'],meals=target['meals'],
            constraints=target['constraints'],summary='整日计划草案已应用',reason='用户确认结构化草案',
            base_version=target['plan_version'],expected_adjustment_id=target['adjustment_id'])
        db.flush();db.expire_all()
        current=context(db,user,row.date)
        if current['day']['actions']!=target['actions'] or current['day']['meals']!=target['meals']:
            raise RevisionError('verification','提交后重读结果与确认草案不符。')
        if current['facts']['meal_logs']!=row.base_state['facts']['meal_logs']:
            raise RevisionError('verification','事实记录意外变化，已回滚。')
        old_logs={l['id']:l for l in row.base_state['facts']['workout_logs']}
        new_logs={l['id']:l for l in current['facts']['workout_logs']}
        if any(new_logs.get(k)!=v for k,v in old_logs.items()):raise RevisionError('verification','已有训练日志意外变化。')
        allowed={(s['id'],i['id']):i for s in row.base_state['facts']['sessions'] for i in s['items'] if int(i.get('completed_sets') or 0)>0} if replacement else {}
        for key,log in new_logs.items():
            if key in old_logs:continue
            original=allowed.get((log['session_id'],log['session_item_id']))
            if not original or log['sets']!=original['completed_sets'] or log['details'].get('set_logs')!=(original.get('set_logs') or []):
                raise RevisionError('verification','补存的训练日志与已有事实不一致。')
        sessions={s['id']:s for s in current['facts']['sessions']}
        for previous in row.base_state['facts']['sessions']:
            after=sessions.get(previous['id'])
            if after is None or any(after[k]!=previous[k] for k in ('actual_minutes','note','difficulty','fatigue','agent_log')):
                raise RevisionError('verification','训练事实或审计意外变化，已回滚。')
            items={i['id']:i for i in after['items']}
            for item in previous['items']:
                if not int(item.get('completed_sets') or 0):continue
                if any(item.get(k)!=(items.get(item['id']) or {}).get(k) for k in ('completed_sets','set_logs','reported_duration_min','duration_source')):
                    raise RevisionError('verification','已记录组数或逐组事实意外变化，已回滚。')
        row.status='applied';row.result={'adjustment_id':result['adjustment_id'],'verified':True,'applied_etag':confirmed_etag}
        db.flush()
    return payload(row)
