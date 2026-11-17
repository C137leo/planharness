"""Catalogue-backed allergy checks shared by every planned-food write boundary."""
import copy
from app.domain.allergens import resolve_tags, split_terms
from app.domain.nutrition_catalog import FOOD_TAGS, FOOD_MACROS_PER_100G, ingredient_key


def restrictions(profile):
    terms = split_terms(list(profile.allergies or []) + list(profile.diet_preferences or []))
    tags, unknown = resolve_tags(terms)
    return terms, tags, unknown


def forbidden(name, profile):
    terms, tags, _ = restrictions(profile)
    key = ingredient_key(name) or name
    named_ingredients = {ingredient_key(term) or term for term in terms}
    return key in named_ingredients or bool(FOOD_TAGS.get(key, set()) & tags)


def check_meals(meals, profile):
    for meal in meals:
        for item in meal.get('items', []):
            if forbidden(item.get('ingredient_key') or item.get('name', ''), profile):
                raise ValueError('餐食包含已保存的过敏或忌口食材：' + item.get('name', '该食材'))


def repair_meals(meals, profile):
    """Replace only conflicting foods within existing meals; never add a snack."""
    result = copy.deepcopy(meals)
    safe = next((key for key in ('鸡胸肉','豆腐','牛肉','鸡蛋') if not forbidden(key, profile)), None)
    changed = False
    for meal in result:
        for item in meal.get('items', []):
            if not forbidden(item.get('ingredient_key') or item.get('name', ''), profile):
                continue
            if safe is None:
                raise ValueError('当前目录无法提供合适替代食材，请核对多项过敏限制。')
            item.update(name=safe, ingredient_key=safe, nutrition_source='internal_catalog_per_100g')
            item.pop('ingredient_id', None)
            grams = float(item.get('grams') or 100)
            item.update({field: round(value*grams/100, 1) for field,value in zip(('kcal','protein','fat','carb'), FOOD_MACROS_PER_100G[safe])})
            changed = True
        meal.update({field:round(sum(float(i.get(key,0)) for i in meal.get('items',[])),1)
                     for field,key in [('kcal','kcal'),('protein_g','protein'),('fat_g','fat'),('carb_g','carb')]})
    check_meals(result, profile)
    return result, changed


