"""First-plan preference, after equipment and safety eligibility, never a ref alias."""
import copy
import json
from pathlib import Path

# Multi-piece equipment missing from the legacy catalog's single enum.
EXTRA_EQUIPMENT = {'back-extension': {'hyperextension_bench'},
    'dumbbell-bench-press': {'bench'}, 'incline-bench-press': {'bench'},
    'bulgarian-split-squat': {'bench'}, 'chest-supported-row': {'bench'}}

GUIDANCE_CATALOG = json.loads(Path(__file__).with_name('exercise_guidance_catalog.json').read_text())


def has_guidance(ref):
    return bool(GUIDANCE_CATALOG.get(ref, {}).get('available'))


def prefer_guided_actions(payload, profile, memory=None, *, instruction=""):
    if payload.get("status") != "ready": return
    from app.domain.exercise_catalog import EXERCISE_CATALOG
    from app.domain.day_plan import allowed_equipment, _action_equipment, _action_mode
    from app.domain.constraint_guard import _injured_regions, _disliked_refs, _ref_key
    from app.domain.safety_rules import is_risky_for
    from app.domain.revisions import MODE_ALTERNATIVES
    allowed = allowed_equipment(profile, memory or {})
    allowed.update(set(getattr(profile, "equipment", None) or []) & {"hyperextension_bench"})
    injured = _injured_regions(memory, instruction)
    disliked = _disliked_refs(memory)
    for day in payload.get('days', []):
        used = {a.get('action_ref') for a in day.get('actions', [])}
        for action in day.get('actions', []):
            ref = action.get('action_ref', '')
            if action.get('slot') == 'warmup' or has_guidance(ref): continue
            mode = _action_mode(ref, action.get('meta'))
            # An explicit curated mode list bounds the candidates; guidance never
            # overrides equipment/safety or replaces a different mode by muscle alone.
            candidates = [r for r in MODE_ALTERNATIVES.get(mode, [])
                if r not in used and _ref_key(r) not in disliked and has_guidance(r) and r in EXERCISE_CATALOG
                and _action_mode(r, {'movement_mode': mode}) == mode
                and _action_equipment(r) in allowed
                and EXTRA_EQUIPMENT.get(r, set()).issubset(allowed)
                and not any(is_risky_for(r, region, EXERCISE_CATALOG[r]) for region in injured)]
            if not candidates: continue
            chosen = candidates[0]
            used.discard(ref)
            used.add(chosen)
            action['action_ref'] = chosen
            action['meta'] = {**copy.deepcopy(EXERCISE_CATALOG[chosen]), 'movement_mode': mode,
                              'guidance_preferred_from': ref}
            if 'name' in action: action['name'] = EXERCISE_CATALOG[chosen]['name_zh']
