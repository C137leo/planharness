"""Audit all catalog entries against cached upstream data; export only exact matches.

Raw input stays ignored. No substring/fuzzy matching or generic movement fallback.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def key(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())

# Reviewed equivalents, including the implement when the catalog specifies it.
ALIASES = {
 'back-extension': 'Hyperextensions_Back_Extensions',
 'glute-bridge': 'Butt_Lift_Bridge',
 'push-up': 'Pushups',
 'bench-press': 'Barbell_Bench_Press_-_Medium_Grip',
 'squat': 'Barbell_Full_Squat',
 'bicep-curl': 'Dumbbell_Bicep_Curl',
 'one-arm-dumbbell-row': 'One-Arm_Dumbbell_Row',
 'seated-row': 'Seated_Cable_Rows',
}

def main():
    catalog = json.loads((ROOT/'vendor/healthy-fitness-coach-skill/healthy-fitness-coach/assets/workout-guide-catalog.json').read_text())['exercises']
    raw = json.loads((ROOT/'data/rag/free_exercise_db.json').read_text())
    by_id = {x['id']: x for x in raw}
    names = {}
    for x in raw: names.setdefault(key(x['name']), []).append(x)
    labels = json.loads((ROOT/'app/domain/exercise_labels.json').read_text())
    local = {x['action_ref'] for x in json.loads((ROOT/'app/domain/exercise_guidance.json').read_text())}
    result = {}
    for x in catalog:
        ref = x['slug']
        matches = names.get(key(x['name']), [])
        match = by_id.get(ALIASES.get(ref)) if ref in ALIASES else matches[0] if len(matches) == 1 else None
        item = {'name_zh': labels[ref], 'name_en': x['name'], 'available': ref in local}
        if match and match.get('instructions'):
            item.update(available=True, upstream_id=match['id'], search_term=match['name'],
                        instructions=match['instructions'], source_url='https://github.com/yuhonas/free-exercise-db/blob/main/exercises/'+match['id']+'.json')
        result[ref] = item
    (ROOT/'app/domain/exercise_guidance_catalog.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(f'{len(result)} audited; {sum(x["available"] for x in result.values())} available')

if __name__ == '__main__': main()