def repair_saved_future_meals(db, user, profile):
    """Append a safe template version and repair future overrides; facts stay intact."""
    from sqlalchemy import select
    from app.models import Plan, PlanVersion, PlanAction, MealPlan, PlanAdjustment, MealLog, WorkoutSession
    from app.domain.timeutil import user_today
    from app.domain.coach import _commit_actions, _write_meals
    today=user_today(profile)
    from app.domain.day_plan import effective_day, save_override
    session_dates=set(db.scalars(select(WorkoutSession.date).where(WorkoutSession.user_id==user.id,WorkoutSession.date>=today)))
    preserved={date:effective_day(db,user,date,fallback_next=False) for date in session_dates}
    plan = db.scalar(select(Plan).where(Plan.user_id==user.id, Plan.status=='active').with_for_update())
    previous_version = plan.current_version if plan else None
    version_no = None
    if plan:
        old = db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id, PlanVersion.version==plan.current_version))
        rows = db.scalars(select(MealPlan).where(MealPlan.plan_version_id==old.id)).all()
        meals = [{k:copy.deepcopy(getattr(row,k)) for k in ('day_index','slot','title','items','kcal','protein_g','fat_g','carb_g')} for row in rows]
        safe, changed = repair_meals(meals, profile)
        if changed:
            actions = [{k:copy.deepcopy(getattr(a,k)) for k in ('day_index','slot','seq','slot_key','action_type','action_ref','sets','reps','duration_min','intensity','portion_g','meta')}
                       for a in db.scalars(select(PlanAction).where(PlanAction.plan_version_id==old.id))]
            new = _commit_actions(db,plan,actions,old.calorie_target,old.protein_target_g,old.step_target,{'summary':'长期过敏限制已更新；训练保留，食谱排除冲突食材'},'allergy safety update')
            _write_meals(db,user.id,new.id,safe)
            version_no = new.version
    today=user_today(profile)
    for row in db.scalars(select(PlanAdjustment).where(PlanAdjustment.user_id==user.id,PlanAdjustment.effective_date>=today,PlanAdjustment.status=='active')):
        if plan and row.base_version not in (None,previous_version):continue
        eaten = {m.slot for m in db.scalars(select(MealLog).where(MealLog.user_id==user.id,MealLog.date==row.effective_date))}
        mutable=[m for m in row.meals or [] if m['slot'] not in eaten]
        safe,changed=repair_meals(mutable,profile)
        if version_no is not None: row.base_version = version_no
        if changed:
            by_slot={m['slot']:m for m in safe}
            row.meals=[by_slot.get(m['slot'],m) for m in row.meals]
    if version_no is not None:
        for date,day in preserved.items():
            existing=db.scalar(select(PlanAdjustment).where(PlanAdjustment.user_id==user.id,PlanAdjustment.effective_date==date,PlanAdjustment.status=='active'))
            if existing is not None and existing.base_version in (None,version_no):continue
            eaten={m.slot for m in db.scalars(select(MealLog).where(MealLog.user_id==user.id,MealLog.date==date))}
            safe,_=repair_meals([m for m in day['meals'] if m['slot'] not in eaten],profile)
            by_slot={m['slot']:m for m in safe}
            save_override(db,user,date,actions=day['actions'],meals=[by_slot.get(m['slot'],m) for m in day['meals']],
                constraints=day['constraints'],summary='长期过敏限制已更新，已有训练安排与记录保留',reason='allergy_safety',base_version=version_no,
                sync_sessions=False)
    repair_ready_previews(db,user,profile,plan,previous_version)
    db.flush()
    return version_no


def repair_ready_previews(db,user,profile,plan,previous_version):
    """Keep live previews safe; never revive a cancelled/superseded draft."""
    from sqlalchemy import select
    from app.models import PlanDraft,CoachPendingPlan
    from app.domain.coach import initial_profile_revision
    changed_ids=[]
    seen=set()
    for row in db.scalars(select(PlanDraft).where(PlanDraft.user_id==user.id).order_by(PlanDraft.created_at.desc())):
        if row.status not in {'ready','confirmed','rejected','superseded'}:continue
        if row.plan_id in seen:continue
        seen.add(row.plan_id)
        if row.status!='ready':continue
        if row.plan_id and (not plan or row.plan_id!=plan.id or row.base_version!=previous_version):
            continue
        payload=copy.deepcopy(row.payload)
        payload['meals'],_=repair_meals(payload.get('meals',[]),profile)
        meta=payload.setdefault('meta',{})
        basis_key='replacement_profile_revision' if row.plan_id else 'initial_profile_revision'
        meta[basis_key]=initial_profile_revision(db,user)
        contract=meta.get('goal_contract')
        if contract is not None:
            contract['allergies']=list(dict.fromkeys([*(contract.get('allergies') or []),*(profile.allergies or [])]))
            rules=[]
            for rule in contract.get('meal_rules',[]):
                rule=copy.deepcopy(rule)
                if rule.get('requirement')=='include':
                    rule['ingredient_options']=[key for key in rule['ingredient_options'] if not forbidden(key,profile)]
                    if not rule['ingredient_options']:continue
                rules.append(rule)
            contract['meal_rules']=rules

        row.payload=payload
        if row.plan_id:row.base_version=plan.current_version
        changed_ids.append(row.id)
    # The old authorization is tied to the old profile, body and etag.
    for pending in db.scalars(select(CoachPendingPlan).where(CoachPendingPlan.user_id==user.id,CoachPendingPlan.status=='pending')):
        if any(t['resolution'].get('selected_id') in changed_ids for t in pending.tasks):
            pending.status='stale';pending.version+=1
    db.info['allergy_preview_ids']=changed_ids
